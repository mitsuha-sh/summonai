#!/usr/bin/env python3
"""
SummonAI Memory MCP Server

SQLite+FTS5 based memory system for a multi-agent workflow.
Implements the general memory architecture design.

Usage:
    python server.py                        # stdio transport (default)
    claude mcp add summonai-memory-mcp -- python /path/to/server.py
"""

import atexit
import os
import hashlib
import json
import math
import re
import socket
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import sqlite_vec
from mcp.server.fastmcp import FastMCP
from sentence_transformers import SentenceTransformer

SCRIPTS_DIR = Path(__file__).parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from hook_context import resolve_agent_id, resolve_scope  # noqa: E402
from recall_socket import socket_filename  # noqa: E402

# Database path: env var or default
DEFAULT_DB_PATH = os.environ.get(
    "SUMMONAI_MEMORY_DB",
    str(Path(__file__).parent / "db" / "summonai_memory.db"),
)
MIGRATIONS_DIR = Path(__file__).parent / "db" / "migrations"
MIGRATION_PATTERN = re.compile(r"^V(?P<version>\d{3})_[A-Za-z0-9_]+\.sql$")

mcp = FastMCP("summonai-memory-mcp")
_embed_model = SentenceTransformer("cl-nagoya/ruri-v3-130m")
_MEMORY_BUCKETS = {"code", "knowledge", "content"}
_MEMORY_LINK_RELATION_TYPES = {
    "derived_from",
    "semantic_sim",
    "supports",
    "contradicts",
    "temporal_next",
}
_BUCKET_DEFAULT_CONFIDENCE = {
    "code": 0.95,
    "knowledge": 0.80,
    "content": 0.60,
}
_BUCKET_MULTIPLIER = {
    "code": 1.08,
    "knowledge": 1.00,
    "content": 0.96,
}
_SUMMARY_FALLBACK_CHARS = 200


def _runtime_source_metadata(source_agent: str | None, source_context: str | None) -> tuple[str | None, str | None]:
    resolved_agent = (source_agent or "").strip()
    if not resolved_agent or resolved_agent == "default":
        resolved_agent = resolve_agent_id({})

    resolved_scope = resolve_scope({})
    runtime_project = resolved_scope.get("project") or "global"
    marker = f"runtime_project={runtime_project}; runtime_agent={resolved_agent}"
    if source_context and source_context.strip():
        if marker not in source_context:
            source_context = f"{source_context.rstrip()}\n[{marker}]"
    else:
        source_context = marker
    return resolved_agent, source_context

def _get_db_path() -> str:
    return os.environ.get("SUMMONAI_MEMORY_DB", DEFAULT_DB_PATH)


def get_db() -> sqlite3.Connection:
    """Get a database connection with WAL mode enabled."""
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
            memory_id INTEGER PRIMARY KEY,
            embedding float[512]
        )
        """
    )
    return conn


def _load_migration_files() -> list[tuple[int, Path]]:
    files: list[tuple[int, Path]] = []
    if not MIGRATIONS_DIR.exists():
        return files
    for path in MIGRATIONS_DIR.iterdir():
        if not path.is_file():
            continue
        m = MIGRATION_PATTERN.match(path.name)
        if not m:
            continue
        files.append((int(m.group("version")), path))
    files.sort(key=lambda x: x[0])
    return files


def _iter_sql_statements(sql_script: str):
    buffer = ""
    for line in sql_script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement:
                yield statement
    trailing = buffer.strip()
    if trailing:
        yield trailing


def _is_duplicate_column_error(exc: sqlite3.OperationalError, statement: str) -> bool:
    err = str(exc).lower()
    if "duplicate column name" not in err:
        return False
    lines = [l for l in statement.strip().splitlines() if not l.strip().startswith("--")]
    cleaned = " ".join(" ".join(lines).lower().split())
    return cleaned.startswith("alter table") and " add column " in cleaned


def _ensure_schema_versions_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_versions (
            version INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
            checksum TEXT NOT NULL
        )
        """
    )


def _fetch_applied_migrations(conn: sqlite3.Connection) -> dict[int, dict[str, str]]:
    rows = conn.execute(
        "SELECT version, filename, checksum FROM schema_versions ORDER BY version"
    ).fetchall()
    return {
        int(row["version"]): {
            "filename": row["filename"],
            "checksum": row["checksum"],
        }
        for row in rows
    }


def _apply_migration(
    conn: sqlite3.Connection,
    version: int,
    migration_file: Path,
    checksum: str,
) -> None:
    script = migration_file.read_text(encoding="utf-8")
    conn.execute("BEGIN")
    try:
        for statement in _iter_sql_statements(script):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as exc:
                if _is_duplicate_column_error(exc, statement):
                    continue
                raise
        conn.execute(
            """
            INSERT INTO schema_versions(version, filename, checksum)
            VALUES (?, ?, ?)
            """,
            (version, migration_file.name, checksum),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def ensure_schema():
    """Apply versioned schema migrations."""
    migrations = _load_migration_files()
    if not migrations:
        print(f"No migration files found in: {MIGRATIONS_DIR}", file=sys.stderr)
        return

    conn = get_db()
    try:
        _ensure_schema_versions_table(conn)
        applied = _fetch_applied_migrations(conn)

        for version, migration_file in migrations:
            checksum = hashlib.sha256(
                migration_file.read_bytes()
            ).hexdigest()
            applied_info = applied.get(version)
            if applied_info is not None:
                if applied_info["checksum"] != checksum:
                    raise RuntimeError(
                        f"Migration checksum mismatch for v{version:03d}: "
                        f"{migration_file.name}"
                    )
                continue
            _apply_migration(conn, version, migration_file, checksum)
    finally:
        conn.close()


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            return parsed.replace(tzinfo=local_tz)
        return parsed
    except ValueError:
        return None


def _retention_curve(age_days: float) -> float:
    if age_days <= 1:
        return 1.00
    if age_days <= 3:
        return 0.70
    if age_days <= 7:
        return 0.45
    if age_days <= 14:
        return 0.22
    if age_days <= 30:
        return 0.10
    return 0.03


def _compute_retention_score(age_days: float, recall_count: int) -> float:
    recall_boost = 1.0 + 0.12 * math.log1p(max(0, recall_count))
    return round(_retention_curve(age_days) * recall_boost, 6)


def _archive_stale_conversation_chunks(
    conn: sqlite3.Connection,
    now: datetime,
) -> None:
    # Archives chunks older than 31 days. retention_score writes are intentionally
    # omitted here — conversation_chunks are ordered by recency, not retention.
    now_iso = now.isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT id, ended_at
        FROM conversation_chunks
        WHERE archived_at IS NULL
        """
    ).fetchall()
    for row in rows:
        ended_at = _parse_iso_datetime(row["ended_at"])
        if ended_at is None:
            continue
        age_days = max(0.0, (now - ended_at).total_seconds() / 86400.0)
        if age_days > 31:
            conn.execute(
                "UPDATE conversation_chunks SET archived_at = ? WHERE id = ?",
                (now_iso, row["id"]),
            )


def _estimate_tokens(text: str) -> int:
    # Rough Japanese/English mixed estimate for lightweight stats.
    return max(1, len(text) // 4) if text else 0


def _resolve_chunk_summary(summary: str | None, content: str | None) -> str:
    summary_text = (summary or "").strip()
    if summary_text:
        return summary_text
    content_text = (content or "").strip()
    if not content_text:
        return ""
    return content_text[:_SUMMARY_FALLBACK_CHARS]


def _single_line_text(text: str | None) -> str:
    if not text:
        return ""
    # Keep DB content intact; normalize only the tool response payload.
    return re.sub(r"\s*\n\s*", " ", text).strip()


def _normalize_transcript_turns(transcript: str) -> list[dict]:
    """Normalize transcript text into turns: [{'role': str, 'content': str}]."""
    turns: list[dict] = []

    raw = transcript.strip()
    if not raw:
        return turns

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role", "unknown")).strip() or "unknown"
                content = str(item.get("content", "")).strip()
                if content:
                    turns.append({"role": role, "content": content})
            if turns:
                return turns
    except json.JSONDecodeError:
        pass

    current_role = None
    buffer: list[str] = []
    role_prefix = re.compile(r"^(user|assistant|system|tool)\s*:\s*(.*)$", re.IGNORECASE)

    def flush():
        if buffer:
            content = "\n".join(buffer).strip()
            if content:
                turns.append({"role": current_role or "unknown", "content": content})

    for line in raw.splitlines():
        m = role_prefix.match(line.strip())
        if m:
            flush()
            current_role = m.group(1).lower()
            buffer = [m.group(2)]
            continue
        buffer.append(line)
    flush()

    if turns:
        return turns

    paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
    return [{"role": "unknown", "content": p} for p in paragraphs]


def _chunk_conversation(turns: list[dict], min_turns: int = 4, max_turns: int = 8,
                        min_chars: int = 1000, max_chars: int = 1500) -> list[dict]:
    """Rule-based chunking: 4-8 turns or 1000-1500 chars."""
    chunks: list[dict] = []
    if not turns:
        return chunks

    current: list[dict] = []
    char_count = 0
    for turn in turns:
        content = turn.get("content", "")
        if not content:
            continue
        current.append(turn)
        char_count += len(content)

        reached_turn_limit = len(current) >= max_turns
        reached_soft_target = len(current) >= min_turns and char_count >= min_chars
        reached_char_cap = char_count >= max_chars
        if reached_turn_limit or reached_soft_target or reached_char_cap:
            chunks.append({"turns": current})
            current = []
            char_count = 0

    if current:
        if chunks and len(current) < min_turns:
            chunks[-1]["turns"].extend(current)
        else:
            chunks.append({"turns": current})

    for chunk in chunks:
        lines = [f"{t['role']}: {t['content']}" for t in chunk["turns"]]
        content = "\n".join(lines).strip()
        chunk["content"] = content
        chunk["summary"] = lines[0][:160] if lines else ""
        chunk["message_count"] = len(chunk["turns"])
        chunk["token_estimate"] = _estimate_tokens(content)
        chunk["hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return chunks



def _normalize_bucket(bucket: str | None, default: str = "knowledge") -> str:
    value = (bucket or "").strip().lower()
    if value in _MEMORY_BUCKETS:
        return value
    return default


def _save_memory_with_conn(
    conn: sqlite3.Connection,
    *,
    content: str,
    memory_type: str = "semantic",
    memory_bucket: str = "knowledge",
    category: str | None = None,
    importance: int = 5,
    confidence: float | None = None,
    emotional_impact: float = 0.0,
    source_context: str | None = None,
    source_agent: str | None = None,
    source_cmd: str | None = None,
    tags_csv: str | None = None,
) -> tuple[int, int]:
    normalized_bucket = _normalize_bucket(memory_bucket)
    resolved_confidence = _bucket_default_confidence(normalized_bucket) if confidence is None else _clamp(confidence, 0.0, 1.0)
    cursor = conn.execute(
        """
        INSERT INTO memories
            (memory_type, memory_bucket, category, content, source_context, source_agent,
             source_cmd, importance, confidence, emotional_impact)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_type,
            normalized_bucket,
            category,
            content,
            source_context,
            source_agent,
            source_cmd,
            importance,
            resolved_confidence,
            emotional_impact,
        ),
    )
    memory_id = int(cursor.lastrowid)
    embedding = _embed_model.encode(content)
    try:
        conn.execute(
            "INSERT INTO memories_vec (memory_id, embedding) VALUES (?, ?)",
            (memory_id, embedding.tobytes()),
        )
    except sqlite3.OperationalError:
        pass

    deduped_tags: list[str] = []
    if tags_csv:
        deduped_tags = sorted(
            {
                tag.strip()
                for tag in tags_csv.split(",")
                if tag.strip() and tag.strip() != normalized_bucket
            }
        )
    for tag in deduped_tags:
        conn.execute(
            "INSERT OR IGNORE INTO tags (memory_id, tag) VALUES (?, ?)",
            (memory_id, tag),
        )

    refreshed_count = 0
    if embedding is not None:
        refreshed_count = _associative_refresh(conn, memory_id, embedding.tolist())
        _auto_link_semantic_similarity(
            conn,
            memory_id=memory_id,
            content=content,
            embedding=embedding.tolist(),
        )

    return memory_id, refreshed_count


def _format_memory_result(row: sqlite3.Row, tags: list[str]) -> dict:
    """Return minimal memory payload for MCP response."""
    return {
        "id": row["id"],
        "content": _single_line_text(row["content"]),
        "tags": tags,
    }


def normalize_datetime_filter(value: str | None, field_name: str) -> str | None:
    """Normalize datetime filter input (ISO 8601). Date-only gets T00:00:00."""
    if value is None:
        return None

    raw = value.strip()
    if not raw:
        return None

    if len(raw) == 10:
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"{field_name} must be ISO 8601 datetime/date: {value}") from exc
        return f"{raw}T00:00:00"

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO 8601 datetime/date: {value}") from exc
    return dt.isoformat()


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(float(value), max_value))


def _bucket_default_confidence(bucket: str) -> float:
    return _BUCKET_DEFAULT_CONFIDENCE.get(_normalize_bucket(bucket), 0.80)


def _memory_recency_score(last_accessed_at: str | None, created_at: str | None, now: datetime) -> float:
    ts = _parse_iso_datetime(last_accessed_at) or _parse_iso_datetime(created_at)
    if ts is None:
        return 0.05
    age_days = (now - ts).total_seconds() / 86400.0
    if age_days <= 3:
        return 1.00
    if age_days <= 14:
        return 0.70
    if age_days <= 45:
        return 0.45
    if age_days <= 180:
        return 0.20
    return 0.05


def _memory_validity_score(
    valid_from: str | None,
    valid_until: str | None,
    now: datetime,
    include_future: bool,
) -> float:
    valid_until_dt = _parse_iso_datetime(valid_until)
    if valid_until_dt is not None and valid_until_dt < now:
        return 0.0
    valid_from_dt = _parse_iso_datetime(valid_from)
    if valid_from_dt is not None and valid_from_dt > now:
        return 0.35 if include_future else 0.0
    return 1.0


def _memory_ranked_score(row: sqlite3.Row) -> float:
    importance_norm = _clamp(float(row["importance"] or 0) / 10.0, 0.0, 1.0)
    confidence_norm = _clamp(float(row["confidence"] or 0.0), 0.0, 1.0)
    access_norm = _clamp(math.log1p(max(int(row["access_count"] or 0), 0)) / math.log(11), 0.0, 1.0)
    recall_norm = _clamp(math.log1p(max(int(row["recall_count"] or 0), 0)) / math.log(11), 0.0, 1.0)
    return (
        0.35 * importance_norm
        + 0.25 * confidence_norm
        + 0.20 * access_norm
        + 0.20 * recall_norm
    )


def _memory_impact_score(row: sqlite3.Row) -> float:
    return _clamp(abs(float(row["emotional_impact"] or 0.0)) / 10.0, 0.0, 1.0)


def _compute_rank_multiplier(
    row: sqlite3.Row,
    now: datetime,
    include_future: bool = True,
) -> float:
    validity_score = _memory_validity_score(
        row["valid_from"] if "valid_from" in row.keys() else None,
        row["valid_until"] if "valid_until" in row.keys() else None,
        now,
        include_future=include_future,
    )
    recency_score = _memory_recency_score(
        row["last_accessed_at"] if "last_accessed_at" in row.keys() else None,
        row["created_at"] if "created_at" in row.keys() else None,
        now,
    )
    temporal_score = 0.7 * validity_score + 0.3 * recency_score
    ranked_score = _memory_ranked_score(row)
    impact_score = _memory_impact_score(row)
    return 1 + 0.30 * ranked_score + 0.18 * temporal_score + 0.12 * impact_score


def _normalize_relation_type(relation_type: str) -> str:
    normalized = (relation_type or "").strip().lower()
    if normalized not in _MEMORY_LINK_RELATION_TYPES:
        allowed = ", ".join(sorted(_MEMORY_LINK_RELATION_TYPES))
        raise ValueError(f"relation_type must be one of: {allowed}")
    return normalized


def _create_memory_link(
    conn: sqlite3.Connection,
    *,
    source_memory_id: int,
    target_memory_id: int,
    relation_type: str,
    strength: float,
    source: str = "manual",
    note: str | None = None,
) -> bool:
    if source_memory_id == target_memory_id:
        return False
    normalized_relation = _normalize_relation_type(relation_type)
    bounded_strength = max(0.0, min(float(strength), 1.0))
    try:
        cursor = conn.execute(
            """
            INSERT INTO memory_links
                (source_memory_id, target_memory_id, relation_type, strength, source, note)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_memory_id, target_memory_id, relation_type) DO UPDATE SET
                strength = excluded.strength,
                source = excluded.source,
                note = excluded.note
            """,
            (
                source_memory_id,
                target_memory_id,
                normalized_relation,
                bounded_strength,
                source,
                note,
            ),
        )
        return cursor.rowcount > 0
    except sqlite3.OperationalError:
        # V007未適用DBではリンク追加をスキップ
        return False


def _tokenize_for_similarity(text: str) -> set[str]:
    if not text:
        return set()
    return {token for token in re.findall(r"[A-Za-z0-9_]{3,}", text.lower())}


def _auto_link_semantic_similarity(
    conn: sqlite3.Connection,
    *,
    memory_id: int,
    content: str,
    embedding: list | None,
    min_similarity: float = 0.82,
    top_n: int = 5,
) -> int:
    created = 0
    candidates: list[tuple[int, float]] = []
    distance_threshold = 1.0 - max(0.0, min(min_similarity, 1.0))

    if embedding:
        try:
            embedding_json = json.dumps(embedding)
            rows = conn.execute(
                """
                SELECT mv.memory_id, mv.distance
                FROM memories_vec mv
                WHERE mv.embedding MATCH ?
                  AND k = ?
                """,
                (embedding_json, top_n + 8),
            ).fetchall()
            for row in rows:
                candidate_id = int(row["memory_id"])
                if candidate_id == memory_id:
                    continue
                distance = float(row["distance"])
                if distance > distance_threshold:
                    continue
                candidates.append((candidate_id, max(0.0, min(1.0, 1.0 - distance))))
        except Exception:
            candidates = []

    if not candidates:
        # sqlite-vec unavailable時のみ最小フォールバック
        tokens = _tokenize_for_similarity(content)
        if tokens:
            rows = conn.execute(
                """
                SELECT id, content
                FROM memories
                WHERE id != ?
                  AND valid_until IS NULL
                ORDER BY id DESC
                LIMIT 50
                """,
                (memory_id,),
            ).fetchall()
            scored: list[tuple[int, float]] = []
            for row in rows:
                other_tokens = _tokenize_for_similarity(row["content"] or "")
                if not other_tokens:
                    continue
                overlap = len(tokens & other_tokens)
                union = len(tokens | other_tokens)
                if union == 0:
                    continue
                score = overlap / union
                if score >= 0.4:
                    scored.append((int(row["id"]), min(score, 1.0)))
            scored.sort(key=lambda x: x[1], reverse=True)
            candidates = scored[:top_n]

    seen_ids: set[int] = set()
    for target_id, similarity in candidates:
        if target_id in seen_ids:
            continue
        seen_ids.add(target_id)
        if _create_memory_link(
            conn,
            source_memory_id=memory_id,
            target_memory_id=target_id,
            relation_type="semantic_sim",
            strength=similarity,
            source="memory_save_auto",
        ):
            created += 1
    return created


def _associative_refresh(conn: sqlite3.Connection, memory_id: int, embedding: list,
                          threshold: float = 0.7, top_n: int = 10) -> int:
    """
    連想リフレッシュ: memory_save後に類似記憶のlast_accessed_atを更新する。
    直接検索されなくても、関連話題が出れば記憶が生き残る（人間の連想記憶）。

    Args:
        conn: DB connection
        memory_id: 今保存した記憶のID（除外対象）
        embedding: 今保存した記憶のembeddingベクトル
        threshold: 類似度の閾値 (default: 0.7)
        top_n: 更新する最大件数 (default: 10)

    Returns:
        更新した記憶の件数
    """
    try:
        import json
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        # sqlite-vecでベクトル検索（vec0はk=?構文必須）
        # top_n+1件取得し、自分自身はPythonで除外する
        embedding_json = json.dumps(embedding)

        similar = conn.execute("""
            SELECT mv.memory_id, mv.distance
            FROM memories_vec mv
            WHERE mv.embedding MATCH ?
              AND k = ?
        """, (embedding_json, top_n + 1)).fetchall()

        # 自分自身を除外
        similar = [row for row in similar if row["memory_id"] != memory_id]

        # 有効な記憶のみ（valid_until IS NULL）、かつ閾値以内
        # sqlite-vecのdistanceは小さいほど近い（コサイン距離）
        # threshold=0.7 → distance < (1 - 0.7) = 0.3
        distance_threshold = 1.0 - threshold

        eligible_ids = []
        for row in similar:
            if row["distance"] <= distance_threshold:
                eligible_ids.append(row["memory_id"])
            if len(eligible_ids) >= top_n:
                break

        if not eligible_ids:
            return 0

        # last_accessed_at を更新（valid_until IS NULL の記憶のみ）
        placeholders = ",".join("?" * len(eligible_ids))
        result = conn.execute(f"""
            UPDATE memories
            SET last_accessed_at = ?
            WHERE id IN ({placeholders})
              AND valid_until IS NULL
        """, [now_str] + eligible_ids)

        return result.rowcount
    except Exception:
        # 連想リフレッシュ失敗はmemory_saveに影響させない
        return 0


# ============================================================
# Tools
# ============================================================


@mcp.tool()
def memory_search(
    query: str,
    memory_type: str | None = None,
    tags: str | None = None,
    top_k: int = 10,
    min_importance: int = 0,
    include_invalid: bool = False,
    include_future: bool = False,
    after: str | None = None,
    before: str | None = None,
    enable_spreading: bool = True,
    spreading_min_strength: float = 0.6,
    spreading_boost: float = 0.25,
) -> str:
    """Search memories using FTS5 + vector hybrid search with TRI ranking.

    Args:
        query: Search query string (FTS5 syntax supported)
        memory_type: Filter by type: episodic, semantic, procedural, idea
        tags: Comma-separated tag filter (e.g. "policy,lesson")
        top_k: Maximum number of results (default 10)
        min_importance: Minimum importance threshold (0-10)
        include_invalid: Include memories with valid_until set
        include_future: Include memories where valid_from is in the future
        after: Include memories created_at >= this timestamp (ISO 8601)
        before: Include memories created_at <= this timestamp (ISO 8601)
        enable_spreading: Enable 1-hop spreading activation by memory_links
        spreading_min_strength: Minimum link strength to activate (0.0-1.0)
        spreading_boost: Score multiplier for propagated activation (0.0-2.0)
    """
    conn = get_db()
    try:
        now = datetime.now(timezone.utc)
        tag_filter = {t.strip() for t in tags.split(",")} if tags else set()
        after_filter = normalize_datetime_filter(after, "after")
        before_filter = normalize_datetime_filter(before, "before")
        spreading_min_strength = max(0.0, min(float(spreading_min_strength), 1.0))
        spreading_boost = max(0.0, min(float(spreading_boost), 2.0))
        row_cache: dict[int, sqlite3.Row | None] = {}
        tag_cache: dict[int, set[str]] = {}

        def _fetch_memory(memory_id: int):
            if memory_id not in row_cache:
                row_cache[memory_id] = conn.execute(
                    "SELECT * FROM memories WHERE id = ?",
                    (memory_id,),
                ).fetchone()
            return row_cache[memory_id]

        def _fetch_tags(memory_id: int) -> set[str]:
            if memory_id not in tag_cache:
                rows = conn.execute(
                    "SELECT tag FROM tags WHERE memory_id = ?",
                    (memory_id,),
                ).fetchall()
                tag_cache[memory_id] = {t["tag"] for t in rows}
            return tag_cache[memory_id]

        def _is_eligible(row: sqlite3.Row) -> bool:
            if row is None:
                return False
            if memory_type and row["memory_type"] != memory_type:
                return False
            if min_importance > 0 and row["importance"] < min_importance:
                return False
            if after_filter and row["created_at"] < after_filter:
                return False
            if before_filter and row["created_at"] > before_filter:
                return False
            if tag_filter and not (_fetch_tags(int(row["id"])) & tag_filter):
                return False
            validity_score = _memory_validity_score(
                row["valid_from"] if "valid_from" in row.keys() else None,
                row["valid_until"],
                now,
                include_future=include_future,
            )
            if validity_score <= 0.0 and not include_invalid:
                return False
            return True

        def _fts5_search(limit: int) -> list[int]:
            params: list = []
            use_fts = len(query) >= 3
            like_pat = f"%{query}%"
            if use_fts:
                sql = """
                    SELECT m.id, rank
                    FROM memories_fts f
                    JOIN memories m ON m.id = f.rowid
                    WHERE (
                        memories_fts MATCH ?
                        OR EXISTS (
                            SELECT 1 FROM tags t
                            WHERE t.memory_id = m.id AND t.tag LIKE ?
                        )
                    )
                """
                params.extend([query, like_pat])
            else:
                sql = """
                    SELECT m.id, 0 as rank
                    FROM memories m
                    WHERE (
                        m.content LIKE ? OR m.source_context LIKE ? OR m.category LIKE ?
                        OR EXISTS (
                            SELECT 1 FROM tags t
                            WHERE t.memory_id = m.id AND t.tag LIKE ?
                        )
                    )
                """
                params.extend([like_pat, like_pat, like_pat, like_pat])

            if not include_invalid:
                sql += " AND (m.valid_until IS NULL OR datetime(m.valid_until) >= datetime('now'))"
            if not include_future:
                sql += " AND datetime(m.valid_from) <= datetime('now')"
            if memory_type:
                sql += " AND m.memory_type = ?"
                params.append(memory_type)
            if min_importance > 0:
                sql += " AND m.importance >= ?"
                params.append(min_importance)
            if after_filter:
                sql += " AND m.created_at >= ?"
                params.append(after_filter)
            if before_filter:
                sql += " AND m.created_at <= ?"
                params.append(before_filter)
            sql += " ORDER BY rank LIMIT ?" if use_fts else " ORDER BY m.importance DESC LIMIT ?"
            params.append(limit)

            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                sql = """
                    SELECT m.id, 0 as rank
                    FROM memories m
                    WHERE (
                        m.content LIKE ? OR m.source_context LIKE ? OR m.category LIKE ?
                        OR EXISTS (
                            SELECT 1 FROM tags t
                            WHERE t.memory_id = m.id AND t.tag LIKE ?
                        )
                    )
                """
                params = [like_pat, like_pat, like_pat, like_pat]
                if not include_invalid:
                    sql += " AND (m.valid_until IS NULL OR datetime(m.valid_until) >= datetime('now'))"
                if not include_future:
                    sql += " AND datetime(m.valid_from) <= datetime('now')"
                if memory_type:
                    sql += " AND m.memory_type = ?"
                    params.append(memory_type)
                if min_importance > 0:
                    sql += " AND m.importance >= ?"
                    params.append(min_importance)
                if after_filter:
                    sql += " AND m.created_at >= ?"
                    params.append(after_filter)
                if before_filter:
                    sql += " AND m.created_at <= ?"
                    params.append(before_filter)
                sql += " ORDER BY m.importance DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
            return [row["id"] for row in rows]

        fts_ids = _fts5_search(limit=top_k * 4)

        vec_ids: list[int] = []
        try:
            query_emb = _embed_model.encode(query).tobytes()
            vec_rows = conn.execute(
                """
                SELECT memory_id, distance
                FROM memories_vec
                WHERE embedding MATCH ? AND k = ?
                """,
                (query_emb, top_k * 3),
            ).fetchall()
            vec_ids = [row["memory_id"] for row in vec_rows]
        except sqlite3.OperationalError:
            vec_ids = []

        if not fts_ids and not vec_ids:
            like_pat = f"%{query}%"
            fallback_rows = conn.execute(
                """
                SELECT DISTINCT m.id
                FROM memories m
                LEFT JOIN tags t ON t.memory_id = m.id
                WHERE (
                    m.content LIKE ?
                    OR m.source_context LIKE ?
                    OR m.category LIKE ?
                    OR t.tag LIKE ?
                )
                ORDER BY m.importance DESC, m.created_at DESC
                LIMIT ?
                """,
                (like_pat, like_pat, like_pat, like_pat, top_k * 6),
            ).fetchall()
            fts_ids = [int(row["id"]) for row in fallback_rows]

        k_rrf = 60
        lexical_scores: dict[int, float] = {}
        semantic_scores: dict[int, float] = {}
        for rank, memory_id in enumerate(fts_ids):
            lexical_scores[memory_id] = max(
                lexical_scores.get(memory_id, 0.0),
                1.0 / (k_rrf + rank + 1),
            )
        for rank, memory_id in enumerate(vec_ids):
            semantic_scores[memory_id] = max(
                semantic_scores.get(memory_id, 0.0),
                1.0 / (k_rrf + rank + 1),
            )

        candidate_ids = set(lexical_scores.keys()) | set(semantic_scores.keys())
        if not candidate_ids:
            return f"No memories found for query: {query}"

        tri_core_scores: dict[int, float] = {}
        base_scores: dict[int, float] = {}
        ranked_candidates = sorted(
            candidate_ids,
            key=lambda memory_id: (0.55 * lexical_scores.get(memory_id, 0.0) + 0.45 * semantic_scores.get(memory_id, 0.0)),
            reverse=True,
        )
        for memory_id in ranked_candidates[: top_k * 6]:
            row = _fetch_memory(memory_id)
            if not _is_eligible(row):
                continue
            lexical_score = lexical_scores.get(memory_id, 0.0)
            semantic_score = semantic_scores.get(memory_id, 0.0)
            base_score = 0.55 * lexical_score + 0.45 * semantic_score
            rank_multiplier = _compute_rank_multiplier(
                row,
                now,
                include_future=include_future,
            )
            bucket_multiplier = _BUCKET_MULTIPLIER.get(row["memory_bucket"] or "knowledge", 1.0)
            base_scores[memory_id] = base_score
            tri_core_scores[memory_id] = base_score * rank_multiplier * bucket_multiplier

        if not tri_core_scores:
            return f"No memories found for query: {query}"

        link_bonus: dict[int, float] = {}
        if enable_spreading and spreading_boost > 0:
            source_ids = list(tri_core_scores.keys())
            if source_ids:
                placeholders = ",".join("?" * len(source_ids))
                try:
                    link_rows = conn.execute(
                        f"""
                        SELECT source_memory_id, target_memory_id, strength
                        FROM memory_links
                        WHERE source_memory_id IN ({placeholders})
                          AND strength >= ?
                        """,
                        [*source_ids, spreading_min_strength],
                    ).fetchall()
                except sqlite3.OperationalError:
                    link_rows = []
                for link in link_rows:
                    source_id = int(link["source_memory_id"])
                    target_id = int(link["target_memory_id"])
                    strength = float(link["strength"] or 0.0)
                    raw_bonus = base_scores.get(source_id, 0.0) * strength * spreading_boost
                    if raw_bonus <= 0:
                        continue
                    row = _fetch_memory(target_id)
                    if not _is_eligible(row):
                        continue
                    if target_id not in tri_core_scores:
                        lexical_score = lexical_scores.get(target_id, 0.0)
                        semantic_score = semantic_scores.get(target_id, 0.0)
                        base_score = 0.55 * lexical_score + 0.45 * semantic_score
                        rank_multiplier = _compute_rank_multiplier(
                            row,
                            now,
                            include_future=include_future,
                        )
                        bucket_multiplier = _BUCKET_MULTIPLIER.get(row["memory_bucket"] or "knowledge", 1.0)
                        base_scores[target_id] = base_score
                        tri_core_scores[target_id] = base_score * rank_multiplier * bucket_multiplier
                    link_bonus[target_id] = min(0.08, link_bonus.get(target_id, 0.0) + raw_bonus)

        results = []
        final_scores = {
            memory_id: tri_core_scores[memory_id] + link_bonus.get(memory_id, 0.0)
            for memory_id in tri_core_scores
        }
        for memory_id, _score in sorted(final_scores.items(), key=lambda x: x[1], reverse=True)[:top_k]:
            row = _fetch_memory(memory_id)
            if row is None:
                continue
            result = _format_memory_result(row, sorted(_fetch_tags(memory_id)))
            results.append(result)

        for result in results:
            conn.execute(
                """
                UPDATE memories
                SET access_count = access_count + 1,
                    recall_count = recall_count + 1,
                    last_accessed_at = ?
                WHERE id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), result["id"]),
            )
        conn.commit()

        import json

        return json.dumps(results, ensure_ascii=False, indent=2)
    finally:
        conn.close()


@mcp.tool()
def memory_save(
    content: str,
    memory_type: str = "semantic",
    memory_bucket: str = "knowledge",
    category: str | None = None,
    importance: int = 5,
    confidence: float | None = None,
    emotional_impact: float = 0.0,
    source_context: str | None = None,
    source_agent: str | None = None,
    source_cmd: str | None = None,
    tags_csv: str | None = None,
) -> str:
    """Save a new memory to the database.

    Args:
        content: The memory text content
        memory_type: One of: episodic, semantic, procedural, idea
        memory_bucket: One of: code, knowledge, content
        category: Category string (e.g. 'policy', 'lesson')
        importance: Importance score 1-10 (default 5)
        confidence: Confidence score 0.0-1.0 (bucket default if omitted)
        emotional_impact: Emotional impact -10.0 to 10.0 (default 0.0)
        source_context: Surrounding context of the original statement
        source_agent: Agent that recorded this (default, coordinator, analyst, etc.)
        source_cmd: Related cmd_id
        tags_csv: Comma-separated tags (e.g. "policy,lesson")
    """
    source_agent, source_context = _runtime_source_metadata(source_agent, source_context)
    conn = get_db()
    try:
        memory_id, refreshed_count = _save_memory_with_conn(
            conn,
            content=content,
            memory_type=memory_type,
            memory_bucket=memory_bucket,
            category=category,
            importance=importance,
            confidence=confidence,
            emotional_impact=emotional_impact,
            source_context=source_context,
            source_agent=source_agent,
            source_cmd=source_cmd,
            tags_csv=tags_csv,
        )
        conn.commit()
        return f"Memory saved with id={memory_id}\nrefreshed_count: {refreshed_count}"
    finally:
        conn.close()


@mcp.tool()
def memory_link_add(
    source_memory_id: int,
    target_memory_id: int,
    relation_type: str,
    strength: float = 0.8,
    note: str | None = None,
) -> str:
    """Create/update a memory link.

    supports/contradicts relations are intended for manual curation.
    """
    normalized_relation = _normalize_relation_type(relation_type)
    conn = get_db()
    try:
        source_row = conn.execute("SELECT id FROM memories WHERE id = ?", (source_memory_id,)).fetchone()
        target_row = conn.execute("SELECT id FROM memories WHERE id = ?", (target_memory_id,)).fetchone()
        if source_row is None or target_row is None:
            return "source_memory_id or target_memory_id not found"
        if normalized_relation in {"supports", "contradicts"} and note is None:
            note = "manual_link"
        created = _create_memory_link(
            conn,
            source_memory_id=source_memory_id,
            target_memory_id=target_memory_id,
            relation_type=normalized_relation,
            strength=strength,
            source="manual",
            note=note,
        )
        conn.commit()
        if created:
            return (
                "memory_link_add ok: "
                f"{source_memory_id} -[{normalized_relation}:{max(0.0, min(float(strength), 1.0)):.2f}]-> "
                f"{target_memory_id}"
            )
        return "memory_link_add skipped"
    finally:
        conn.close()


@mcp.tool()
def memory_invalidate(
    memory_id: int,
    reason: str | None = None,
) -> str:
    """Invalidate a memory by setting valid_until to now.

    Args:
        memory_id: Target memory ID
        reason: Optional invalidation reason (stored in source_context)
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, source_context, valid_until FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if not row:
            return f"Memory id={memory_id} not found"

        now_iso = datetime.now(timezone.utc).isoformat()

        if reason:
            existing_context = row["source_context"] or ""
            reason_entry = f"[invalidated {now_iso}] {reason}"
            new_context = (
                f"{existing_context}\n{reason_entry}" if existing_context else reason_entry
            )
            conn.execute(
                "UPDATE memories SET valid_until = ?, source_context = ? WHERE id = ?",
                (now_iso, new_context, memory_id),
            )
        else:
            conn.execute(
                "UPDATE memories SET valid_until = ? WHERE id = ?",
                (now_iso, memory_id),
            )

        conn.commit()
        return f"Memory id={memory_id} invalidated at {now_iso}"
    finally:
        conn.close()


@mcp.tool()
def memory_stats() -> str:
    """Get statistics about the memory database."""
    conn = get_db()
    try:
        stats = {}

        # Total memories
        row = conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()
        stats["total_memories"] = row["cnt"]

        # By type
        rows = conn.execute(
            "SELECT memory_type, COUNT(*) as cnt FROM memories GROUP BY memory_type"
        ).fetchall()
        stats["by_type"] = {r["memory_type"]: r["cnt"] for r in rows}

        # Valid vs invalid
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM memories WHERE valid_until IS NULL"
        ).fetchone()
        stats["valid_memories"] = row["cnt"]
        stats["invalidated_memories"] = stats["total_memories"] - stats["valid_memories"]

        # Tags
        row = conn.execute("SELECT COUNT(DISTINCT tag) as cnt FROM tags").fetchone()
        stats["unique_tags"] = row["cnt"]

        # Top tags
        rows = conn.execute(
            "SELECT tag, COUNT(*) as cnt FROM tags GROUP BY tag ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        stats["top_tags"] = {r["tag"]: r["cnt"] for r in rows}

        # Average importance
        row = conn.execute(
            "SELECT AVG(importance) as avg_imp FROM memories WHERE valid_until IS NULL"
        ).fetchone()
        stats["avg_importance"] = round(row["avg_imp"], 2) if row["avg_imp"] else 0

        import json

        try:
            layer1_recent_chunks = conn.execute(
                """
                SELECT COUNT(*) FROM conversation_chunks
                WHERE archived_at IS NULL
                  AND datetime(ended_at) >= datetime('now', '-3 days')
                """
            ).fetchone()[0]
        except sqlite3.OperationalError:
            layer1_recent_chunks = 0

        try:
            row = conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()
            stats["total_links"] = row[0] if row else 0
            rows = conn.execute(
                """
                SELECT relation_type, COUNT(*) as cnt
                FROM memory_links
                GROUP BY relation_type
                ORDER BY cnt DESC
                """
            ).fetchall()
            stats["links_by_relation"] = {r["relation_type"]: r["cnt"] for r in rows}
        except sqlite3.OperationalError:
            stats["total_links"] = 0
            stats["links_by_relation"] = {}

        return (
            json.dumps(stats, ensure_ascii=False, indent=2)
            + "\nlayer2_decay_enabled: false"
            + "\nlayer2_decay_note: structured memories are retained without time-based decay"
            + "\nlayer1_recency_window_days_default: 3"
            + f"\nlayer1_recent_chunks_3d: {layer1_recent_chunks}"
        )
    finally:
        conn.close()


@mcp.tool()
def memory_load(
    min_importance: int = 0,
    tags: str | None = None,
    bucket: str | None = None,
    memory_type: str | None = None,
    after: str | None = None,
    before: str | None = None,
) -> str:
    """Bulk load memories for session startup with minimal payload.

    Args:
        min_importance: Minimum importance threshold (0-10, default 0)
        tags: Comma-separated tag filter (e.g. "rule,lesson")
        bucket: Filter by memory bucket: code, knowledge, content
        memory_type: Filter by type: episodic, semantic, procedural, idea
        after: Include memories created_at >= this timestamp (ISO 8601)
        before: Include memories created_at <= this timestamp (ISO 8601)
    """
    conn = get_db()
    try:
        after_filter = normalize_datetime_filter(after, "after")
        before_filter = normalize_datetime_filter(before, "before")

        sql = """
            SELECT DISTINCT m.id, m.content
            FROM memories m
        """
        params: list = []

        tag_list: list[str] = []
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            if tag_list:
                placeholders = ",".join("?" * len(tag_list))
                sql += f"""
                    JOIN tags t ON m.id = t.memory_id
                    WHERE t.tag IN ({placeholders})
                """
                params.extend(tag_list)
                sql += " AND m.valid_until IS NULL"
            else:
                sql += " WHERE m.valid_until IS NULL"
        else:
            sql += " WHERE m.valid_until IS NULL"

        if min_importance > 0:
            sql += " AND m.importance >= ?"
            params.append(min_importance)
        normalized_bucket = _normalize_bucket(bucket, default="")
        if normalized_bucket:
            sql += " AND m.memory_bucket = ?"
            params.append(normalized_bucket)
        if memory_type:
            sql += " AND m.memory_type = ?"
            params.append(memory_type)
        if after_filter:
            sql += " AND m.created_at >= ?"
            params.append(after_filter)
        if before_filter:
            sql += " AND m.created_at <= ?"
            params.append(before_filter)

        sql += " ORDER BY m.importance DESC, m.created_at DESC"

        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return "（該当する記憶なし）"
        results = []
        for row in rows:
            tag_rows = conn.execute(
                "SELECT tag FROM tags WHERE memory_id = ? ORDER BY tag ASC",
                (row["id"],),
            ).fetchall()
            tags_list = [tag_row["tag"] for tag_row in tag_rows]
            results.append(
                {
                    "id": row["id"],
                    "content": _single_line_text(row["content"]),
                    "tags": tags_list,
                }
            )

        return json.dumps(results, ensure_ascii=False, indent=2)
    finally:
        conn.close()


@mcp.tool()
def conversation_save(
    session_id: str,
    agent_id: str,
    project: str | None,
    task_id: str | None,
    transcript: str,
    ended_at: str,
    scope_type: str | None = None,
    scope_id: str | None = None,
) -> str:
    """Save conversation transcript as rule-based chunks (idempotent by chunk hash).

    Args:
        session_id: Unique conversation/session identifier
        agent_id: Agent name (e.g. default, coordinator, worker4)
        project: Project identifier (optional)
        task_id: Related task/cmd identifier (optional)
        transcript: Full transcript (JSON turns or plain text)
        ended_at: Session end timestamp (ISO 8601)
    """
    ended_at_norm = normalize_datetime_filter(ended_at, "ended_at")
    if not ended_at_norm:
        raise ValueError("ended_at is required")
    if not session_id.strip():
        raise ValueError("session_id is required")
    if not agent_id.strip():
        raise ValueError("agent_id is required")

    turns = _normalize_transcript_turns(transcript)
    if not turns:
        return "conversation_save skipped: transcript empty"

    chunks = _chunk_conversation(turns)
    if not chunks:
        return "conversation_save skipped: no chunks generated"

    started_at = ended_at_norm
    message_count = len(turns)
    token_estimate = _estimate_tokens(transcript)
    summary = chunks[0]["summary"][:200] if chunks else None

    normalized_scope_type = (scope_type or "").strip().lower()
    if normalized_scope_type not in {"user", "project"}:
        normalized_scope_type = "project" if project else "user"
    normalized_scope_id = (scope_id or "").strip() or (project or "").strip() or "global"

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO conversation_sessions
                (session_id, agent_id, project, task_id, started_at, ended_at,
                 message_count, token_estimate, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                agent_id=excluded.agent_id,
                project=excluded.project,
                task_id=excluded.task_id,
                ended_at=excluded.ended_at,
                message_count=excluded.message_count,
                token_estimate=excluded.token_estimate,
                summary=excluded.summary
            """,
            (
                session_id,
                agent_id,
                project,
                task_id,
                started_at,
                ended_at_norm,
                message_count,
                token_estimate,
                summary,
            ),
        )

        existing_count = conn.execute(
            "SELECT COUNT(*) FROM conversation_chunks WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        replaced = int(existing_count or 0)
        if replaced > 0:
            conn.execute(
                "DELETE FROM conversation_chunks WHERE session_id = ?",
                (session_id,),
            )

        inserted = 0
        deduped = 0
        seen_hashes: set[str] = set()
        for chunk in chunks:
            chunk_hash = chunk["hash"]
            if chunk_hash in seen_hashes:
                deduped += 1
                continue
            seen_hashes.add(chunk_hash)
            cursor = conn.execute(
                """
                INSERT INTO conversation_chunks
                    (session_id, chunk_index, chunk_hash, agent_id, project, task_id,
                     started_at, ended_at, message_count, token_estimate, summary, content,
                     scope_type, scope_id,
                     retention_score, recall_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    inserted,
                    chunk_hash,
                    agent_id,
                    project,
                    task_id,
                    started_at,
                    ended_at_norm,
                    chunk["message_count"],
                    chunk["token_estimate"],
                    chunk["summary"],
                    chunk["content"],
                    normalized_scope_type,
                    normalized_scope_id,
                    1.0,
                    0,
                ),
            )
            if cursor.rowcount == 1:
                inserted += 1

        conn.commit()
        return (
            f"conversation_save ok: session_id={session_id}, turns={message_count}, "
            f"chunks_total={len(chunks)}, replaced={replaced}, inserted={inserted}, deduped={deduped}"
        )
    finally:
        conn.close()


@mcp.tool()
def conversation_load_recent(
    agent_id: str,
    project: str | None = None,
    scope_type: str | None = None,
    scope_id: str | None = None,
    limit_chunks: int = 6,
    since_days: int = 3,
) -> str:
    """Load recent conversation chunks by agent/project and recency window."""
    if not agent_id.strip():
        raise ValueError("agent_id is required")

    limit_chunks = max(1, min(limit_chunks, 50))
    since_days = max(1, min(since_days, 365))

    conn = get_db()
    try:
        now = datetime.now(timezone.utc)
        _archive_stale_conversation_chunks(conn, now)

        sql = """
            SELECT
                c.id,
                c.session_id,
                c.chunk_index,
                c.summary,
                c.content,
                c.message_count,
                c.token_estimate,
                c.project,
                c.task_id,
                c.scope_type,
                c.scope_id,
                c.started_at,
                c.ended_at,
                c.created_at,
                c.consolidated_at
            FROM conversation_chunks c
            WHERE c.agent_id = ?
              AND c.archived_at IS NULL
              AND datetime(c.ended_at) >= datetime('now', ?)
        """
        params: list = [agent_id, f"-{since_days} days"]
        if project:
            sql += " AND c.project = ?"
            params.append(project)
        if scope_type:
            sql += " AND c.scope_type = ?"
            params.append(scope_type.strip().lower())
        if scope_id:
            sql += " AND c.scope_id = ?"
            params.append(scope_id.strip())
        sql += " ORDER BY datetime(c.ended_at) DESC, c.chunk_index DESC LIMIT ?"
        params.append(limit_chunks)

        rows = conn.execute(sql, params).fetchall()
        if not rows:
            conn.commit()
            return "[]"

        result = []
        now_iso = now.isoformat(timespec="seconds")
        for row in rows:
            ended_at = _parse_iso_datetime(row["ended_at"])
            age_days = 9999.0
            if ended_at is not None:
                age_days = max(0.0, (now - ended_at).total_seconds() / 86400.0)
            detail_level = "full" if age_days <= 1.0 else "summary"
            if ended_at is not None and age_days <= 1.0 and not row["consolidated_at"]:
                conn.execute(
                    "UPDATE conversation_chunks SET consolidated_at = ? WHERE id = ?",
                    (now_iso, row["id"]),
                )

            summary_text = _resolve_chunk_summary(row["summary"], row["content"])
            content_text = row["content"] if detail_level == "full" else summary_text

            result.append(
                {
                    "content": _single_line_text(content_text),
                    "started_at": row["started_at"],
                }
            )
        conn.commit()
        return json.dumps(result, ensure_ascii=False, indent=2)
    finally:
        conn.close()


# ============================================================
# Passive Recall Socket Server
# ============================================================

_RECALL_SOCKET_PATH = Path(tempfile.gettempdir()) / socket_filename(_get_db_path(), os.getpid())
_RECALL_SIM_THRESHOLD = 0.65
_RECALL_TOP_K = 3
_RECALL_TOKEN_BUDGET = 500
_RECALL_SEARCH_K = 20
_RECALL_CONN_TIMEOUT = 2.0


def _recall_search(prompt_text: str) -> list[tuple[int, str, float]]:
    """Search memories_vec for relevant memories using cosine similarity."""
    conn = get_db()
    try:
        emb = _embed_model.encode(prompt_text)
        emb_norm = emb / np.linalg.norm(emb)
        now = datetime.now(timezone.utc)

        # KNN by L2 distance to get candidate IDs (vec0 default metric)
        rows = conn.execute(
            """
            SELECT mv.memory_id
            FROM memories_vec mv
            JOIN memories m ON m.id = mv.memory_id
            WHERE mv.embedding MATCH ? AND k = ?
              AND m.memory_bucket IN ('knowledge', 'content')
              AND m.valid_until IS NULL
            """,
            (emb.tobytes(), _RECALL_SEARCH_K),
        ).fetchall()

        # Compute cosine similarity manually (stored vecs are not normalized)
        candidates: list[tuple[int, float, float]] = []
        for r in rows:
            mid = int(r["memory_id"])
            vec_row = conn.execute(
                "SELECT embedding FROM memories_vec WHERE memory_id = ?", (mid,)
            ).fetchone()
            if not vec_row:
                continue
            stored = np.frombuffer(bytes(vec_row["embedding"]), dtype=np.float32)
            stored_n = np.linalg.norm(stored)
            if stored_n == 0:
                continue
            sim = float(np.dot(emb_norm, stored / stored_n))
            if sim > _RECALL_SIM_THRESHOLD:
                memory_row = conn.execute(
                    """
                    SELECT content, importance, confidence, access_count, recall_count,
                           emotional_impact, last_accessed_at, created_at
                    FROM memories
                    WHERE id = ?
                    """,
                    (mid,),
                ).fetchone()
                if not memory_row:
                    continue
                rank_multiplier = _compute_rank_multiplier(memory_row, now)
                candidates.append((mid, sim, sim * rank_multiplier))

        candidates.sort(key=lambda x: x[2], reverse=True)
        candidates = candidates[:_RECALL_TOP_K]

        results: list[tuple[int, str, float]] = []
        total_tokens = 0
        for memory_id, similarity, _ in candidates:
            row = conn.execute(
                "SELECT content FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if not row:
                continue
            content = row["content"]
            tokens = len(content) // 4
            if total_tokens + tokens > _RECALL_TOKEN_BUDGET:
                continue
            results.append((memory_id, content, similarity))
            total_tokens += tokens
        return results
    finally:
        conn.close()


def _handle_recall_connection(conn_sock: socket.socket) -> None:
    """Handle one recall request over a Unix socket connection."""
    try:
        conn_sock.settimeout(_RECALL_CONN_TIMEOUT)
        buf = b""
        while b"\n" not in buf:
            chunk = conn_sock.recv(4096)
            if not chunk:
                return
            buf += chunk
        line = buf.split(b"\n", 1)[0]
        req = json.loads(line.decode("utf-8"))
        prompt_text = (req.get("prompt") or "").strip()
        if not prompt_text:
            conn_sock.sendall(b'{"results":[]}\n')
            return
        results = _recall_search(prompt_text)
        payload = {"results": [[mid, content, sim] for mid, content, sim in results]}
        conn_sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    except Exception:
        try:
            conn_sock.sendall(b'{"results":[]}\n')
        except Exception:
            pass
    finally:
        try:
            conn_sock.close()
        except Exception:
            pass


def _recall_socket_server_loop() -> None:
    """Background daemon thread: accept recall requests on Unix socket."""
    sock_path = str(_RECALL_SOCKET_PATH)
    try:
        _RECALL_SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(sock_path)
            server.listen(8)
            while True:
                try:
                    conn_sock, _ = server.accept()
                    t = threading.Thread(
                        target=_handle_recall_connection, args=(conn_sock,), daemon=True
                    )
                    t.start()
                except Exception:
                    continue
    except Exception:
        pass


def _cleanup_recall_socket() -> None:
    try:
        _RECALL_SOCKET_PATH.unlink()
    except Exception:
        pass


def start_recall_socket_server() -> None:
    """Start the background Unix socket recall server. Idempotent."""
    t = threading.Thread(target=_recall_socket_server_loop, daemon=True)
    t.start()
    atexit.register(_cleanup_recall_socket)


# ============================================================
# Startup
# ============================================================

# Ensure schema on import
ensure_schema()

if __name__ == "__main__":
    start_recall_socket_server()
    mcp.run(transport="stdio")
