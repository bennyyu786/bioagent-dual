"""Safe event adapters: filesystem metadata and authenticated webhook ingestion.

The webhook adapter checks authentication and stores only an event digest, never
untrusted free-text instructions. Put behind TLS and reverse-proxy rate limits.
"""
from __future__ import annotations
import hashlib
import hmac
import os
from pathlib import Path

from .persistent_runtime import EventKind, PersistentRuntimeStore


class FileFingerprintProbe:
    """Inspect a single fixed file/directory metadata without reading file contents.

    A directory only tracks its own metadata, not recursive contents. Watching
    recursive trees requires a trusted external watcher with bounded scope.
    """
    def __init__(self, path: str | Path, *, allowed_root: str | Path):
        root=Path(allowed_root).resolve(strict=True)
        target=Path(path).resolve(strict=True)
        if not target.is_relative_to(root):
            raise PermissionError('Monitored path outside trusted root')
        self.path=target

    async def sample(self) -> dict:
        stat=self.path.stat()
        return {'mtime_ns':stat.st_mtime_ns,'size':stat.st_size,
                'kind':'directory' if self.path.is_dir() else 'file'}


class AuthenticatedWebhookIngress:
    """HMAC-SHA256 signed event ingress with explicit replay nonce.

    Caller handles HTTPS, body-size limits, authentication-key rotation and
    API rate limits. Only digest metadata is committed to the runtime queue.
    """
    def __init__(self, store: PersistentRuntimeStore, secret: bytes):
        if len(secret)<32: raise ValueError('HMAC secret must have at least 32 bytes')
        self.store=store
        self.secret=secret

    def ingest(self, goal_id: str, kind: EventKind, body: bytes, signature: str,
               nonce: str) -> tuple[int,bool]:
        if len(body)>16384 or not nonce or len(nonce)>120:
            raise ValueError('Invalid webhook size/nonce')
        if not isinstance(kind,EventKind): raise ValueError('Invalid event kind')
        expected=hmac.new(self.secret,nonce.encode()+b'\n'+body,hashlib.sha256).hexdigest()
        # Signature is the lowercase hex digest; replay protection is the event key.
        if not hmac.compare_digest(signature,expected):
            raise PermissionError('Invalid webhook signature')
        digest=hashlib.sha256(body).hexdigest()
        return self.store.submit(goal_id,kind,f'webhook:{goal_id}:{nonce}',
                                 {'body_sha256':digest,'authenticated':True})
