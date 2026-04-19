#!/usr/bin/env python3
"""Utilities for deriving stable scope/project metadata from hook payloads."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

FALSE_VALUES = {"0", "false", "off", "no"}
CONFIG_ENV = "SUMMONAI_MEMORY_CONFIG"


def pick(payload: dict, *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def pick_env(*keys: str) -> str | None:
    for key in keys:
        value = os.environ.get(key)
        if value and value.strip():
            return value.strip()
    return None


def tmux_option(option_name: str) -> str | None:
    pane = os.environ.get("TMUX_PANE")
    cmd = ["tmux", "display-message"]
    if pane:
        cmd.extend(["-t", pane])
    cmd.extend(["-p", f"#{{@{option_name}}}"])
    try:
        value = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None
    return value or None


def _find_project_config(payload: dict | None = None) -> Path | None:
    override = pick_env(CONFIG_ENV)
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None

    candidates: list[Path] = []
    project_dir = pick_env("CLAUDE_PROJECT_DIR")
    cwd = pick(payload or {}, "cwd") or pick_env("PWD")
    for raw in (project_dir, cwd):
        if not raw:
            continue
        path = Path(raw).expanduser()
        candidates.extend([path, *path.parents])

    summonai_dir = pick_env("SUMMONAI_DIR")
    if summonai_dir:
        candidates.append(Path(summonai_dir).expanduser())

    seen: set[Path] = set()
    for base in candidates:
        try:
            resolved = base.resolve()
        except Exception:
            resolved = base
        if resolved in seen:
            continue
        seen.add(resolved)
        config_path = resolved / ".summonai" / "memory.toml"
        if config_path.is_file():
            return config_path
    return None


def load_runtime_config(payload: dict | None = None) -> dict[str, str]:
    """Load project-local memory hook config from .summonai/memory.toml.

    Supported keys: agent_id, project, scope_type, scope_id, persona_dir.
    Environment variables intentionally override these values at call sites.
    """
    path = _find_project_config(payload)
    if not path or tomllib is None:
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    raw = data.get("memory", data)
    if not isinstance(raw, dict):
        return {}

    result: dict[str, str] = {}
    for key in ("agent_id", "project", "scope_type", "scope_id", "persona_dir"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()
    return result


def resolve_agent_id(payload: dict | None = None) -> str:
    config = load_runtime_config(payload)
    return (
        pick_env("SUMMONAI_AGENT_ID")
        or config.get("agent_id")
        or tmux_option("agent_id")
        or "default"
    )


def resolve_persona_dir(payload: dict | None = None, repo_dir: Path | None = None) -> Path:
    config = load_runtime_config(payload)
    raw = pick_env("SUMMONAI_PERSONA_DIR") or config.get("persona_dir")
    if raw:
        return Path(raw).expanduser()
    if repo_dir is not None:
        return repo_dir / "persona"
    return Path(__file__).resolve().parent.parent / "persona"


def is_tmux_session() -> bool:
    return bool(os.environ.get("TMUX_PANE"))


def memory_l1_save_enabled(payload: dict) -> bool:
    """Return True when L1/L2 memory hooks should run for this pane/session."""
    if not is_tmux_session():
        return True

    raw = (
        pick(payload, "memory_l1_save", "memoryL1Save")
        or pick_env("MEMORY_L1_SAVE", "SUMMONAI_MEMORY_L1_SAVE")
        or tmux_option("memory_l1_save")
    )
    if raw is None:
        return True
    return raw.strip().lower() not in FALSE_VALUES


def _sanitize_scope_id(raw: str) -> str:
    value = raw.strip()
    if not value:
        return "global"
    value = value.replace("\\", "/")
    value = value.rstrip("/")
    value = value.split("/")[-1] or value
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    return value.strip("-") or "global"


def _scope_from_cwd(cwd: str | None) -> str | None:
    if not cwd:
        return None
    try:
        git_root = subprocess.check_output(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None
    if not git_root:
        return None
    return _sanitize_scope_id(git_root)


def _scope_from_transcript_path(transcript_path: str | None) -> str | None:
    if not transcript_path:
        return None
    path = Path(transcript_path).expanduser()
    parent = path.parent.name
    # Claude transcript project slug for real repos starts with "-Users-...".
    if parent.startswith("-Users-"):
        tokens = [token for token in parent.split("-") if token]
        if tokens:
            return _sanitize_scope_id(tokens[-1])
    return None


def resolve_scope(payload: dict) -> dict[str, str | None]:
    """Resolve scope with precedence: env > project config > payload/cwd defaults."""
    config = load_runtime_config(payload)
    explicit_project = pick_env("SUMMONAI_PROJECT") or config.get("project") or pick(payload, "project")
    explicit_scope_type = pick_env("SUMMONAI_SCOPE_TYPE") or config.get("scope_type")
    explicit_scope_id = pick_env("SUMMONAI_SCOPE_ID") or config.get("scope_id")
    project_dir = pick_env("CLAUDE_PROJECT_DIR")
    cwd = pick(payload, "cwd") or pick_env("PWD")
    transcript_path = pick(payload, "transcript_path", "transcriptPath")

    if explicit_scope_id:
        scope_type = (explicit_scope_type or "project").strip().lower()
        if scope_type not in {"user", "project"}:
            scope_type = "project"
        scope_id = _sanitize_scope_id(explicit_scope_id)
        scope_source = "env/config.scope"
    elif explicit_project:
        scope_type = (explicit_scope_type or "project").strip().lower()
        if scope_type not in {"user", "project"}:
            scope_type = "project"
        scope_id = _sanitize_scope_id(explicit_project)
        scope_source = "env/config.project" if (pick_env("SUMMONAI_PROJECT") or config.get("project")) else "payload.project"
    elif project_dir:
        scope_type = "project"
        scope_id = _sanitize_scope_id(project_dir)
        scope_source = "CLAUDE_PROJECT_DIR"
    else:
        cwd_scope = _scope_from_cwd(cwd)
        transcript_scope = _scope_from_transcript_path(transcript_path)
        if cwd_scope:
            scope_type = "project"
            scope_id = cwd_scope
            scope_source = "cwd.git_root"
        elif transcript_scope:
            scope_type = "project"
            scope_id = transcript_scope
            scope_source = "transcript_path"
        else:
            scope_type = "user"
            scope_id = "global"
            scope_source = "user.default"

    if scope_type != "project":
        project = None
    else:
        project = scope_id
    return {
        "scope_type": scope_type,
        "scope_id": scope_id,
        "project": project,
        "scope_source": scope_source,
    }
