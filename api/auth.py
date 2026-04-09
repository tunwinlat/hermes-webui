"""
Hermes Web UI -- Optional password authentication.
Off by default. Enable by setting HERMES_WEBUI_PASSWORD env var
or configuring a password in the Settings panel.
"""
import hashlib
import hmac
import http.cookies
import os
import secrets
import threading
import time

from api.config import STATE_DIR, load_settings

# ── Rate limiting ─────────────────────────────────────────────────────────────
_MAX_ATTEMPTS = 5           # lock out after this many failures
_WINDOW_SECS = 900          # 15-minute window
_LOCKOUT_SECS = 900         # 15-minute lockout
_ATTEMPT_RECORD_MAX = 10    # cap stored attempts per IP

_rate_lock = threading.Lock()
_rate_store: dict[str, list[float]] = {}   # ip -> list of failure timestamps


def _get_client_ip(handler) -> str:
    """Return the best-effort client IP, preferring X-Forwarded-For if present."""
    xff = handler.headers.get('X-Forwarded-For', '').strip()
    if xff:
        return xff.split(',')[0].strip()
    return handler.client_address[0]


def _check_rate_limit(handler) -> tuple[bool, str]:
    """Return (allowed, reason). If not allowed, reason describes the lockout."""
    ip = _get_client_ip(handler)
    now = time.time()
    window_start = now - _WINDOW_SECS

    with _rate_lock:
        attempts = _rate_store.get(ip, [])

        # Prune old entries outside the window
        attempts = [t for t in attempts if t > window_start]
        _rate_store[ip] = attempts

        # Check for active lockout
        if len(attempts) >= _MAX_ATTEMPTS:
            earliest = min(attempts)
            lockout_remaining = int(_LOCKOUT_SECS - (now - earliest))
            if lockout_remaining > 0:
                return False, f"Too many failed attempts. Try again in {lockout_remaining // 60} minutes."

        return True, ""


def _record_failed_attempt(handler) -> None:
    """Log a failed login attempt for the current IP."""
    ip = _get_client_ip(handler)
    now = time.time()
    with _rate_lock:
        attempts = _rate_store.get(ip, [])
        window_start = now - _WINDOW_SECS
        attempts = [t for t in attempts if t > window_start]
        attempts.append(now)
        # Cap list size to prevent memory bloat
        _rate_store[ip] = attempts[-_ATTEMPT_RECORD_MAX:]


def _clear_failed_attempts(handler) -> None:
    """Clear failed attempts on successful login."""
    ip = _get_client_ip(handler)
    with _rate_lock:
        _rate_store.pop(ip, None)

# ── Public paths (no auth required) ─────────────────────────────────────────
PUBLIC_PATHS = frozenset({
    '/login', '/health', '/favicon.ico',
    '/api/auth/login', '/api/auth/status',
})

COOKIE_NAME = 'hermes_session'
SESSION_TTL = 86400  # 24 hours

# Active sessions: token -> expiry timestamp
_sessions = {}

# ── Login rate limiter ──────────────────────────────────────────────────────
_login_attempts = {}  # ip -> [timestamp, ...]
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW = 60  # seconds

def _check_login_rate(ip: str) -> bool:
    """Return True if the IP is allowed to attempt login."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    # Prune old attempts
    attempts = [t for t in attempts if now - t < _LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) < _LOGIN_MAX_ATTEMPTS

def _record_login_attempt(ip: str) -> None:
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    attempts.append(now)
    _login_attempts[ip] = attempts


def _signing_key():
    """Return a random signing key, generating and persisting one on first call."""
    key_file = STATE_DIR / '.signing_key'
    if key_file.exists():
        try:
            raw = key_file.read_bytes()
            if len(raw) >= 32:
                return raw[:32]
        except Exception:
            pass
    # Generate a new random key
    key = secrets.token_bytes(32)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(key)
        key_file.chmod(0o600)
    except Exception:
        pass  # key works for this process even if persist fails
    return key


def _hash_password(password):
    """PBKDF2-SHA256 with 600k iterations (OWASP recommendation).
    Salt is the persisted random signing key, which is secret and unique per
    installation. This keeps the stored hash format a plain hex string
    (no format change to settings.json) while replacing the predictable
    STATE_DIR-derived salt from the original implementation."""
    salt = _signing_key()
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600_000)
    return dk.hex()


def get_password_hash() -> str | None:
    """Return the active password hash, or None if auth is disabled.
    Priority: env var > settings.json."""
    env_pw = os.getenv('HERMES_WEBUI_PASSWORD', '').strip()
    if env_pw:
        return _hash_password(env_pw)
    settings = load_settings()
    return settings.get('password_hash') or None


def is_auth_enabled() -> bool:
    """True if a password is configured (env var or settings)."""
    return get_password_hash() is not None


def verify_password(plain) -> bool:
    """Verify a plaintext password against the stored hash."""
    expected = get_password_hash()
    if not expected:
        return False
    return hmac.compare_digest(_hash_password(plain), expected)


def create_session() -> str:
    """Create a new auth session. Returns signed cookie value."""
    token = secrets.token_hex(32)
    _sessions[token] = time.time() + SESSION_TTL
    sig = hmac.new(_signing_key(), token.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{token}.{sig}"


def verify_session(cookie_value) -> bool:
    """Verify a signed session cookie. Returns True if valid and not expired."""
    if not cookie_value or '.' not in cookie_value:
        return False
    token, sig = cookie_value.rsplit('.', 1)
    expected_sig = hmac.new(_signing_key(), token.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected_sig):
        return False
    expiry = _sessions.get(token)
    if not expiry or time.time() > expiry:
        _sessions.pop(token, None)
        return False
    return True


def invalidate_session(cookie_value) -> None:
    """Remove a session token."""
    if cookie_value and '.' in cookie_value:
        token = cookie_value.rsplit('.', 1)[0]
        _sessions.pop(token, None)


def parse_cookie(handler) -> str | None:
    """Extract the auth cookie from the request headers."""
    cookie_header = handler.headers.get('Cookie', '')
    if not cookie_header:
        return None
    cookie = http.cookies.SimpleCookie()
    try:
        cookie.load(cookie_header)
    except http.cookies.CookieError:
        return None
    morsel = cookie.get(COOKIE_NAME)
    return morsel.value if morsel else None


def check_auth(handler, parsed) -> bool:
    """Check if request is authorized. Returns True if OK.
    If not authorized, sends 401 (API) or 302 redirect (page) and returns False."""
    if not is_auth_enabled():
        return True
    # Public paths don't require auth
    if parsed.path in PUBLIC_PATHS or parsed.path.startswith('/static/'):
        return True
    # Check session cookie
    cookie_val = parse_cookie(handler)
    if cookie_val and verify_session(cookie_val):
        return True
    # Not authorized
    if parsed.path.startswith('/api/'):
        handler.send_response(401)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(b'{"error":"Authentication required"}')
    else:
        handler.send_response(302)
        handler.send_header('Location', '/login')
        handler.end_headers()
    return False


def set_auth_cookie(handler, cookie_value) -> None:
    """Set the auth cookie on the response."""
    cookie = http.cookies.SimpleCookie()
    cookie[COOKIE_NAME] = cookie_value
    cookie[COOKIE_NAME]['httponly'] = True
    cookie[COOKIE_NAME]['samesite'] = 'Lax'
    cookie[COOKIE_NAME]['path'] = '/'
    cookie[COOKIE_NAME]['max-age'] = str(SESSION_TTL)
    # Set Secure flag when connection is HTTPS
    if getattr(handler.request, 'getpeercert', None) is not None or handler.headers.get('X-Forwarded-Proto', '') == 'https':
        cookie[COOKIE_NAME]['secure'] = True
    handler.send_header('Set-Cookie', cookie[COOKIE_NAME].OutputString())


def clear_auth_cookie(handler) -> None:
    """Clear the auth cookie on the response."""
    cookie = http.cookies.SimpleCookie()
    cookie[COOKIE_NAME] = ''
    cookie[COOKIE_NAME]['httponly'] = True
    cookie[COOKIE_NAME]['path'] = '/'
    cookie[COOKIE_NAME]['max-age'] = '0'
    handler.send_header('Set-Cookie', cookie[COOKIE_NAME].OutputString())
