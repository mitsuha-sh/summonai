"""Shared naming for passive recall Unix sockets."""

from __future__ import annotations

import hashlib
from pathlib import Path


def _normalize_db_path(db_path: str) -> str:
    return str(Path(db_path).expanduser().resolve())


def db_scope_hash(db_path: str) -> str:
    """Return a stable short hash for the realpath of the DB file."""
    normalized = _normalize_db_path(db_path)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def socket_filename(db_path: str, pid: int) -> str:
    return f"summonai_recall_{db_scope_hash(db_path)}_{pid}.sock"


def socket_glob(db_path: str) -> str:
    return f"summonai_recall_{db_scope_hash(db_path)}_*.sock"
