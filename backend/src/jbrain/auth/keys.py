"""Owner-key and session-token primitives.

Keys are 256-bit random values, so a plain salted-less SHA-256 is the right
storage hash: there is nothing to brute-force and no need for a slow KDF
(those exist to compensate for low-entropy human passwords).
"""

import base64
import hashlib
import hmac
import secrets

KEY_PREFIX = "jb1"
_GROUP = 4


def generate_owner_key() -> str:
    """256-bit key rendered in dash-grouped base32 for paper transcription."""
    raw = secrets.token_bytes(32)
    encoded = base64.b32encode(raw).decode().rstrip("=")
    groups = [encoded[i : i + _GROUP] for i in range(0, len(encoded), _GROUP)]
    return "-".join([KEY_PREFIX, *groups])


def normalize_key(key: str) -> str:
    """Tolerate the ways a hand-copied key gets mangled: case, spacing, dashes."""
    compact = "".join(key.split()).replace("-", "").upper()
    prefix = KEY_PREFIX.upper()
    if compact.startswith(prefix):
        compact = compact[len(prefix) :]
    return compact


def hash_key(key: str) -> str:
    return hashlib.sha256(normalize_key(key).encode()).hexdigest()


def verify_key(key: str, key_hash: str) -> bool:
    return hmac.compare_digest(hash_key(key), key_hash)


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
