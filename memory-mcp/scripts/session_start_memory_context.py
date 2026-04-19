#!/usr/bin/env python3
"""SessionStart hook bridge: inject scope-aware memory restore instructions."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from hook_context import (
    memory_l1_save_enabled,
    pick_env,
    resolve_agent_id as resolve_configured_agent_id,
    resolve_persona_dir,
    resolve_scope,
)

_EXECUTOR_PANE_FILE_PATTERN = "summonai_pane_{key}.task_id"


def resolve_repo_dir() -> Path:
    repo_dir = os.environ.get("SUMMONAI_MEMORY_MCP_DIR", "").strip()
    if repo_dir:
        return Path(repo_dir).expanduser()
    return Path(__file__).resolve().parent.parent


def resolve_agent_id(payload: dict) -> str:
    return resolve_configured_agent_id(payload)


def _resolve_role_and_task_id() -> tuple[str, str]:
    """Returns (role, task_id).

    Detection order:
    1. ZELLIJ_PANE_ID env var -> look up /tmp/summonai_pane_terminal_{N}.task_id
    2. Fallback: SUMMONAI_ROLE / SUMMONAI_TASK_ID env vars (for tmux-based dev/testing)
    """
    pane_id = os.environ.get("ZELLIJ_PANE_ID", "").strip()
    if pane_id:
        # ZELLIJ_PANE_ID is numeric (e.g. "1"); server.py writes "terminal_1" keyed files.
        for key in (f"terminal_{pane_id}", pane_id):
            task_file = Path(tempfile.gettempdir()) / _EXECUTOR_PANE_FILE_PATTERN.format(
                key=key
            )
            if task_file.is_file():
                task_id = task_file.read_text(encoding="utf-8").strip()
                if task_id:
                    return "executor", task_id

    # Fallback for tmux-based development/testing or non-zellij environments.
    role = os.environ.get("SUMMONAI_ROLE", "").strip()
    task_id = os.environ.get("SUMMONAI_TASK_ID", "").strip()
    return role or "interface", task_id


def emit_persona_markdown(payload: dict | None = None) -> None:
    persona_dir = resolve_persona_dir(payload or {}, resolve_repo_dir())
    for filename in ("USER.md", "SOUL.md"):
        path = persona_dir / filename
        if not path.is_file():
            print(f"[SESSION_START_PERSONA] {path} が見つからないため注入をスキップ。")
            continue
        print(f"[SESSION_START_PERSONA] ----- BEGIN {filename} -----")
        print(path.read_text(encoding="utf-8").rstrip())
        print(f"[SESSION_START_PERSONA] ----- END {filename} -----")


def emit_memory_guidelines_markdown() -> None:
    path = resolve_repo_dir() / "docs" / "memory_guidelines.md"
    if not path.is_file():
        print(f"[SESSION_START_MEMORY_GUIDE] {path} が見つからないため注入をスキップ。")
        return
    # Keep SessionStart injection short to avoid token waste.
    print(
        "[SESSION_START_MEMORY_GUIDE] "
        "memory_save時は docs/memory_guidelines.md を遵守せよ。"
        "bucket=code/knowledge/content。"
        "memory_type=episodic/semantic/procedural/idea。"
        "importance=1-10（低1-3/中4-6/高7-8/最重要9-10）。"
        "保存対象=再利用可能な教訓・決定・手順。"
        "非対象=生ログ・一時進捗・重複情報。"
    )


def resolve_summonai_dir() -> Path | None:
    summonai_dir = os.environ.get("SUMMONAI_DIR", "").strip()
    if summonai_dir:
        return Path(summonai_dir).expanduser()

    memory_repo_dir = resolve_repo_dir().resolve()
    candidates = (
        memory_repo_dir.parent,
        memory_repo_dir.parent / "summonai",
    )
    # Prefer candidates that look like summonai repo roots.
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_dir() and (resolved / "instructions").is_dir():
            return resolved

    # Fall back to any existing directory candidate.
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_dir():
            return resolved
    return None


def resolve_executor_instructions_path() -> Path | None:
    override_path = os.environ.get("SUMMONAI_EXECUTOR_INSTRUCTIONS_PATH", "").strip()
    if override_path:
        path = Path(override_path).expanduser()
        return path if path.is_file() else None

    summonai_dir = resolve_summonai_dir()
    if not summonai_dir:
        return None
    path = summonai_dir / "instructions" / "executor.md"
    return path if path.is_file() else None


def emit_executor_instructions_markdown() -> None:
    path = resolve_executor_instructions_path()
    if not path:
        print("[SESSION_START_EXECUTOR_INSTRUCTIONS] WARNING: instructions/executor.md not found, skipping injection.")
        return
    print("[SESSION_START_EXECUTOR_INSTRUCTIONS]")
    print(path.read_text(encoding="utf-8").rstrip())


def resolve_interface_instructions_path() -> Path | None:
    override_path = os.environ.get("SUMMONAI_INTERFACE_INSTRUCTIONS_PATH", "").strip()
    if override_path:
        path = Path(override_path).expanduser()
        return path if path.is_file() else None

    summonai_dir = resolve_summonai_dir()
    if not summonai_dir:
        return None
    path = summonai_dir / "instructions" / "interface.md"
    return path if path.is_file() else None


def emit_interface_instructions_markdown() -> None:
    path = resolve_interface_instructions_path()
    if not path:
        print(
            "[SESSION_START_INTERFACE_INSTRUCTIONS] "
            "instructions/interface.md が見つからないため注入をスキップ。"
        )
        return
    print("[SESSION_START_INTERFACE_INSTRUCTIONS] ----- BEGIN interface.md -----")
    print(path.read_text(encoding="utf-8").rstrip())
    print("[SESSION_START_INTERFACE_INSTRUCTIONS] ----- END interface.md -----")


def main() -> int:
    raw = sys.stdin.read().strip()
    payload: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = {}

    role, task_id_from_pane = _resolve_role_and_task_id()
    is_executor = role == "executor"

    if not is_executor:
        emit_persona_markdown(payload)
        emit_memory_guidelines_markdown()
    if not memory_l1_save_enabled(payload):
        print("[SESSION_START_MEMORY] memory_l1_save=0 のため L1/L2 memory 復元をスキップせよ。")
        return 0

    agent_id = resolve_agent_id(payload)
    scope = resolve_scope(payload)
    scope_type = scope["scope_type"] or "user"
    scope_id = scope["scope_id"] or "global"
    project = scope["project"]
    project_arg = f', project="{project}"' if project else ""

    if is_executor:
        task_id = task_id_from_pane or "<missing-task-id>"
        print(
            f"[SESSION_START_EXECUTOR_PROTOCOL] executor task_id=\"{task_id}\""
        )
        emit_executor_instructions_markdown()
        print(
            "[SESSION_START_MEMORY] "
            f"scope_type={scope_type} scope_id={scope_id}. "
            "起動直後に memory_load bucket=\"code\" を実行せよ。"
            "executor pane のため conversation_load_recent はスキップせよ。"
        )
    else:
        print(
            "[SESSION_START_MEMORY] "
            f"scope_type={scope_type} scope_id={scope_id}. "
            "起動直後に memory_load bucket=\"code\" と "
            "conversation_load_recent("
            f'agent_id="{agent_id}", limit_chunks=6, since_days=3) を実行せよ。'
            "project を指定しない場合は同一 agent_id の会話をプロジェクト横断で取得し、"
            f"絞り込みが必要な場合のみ scope_type/scope_id{project_arg} を指定せよ。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
