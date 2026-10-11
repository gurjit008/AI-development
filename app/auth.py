"""Password hashing and opaque session credentials."""
import hashlib
import hmac
import secrets
from datetime import datetime, timezone, timedelta

ITERATIONS = 600_000


def now():
    return datetime.now(timezone.utc).isoformat()


def password_hash(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('Password must contain 12 to 256 characters')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}'


def verify_password(password, encoded):
    try:
        algorithm, iterations, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or not 100_000 <= int(iterations) <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, AttributeError, TypeError):
        return False


def credential():
    return secrets.token_urlsafe(32)


def session_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def expiry(hours=8):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
