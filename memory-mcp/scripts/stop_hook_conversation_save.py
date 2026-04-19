#!/usr/bin/env python3
"""Stop-hook bridge: read event JSON from stdin and persist conversation chunks."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import server
from hook_context import memory_l1_save_enabled, pick, pick_env, resolve_agent_id, resolve_scope, tmux_option


def _dump_payload(raw: str, payload: dict) -> None:
    if os.environ.get("SUMMONAI_DEBUG_DUMP") != "1":
        return

    dump_path = Path("/tmp/stop_hook_payload_debug.json")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dump_ts_path = Path(f"/tmp/stop_hook_payload_debug_{timestamp}.json")
    try:
        parsed = dict(payload)
        parsed["_debug_meta"] = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "top_level_keys": sorted(payload.keys()),
        }
        text = json.dumps(parsed, ensure_ascii=False, indent=2)
        dump_path.write_text(text + "\n")
        dump_ts_path.write_text(text + "\n")
    except Exception:
        # Never fail stop-hook for debug dump errors.
        try:
            dump_path.write_text(raw + "\n")
            dump_ts_path.write_text(raw + "\n")
        except Exception:
            pass


def _extract_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                text = str(item.get("text", "")).strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return ""


def _load_transcript_from_path(path_str: str) -> list[dict]:
    path = Path(path_str).expanduser()
    if not path.exists() or not path.is_file():
        return []

    turns: list[dict] = []
    try:
        for raw_line in path.read_text().splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            message = event.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue

            # Ignore synthetic local-command and tool-result echoes.
            if role == "user" and event.get("isMeta"):
                continue
            content = message.get("content")
            if (
                role == "user"
                and isinstance(content, list)
                and content
                and all(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)
            ):
                continue

            text = _extract_text(content)
            if not text:
                continue
            turns.append({"role": role, "content": text})
    except Exception:
        return []
    return turns


def _extract_transcript(payload: dict) -> str:
    transcript = payload.get("transcript")
    if isinstance(transcript, str) and transcript.strip():
        return transcript

    messages = payload.get("messages")
    if isinstance(messages, list):
        return json.dumps(messages, ensure_ascii=False)

    transcript_path = pick(payload, "transcript_path", "transcriptPath")
    if transcript_path:
        turns = _load_transcript_from_path(transcript_path)
        if turns:
            return json.dumps(turns, ensure_ascii=False)

    last_message = payload.get("last_assistant_message")
    if isinstance(last_message, str) and last_message.strip():
        return f"assistant: {last_message.strip()}"
    return ""


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print("skip: empty stop-hook payload")
        return 0

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print("skip: payload is not JSON")
        return 0

    if not isinstance(payload, dict):
        print("skip: payload must be JSON object")
        return 0

    _dump_payload(raw, payload)

    if not memory_l1_save_enabled(payload):
        print("skip: memory_l1_save=0 in tmux pane")
        return 0

    agent_id = resolve_agent_id(payload)
    resolved_scope = resolve_scope(payload)
    project = resolved_scope["project"]
    task_id = pick_env("SUMMONAI_TASK_ID") or tmux_option("task_id")
    ended_at = pick(payload, "ended_at", "timestamp", "endedAt") or datetime.now().isoformat(timespec="seconds")
    session_id = pick(payload, "session_id", "conversation_id", "sessionId") or datetime.now().strftime(
        f"{agent_id}-stop-%Y%m%d%H%M%S"
    )

    transcript = _extract_transcript(payload)

    save_result = server.conversation_save(
        session_id=session_id,
        agent_id=agent_id,
        project=project,
        task_id=task_id,
        transcript=transcript,
        ended_at=ended_at,
        scope_type=resolved_scope["scope_type"],
        scope_id=resolved_scope["scope_id"],
    )

    print(save_result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
