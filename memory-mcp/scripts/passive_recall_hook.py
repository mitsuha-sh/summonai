#!/usr/bin/env python3
"""UserPromptSubmit hook: passive memory recall via Unix socket IPC.

Sends the user's prompt to the already-running memory-mcp server process
(which has ruri-v3-130m warm in memory) via a Unix socket, receives
matching memories, applies session-level dedup, and prints [RECALL] blocks.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from recall_socket import socket_glob

WINDOW_SIZE = 5       # sliding window: last N turns for dedup
TOP_K = 3             # max memories to inject
TOKEN_BUDGET = 500    # max total tokens
SIM_THRESHOLD = 0.65  # keep in sync with server.py _RECALL_SIM_THRESHOLD

SOCKET_TIMEOUT = 2.0  # seconds per socket attempt


def _memory_db_path() -> str:
    default_db = ROOT_DIR / "db" / "summonai_memory.db"
    return os.environ.get("SUMMONAI_MEMORY_DB", str(default_db))


def _estimate_tokens(text: str) -> int:
    return len(text) // 4 if text else 0


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


def _find_sockets() -> list[Path]:
    """Return socket files sorted by mtime descending (most recently active first)."""
    tmpdir = Path(tempfile.gettempdir())
    socks = list(tmpdir.glob(socket_glob(_memory_db_path())))
    socks.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return socks


def _query_server(prompt_text: str) -> list[tuple[int, str, float]]:
    """Send recall request to MCP server via Unix socket. Returns raw results."""
    socks = _find_sockets()
    if not socks:
        return []

    request = (json.dumps({"prompt": prompt_text}, ensure_ascii=False) + "\n").encode("utf-8")

    for sock_path in socks:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(SOCKET_TIMEOUT)
                s.connect(str(sock_path))
                s.sendall(request)

                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk

            line = buf.split(b"\n", 1)[0]
            data = json.loads(line.decode("utf-8"))
            results = [
                (int(r[0]), str(r[1]), float(r[2]))
                for r in data.get("results", [])
            ]
            return results
        except Exception:
            continue

    return []


def recall(prompt_text: str, session_id: str) -> list[tuple[int, str, float]]:
    """Return dedup-filtered list of (memory_id, content, similarity) for injection."""
    raw = _query_server(prompt_text)
    state_file = _state_path(session_id)
    if state_file and state_file.exists():
        state = _load_state(state_file)
    else:
        state = {"recent_ids": []}

    recent_ids: set[int] = set()
    for turn_ids in state.get("recent_ids", []):
        if isinstance(turn_ids, list):
            recent_ids.update(turn_ids)

    candidates = [(mid, content, sim) for mid, content, sim in raw if mid not in recent_ids]

    # Apply token budget
    results: list[tuple[int, str, float]] = []
    total_tokens = 0
    for memory_id, content, similarity in candidates[:TOP_K]:
        tokens = _estimate_tokens(content)
        if total_tokens + tokens > TOKEN_BUDGET:
            continue
        results.append((memory_id, content, similarity))
        total_tokens += tokens

    if state_file is not None:
        this_turn_ids = [mid for mid, _, _ in results]
        recent = state.get("recent_ids", [])
        if not isinstance(recent, list):
            recent = []
        recent.append(this_turn_ids)
        recent = recent[-WINDOW_SIZE:]
        _save_state(state_file, {"recent_ids": recent})

    return results


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

    try:
        results = recall(prompt_text, session_id)
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
