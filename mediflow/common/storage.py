"""Blob storage: local encrypted folder, or Azure Blob Storage.

Local files are encrypted at rest with AES-256-GCM (key held in the Key Vault
stand-in). Azure Blob Storage encrypts at rest by default (SSE); access is via
managed identity + RBAC, public access disabled, and private endpoints in prod.
"""
from __future__ import annotations

import base64
import os
import threading
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config


class LocalBlobStorage:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or config.data_dir() / "blobs")
        self.root.mkdir(parents=True, exist_ok=True)
        key_b64 = config.env("MEDIFLOW_STORAGE_KEY") or self._key()
        self.aes = AESGCM(base64.urlsafe_b64decode(key_b64))

    def _key(self) -> str:
        path = config.data_dir() / ".storage_key"
        if not path.exists():
            try:
                with open(path, "x") as fh:
                    fh.write(base64.urlsafe_b64encode(AESGCM.generate_key(256)).decode())
            except FileExistsError:
                pass
        return path.read_text().strip()

    def _path(self, container: str, name: str) -> Path:
        path = (self.root / container / name).resolve()
        if not str(path).startswith(str(self.root.resolve())):  # path traversal guard
            raise ValueError("invalid blob path")
        return path

    def upload(self, container: str, name: str, data: bytes, content_type: str = "") -> str:
        path = self._path(container, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        nonce = os.urandom(12)
        path.write_bytes(nonce + self.aes.encrypt(nonce, data, name.encode()))
        return f"{container}/{name}"

    def download(self, container: str, name: str) -> bytes:
        raw = self._path(container, name).read_bytes()
        return self.aes.decrypt(raw[:12], raw[12:], name.encode())


class AzureBlobStorage:  # pragma: no cover
    def __init__(self):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        account = config.env("MEDIFLOW_STORAGE_ACCOUNT")
        self.client = BlobServiceClient(f"https://{account}.blob.core.windows.net", DefaultAzureCredential())

    def upload(self, container: str, name: str, data: bytes, content_type: str = "") -> str:
        from azure.storage.blob import ContentSettings

        self.client.get_blob_client(container, name).upload_blob(
            data, overwrite=False, content_settings=ContentSettings(content_type=content_type)
        )
        return f"{container}/{name}"

    def download(self, container: str, name: str) -> bytes:
        return self.client.get_blob_client(container, name).download_blob().readall()


_storage = None
_storage_lock = threading.Lock()


def get_storage():
    global _storage
    if _storage is None:
        with _storage_lock:
            if _storage is None:
                _storage = AzureBlobStorage() if config.mode("storage") == "azure" else LocalBlobStorage()
    return _storage


def reset_storage() -> None:
    global _storage
    _storage = None
