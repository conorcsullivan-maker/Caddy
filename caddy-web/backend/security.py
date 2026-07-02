"""PIN hashing, token generation, and cookie/session policy.

PIN storage format (two generations):
- Legacy: bare unsalted sha256 hexdigest (64 hex chars, no '$'). Still
  verifiable so existing users keep working; upgraded in place on their
  next successful login.
- Current: "pbkdf2$<salt_hex>$<derived_hex>" — PBKDF2-HMAC-SHA256,
  100k iterations, 16-byte random salt.

A 4-digit PIN is low entropy no matter how it's hashed — the salt+work
factor mainly protects against a leaked DB being reversed with a single
rainbow table pass across all users at once.
"""
import hashlib
import hmac
import os
import secrets

# Cookie security: secure HTTPS-only in production, plain in local dev.
# SameSite=Lax works because the frontend proxies /api/* through its own
# origin (vercel.json + next.config.ts rewrites), making cookies first-party.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
COOKIE_SAMESITE = "lax"

# Cookie max-age and server-side session lifetime move together — before
# this existed, tokens lived in the sessions table forever even after the
# cookie expired.
SESSION_MAX_AGE_DAYS = 30

_PBKDF2_ITERATIONS = 100_000


def generate_pin() -> str:
    """Generate a random 4-digit PIN."""
    return f"{secrets.randbelow(10000):04d}"


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_pin(pin: str) -> str:
    """Legacy unsalted sha256 — kept ONLY so admin/create-user-directly can
    keep accepting pre-hashed values from old exports. New hashes should use
    hash_pin_secure."""
    return hashlib.sha256(pin.encode()).hexdigest()


def hash_pin_secure(pin: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return f"pbkdf2${salt}${dk.hex()}"


def is_legacy_hash(stored: str) -> bool:
    return bool(stored) and "$" not in stored


def verify_pin(pin: str, stored: str) -> bool:
    """Constant-time verification against either hash generation."""
    if not stored or not pin:
        return False
    if stored.startswith("pbkdf2$"):
        try:
            _, salt, derived_hex = stored.split("$")
            dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
            return hmac.compare_digest(dk.hex(), derived_hex)
        except (ValueError, TypeError):
            return False
    return hmac.compare_digest(hashlib.sha256(pin.encode()).hexdigest(), stored)
