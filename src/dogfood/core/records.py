"""Signed participation records and certificates. See contracts/t4-records-widget.md.

Ed25519 over canonical JSON, so anyone can verify offline with openssl and the
published public key, without trusting or reaching the portal.
"""

import base64
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def canonical(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def load_or_create_key(path: Path) -> tuple[Ed25519PrivateKey, bool]:
    """(key, created). The private key never leaves this file."""
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(f"{path} is not an Ed25519 private key")
        return key, False
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return key, True


def public_pem(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def sign(key: Ed25519PrivateKey, record: dict) -> str:
    return base64.b64encode(key.sign(canonical(record))).decode("ascii")


def verify(public_key: Ed25519PublicKey, record: dict, signature_b64: str) -> bool:
    try:
        public_key.verify(base64.b64decode(signature_b64, validate=True), canonical(record))
        return True
    except (InvalidSignature, ValueError):
        return False
