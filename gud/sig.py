"""Ed25519 keys and signatures for agent members."""

from __future__ import annotations

import base64
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .wire import sha, signing_bytes


def generate() -> tuple[bytes, str]:
    """Returns (private key raw bytes, public key string `ed25519:<b64>`)."""
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                            serialization.NoEncryption())
    return raw, public_of(raw)


def public_of(private_raw: bytes) -> str:
    pub = Ed25519PrivateKey.from_private_bytes(private_raw).public_key()
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "ed25519:" + base64.b64encode(raw).decode()


def fingerprint(public_key: str) -> str:
    return sha(public_key)[:16]


def sign(private_raw: bytes, trailers: dict[str, str], diff: str) -> str:
    sig = Ed25519PrivateKey.from_private_bytes(private_raw).sign(signing_bytes(trailers, diff))
    return "ed25519:" + base64.b64encode(sig).decode()


def verify(public_key: str | None, trailers: dict[str, str], diff: str) -> bool:
    signature = trailers.get("Gud-Sig")
    if not public_key or not signature or not public_key.startswith("ed25519:") \
            or not signature.startswith("ed25519:"):
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key.split(":", 1)[1]))
        pub.verify(base64.b64decode(signature.split(":", 1)[1]), signing_bytes(trailers, diff))
        return True
    except (InvalidSignature, ValueError):
        return False


def load_private(path: Path) -> bytes:
    return base64.b64decode(path.read_text().strip())


def save_private(path: Path, private_raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(base64.b64encode(private_raw).decode() + "\n")
    path.chmod(0o600)
