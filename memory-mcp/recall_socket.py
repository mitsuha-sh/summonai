"""Shared naming for passive recall Unix sockets."""

from __future__ import annotations

import hashlib
from pathlib import Path


def _normalize_db_path(db_path: str) -> str:
    expanded = Path(db_path).expanduser()
    if not expanded.is_absolute():
        raise ValueError(f"SUMMONAI_MEMORY_DB must be an absolute path: {db_path}")
    normalized = expanded.resolve()
    if not normalized.is_absolute():
        raise ValueError(f"Failed to normalize SUMMONAI_MEMORY_DB as absolute path: {db_path}")
    return str(normalized)


def db_scope_hash(db_path: str) -> str:
    """Return a stable short hash for the realpath of the DB file."""
    normalized = _normalize_db_path(db_path)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def socket_filename(db_path: str, pid: int) -> str:
    return f"summonai_recall_{db_scope_hash(db_path)}_{pid}.sock"


def socket_glob(db_path: str) -> str:
    return f"summonai_recall_{db_scope_hash(db_path)}_*.sock"
