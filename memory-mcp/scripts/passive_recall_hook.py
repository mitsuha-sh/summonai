#!/usr/bin/env python3
"""UserPromptSubmit hook: passive memory recall via vector similarity search.

Reads a Claude Code user_prompt_submit JSON from stdin, extracts the user's
text, and searches memories_vec (knowledge + content buckets) for relevant
memories. Matching memories are printed as [RECALL] blocks for context
injection. Empty stdout when nothing relevant is found.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

WINDOW_SIZE = 5       # sliding window: last N turns for dedup
SIM_THRESHOLD = 0.65  # cosine similarity cutoff
TOP_K = 3             # max memories to inject
TOKEN_BUDGET = 500    # max total tokens
SEARCH_K = 8          # k for vec0 KNN search
TARGET_BUCKETS = ("knowledge", "content")


def _estimate_tokens(text: str) -> int:
    return len(text) // 4 if text else 0


_embed_model = None


def _get_model():
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer("cl-nagoya/ruri-v3-130m")
    return _embed_model


def _state_path(session_id: str) -> Path | None:
    if not session_id or session_id == "unknown":
        return None
    safe = session_id[:32].replace("/", "_").replace(":", "_")
    return Path(tempfile.gettempdir()) / f"summonai_passive_recall_{safe}.json"


def _load_state(state_file: Path) -> dict:
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return {"recent_ids": data.get("recent_ids", [])}
    except Exception:
        return {"recent_ids": []}


def _save_state(state_file: Path, state: dict) -> None:
    try:
        state_file.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _cleanup_stale_state_files() -> None:
    """Remove state files older than 24 hours on each startup."""
    try:
        now = time.time()
        tmpdir = Path(tempfile.gettempdir())
        for f in tmpdir.glob("summonai_passive_recall_*.json"):
            try:
                if now - f.stat().st_mtime > 86400:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def resolve_db_path() -> str:
    env = os.environ.get("SUMMONAI_MEMORY_DB", "").strip()
    if env:
        return env
    scripts_dir = Path(__file__).resolve().parent
    repo_root = scripts_dir.parent.parent
    return str(repo_root / ".data" / "summonai_memory.db")


def recall(prompt_text: str, session_id: str, db_path: str) -> list[tuple[int, str, float]]:
    """Return list of (memory_id, content, similarity) for injection."""
    import sqlite3
    import sqlite_vec

    model = _get_model()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    try:
        emb = model.encode(prompt_text)
        rows = conn.execute(
            """
            SELECT mv.memory_id, mv.distance
            FROM memories_vec mv
            JOIN memories m ON m.id = mv.memory_id
            WHERE mv.embedding MATCH ? AND k = ?
              AND m.memory_bucket IN ('knowledge', 'content')
              AND m.valid_until IS NULL
            """,
            (emb.tobytes(), SEARCH_K),
        ).fetchall()

        candidates = sorted(
            [
                (int(r["memory_id"]), 1.0 - float(r["distance"]))
                for r in rows
                if (1.0 - float(r["distance"])) > SIM_THRESHOLD
            ],
            key=lambda x: x[1],
            reverse=True,
        )[:TOP_K]

        # Dedup: skip memory_ids seen in the last WINDOW_SIZE turns
        state_file = _state_path(session_id)
        if state_file and state_file.exists():
            state = _load_state(state_file)
        else:
            state = {"recent_ids": []}

        recent_ids: set[int] = set()
        for turn_ids in state.get("recent_ids", []):
            if isinstance(turn_ids, list):
                recent_ids.update(turn_ids)
        candidates = [(mid, sim) for mid, sim in candidates if mid not in recent_ids]

        if not candidates:
            return []

        results: list[tuple[int, str, float]] = []
        total_tokens = 0
        for memory_id, similarity in candidates:
            row = conn.execute(
                "SELECT content FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if not row:
                continue
            content = row["content"]
            tokens = _estimate_tokens(content)
            if total_tokens + tokens > TOKEN_BUDGET:
                break
            results.append((memory_id, content, similarity))
            total_tokens += tokens

        # Persist dedup state
        if results and state_file is not None:
            this_turn_ids = [mid for mid, _, _ in results]
            recent = state.get("recent_ids", [])
            if not isinstance(recent, list):
                recent = []
            recent.append(this_turn_ids)
            recent = recent[-WINDOW_SIZE:]
            _save_state(state_file, {"recent_ids": recent})

        return results

    finally:
        conn.close()


def main() -> int:
    _cleanup_stale_state_files()

    raw = sys.stdin.read().strip()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    prompt_text = (payload.get("prompt") or "").strip()
    if not prompt_text:
        return 0

    session_id = (
        payload.get("session_id")
        or payload.get("sessionId")
        or ""
    ).strip()

    db_path = resolve_db_path()

    try:
        results = recall(prompt_text, session_id, db_path)
    except Exception as e:
        print(f"[RECALL_ERROR] {e}", file=sys.stderr)
        return 0

    for memory_id, content, similarity in results:
        print(f"[RECALL] (memory_id={memory_id}, similarity={similarity:.3f})")
        print(content.strip())
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
