"""Offline verification for supporter codes."""

from __future__ import annotations

import base64
import json

TOKEN_PREFIX = "DG"
SIGNATURE_B64_LENGTH = 86
MAX_TOKEN_LENGTH = 4000
BASE64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
# Raw Ed25519 public key. The private key is not in this repository.
PUBLIC_KEY_RAW = bytes.fromhex(
    "c69bf5799a67bbba5c37c2197e63eac05a1711a39964ae2e89b9901248862075"
)

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError:
    InvalidSignature = None  # type: ignore[misc, assignment]
    Ed25519PublicKey = None  # type: ignore[misc, assignment]


class Supporter:
    __slots__ = ("supporter_id", "name")

    def __init__(self, supporter_id: str, name: str) -> None:
        self.supporter_id = supporter_id
        self.name = name


def normalize_supporter_token(token: str) -> str:
    return "".join(token.split())


def verify_supporter_code(token: str) -> Supporter:
    token = normalize_supporter_token(token)
    if not token:
        raise ValueError("Paste a supporter code.")
    if (
        len(token) > MAX_TOKEN_LENGTH
        or not token.startswith(TOKEN_PREFIX)
        or len(token) <= len(TOKEN_PREFIX) + SIGNATURE_B64_LENGTH
        or any(character not in BASE64URL_ALPHABET for character in token[len(TOKEN_PREFIX) :])
    ):
        raise ValueError("This code is not valid.")
    if Ed25519PublicKey is None or InvalidSignature is None:
        raise ValueError("The cryptography package is not installed.")

    encoded = token[len(TOKEN_PREFIX) :]
    try:
        payload_bytes = _b64url_decode(encoded[:-SIGNATURE_B64_LENGTH])
        signature = _b64url_decode(encoded[-SIGNATURE_B64_LENGTH :])
    except Exception as exc:
        raise ValueError("This code is not valid.") from exc
    if len(payload_bytes) > 5000 or len(signature) != 64:
        raise ValueError("This code is not valid.")

    public_key = Ed25519PublicKey.from_public_bytes(PUBLIC_KEY_RAW)
    try:
        public_key.verify(signature, payload_bytes)
    except InvalidSignature as exc:
        raise ValueError("This code is not valid.") from exc

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("This code is not valid.") from exc
    if not isinstance(payload, dict):
        raise ValueError("This code is not valid.")
    if type(payload.get("v")) is not int or payload.get("v") != 1:
        raise ValueError("This code is not valid.")
    name = payload.get("name")
    supporter_id = payload.get("id")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("This code is not valid.")
    if not isinstance(supporter_id, str) or not supporter_id.strip():
        raise ValueError("This code is not valid.")
    return Supporter(supporter_id=supporter_id.strip(), name=name.strip())


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
