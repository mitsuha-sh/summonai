"""Zellij CLI wrapper for pane operations."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Sequence

PROMPT_MARKER_PATTERN = re.compile(r"[#$>%❯›]\s*$")
CODEX_PROMPT_PATTERN = re.compile(r"^\s*›\s")
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*[mGKHF]|\x1b\[\d* ?q")
SEND_TEXT_ENTER_DELAY_SECONDS = 0.2


class ZellijError(RuntimeError):
    """Raised when zellij pane operations fail."""


def _run_zellij(session: str, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    cmd = ["zellij", "--session", session, "action", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True)  # noqa: S603
    except FileNotFoundError as exc:
        raise ZellijError("zellij is not installed or not found in PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise ZellijError(_format_cli_error(exc)) from exc


def _format_cli_error(exc: subprocess.CalledProcessError) -> str:
    stderr = (exc.stderr or "").strip()
    stdout = (exc.stdout or "").strip()
    body = stderr or stdout or "unknown zellij CLI error"
    lowered = body.lower()
    if "no active zellij" in lowered or "session" in lowered and "not" in lowered:
        return f"zellij session is unavailable: {body}"
    return f"zellij command failed: {body}"


def _extract_pane_id(pane: dict) -> str | None:
    for key in ("pane_id", "paneId", "id"):
        value = pane.get(key)
        if value is not None:
            return _normalize_pane_id(value, pane)
    return None


def _normalize_pane_id(value: object, pane: dict) -> str:
    raw = str(value)
    if re.match(r"^(terminal|plugin)_\d+$", raw):
        return raw
    if raw.isdigit():
        pane_type = "plugin" if pane.get("is_plugin") else "terminal"
        return f"{pane_type}_{raw}"
    return raw


def create_tab(session: str, name: str, cwd: str | None = None) -> str:
    """Create a new tab and return its default pane_id.

    Issues ``zellij action new-tab --name <name>`` and returns the pane_id of
    the single default pane that zellij creates inside the new tab.

    When *cwd* is given, ``--cwd <path>`` is passed so the new tab's shell
    starts in the specified directory instead of inheriting the server's cwd.
    """
    before = {pane_id for pane_id in (_extract_pane_id(p) for p in list_panes(session)) if pane_id}
    cmd = ["new-tab", "--name", name]
    if cwd is not None:
        cmd.extend(["--cwd", cwd])
    _run_zellij(session, cmd)
    after = list_panes(session)
    new_ids = [pane_id for pane_id in (_extract_pane_id(p) for p in after) if pane_id and pane_id not in before]
    if len(new_ids) == 1:
        return new_ids[0]
    if len(new_ids) > 1:
        # Multiple new panes can appear when zellij also creates plugin/UI panes.
        # Pick the terminal pane with the highest numeric ID — it is the executor
        # pane that zellij opened as the default content of the new tab.
        terminal_ids = [pid for pid in new_ids if pid.startswith("terminal_")]
        if terminal_ids:
            return max(terminal_ids, key=lambda pid: int(pid.split("_")[1]))
        return new_ids[-1]
    raise ZellijError("failed to determine pane_id after creating tab")


def ensure_tab(session: str, tab_name: str) -> None:
    """Switch to named tab, creating it if it does not exist."""
    try:
        _run_zellij(session, ["go-to-tab-name", tab_name])
    except ZellijError:
        _run_zellij(session, ["new-tab", "--name", tab_name])


def go_to_tab(session: str, tab_name: str) -> None:
    """Switch focus to the named tab."""
    _run_zellij(session, ["go-to-tab-name", tab_name])


def create_pane(session: str, name: str | None = None, tab_name: str | None = None) -> str:
    """Create a pane and return its pane_id.

    If *tab_name* is given, the pane is created inside that tab (which is
    created automatically when it does not yet exist).  After creation the
    active tab is **not** changed back — call :func:`go_to_tab` from the
    caller when you need to restore focus.
    """
    if tab_name is not None:
        ensure_tab(session, tab_name)

    before = {pane_id for pane_id in (_extract_pane_id(p) for p in list_panes(session)) if pane_id}

    args = ["new-pane"]
    if name:
        args.extend(["--name", name])
    _run_zellij(session, args)

    after = list_panes(session)
    new_ids = [pane_id for pane_id in (_extract_pane_id(p) for p in after) if pane_id and pane_id not in before]
    if len(new_ids) == 1:
        return new_ids[0]

    if name:
        named_ids = [
            pane_id
            for pane in after
            for pane_id in [_extract_pane_id(pane)]
            if pane_id and str(pane.get("name") or pane.get("title") or "") == name
        ]
        if named_ids:
            return named_ids[-1]

    if len(new_ids) > 1:
        raise ZellijError(f"failed to determine pane_id uniquely after creation: {new_ids}")
    raise ZellijError("failed to determine pane_id after creating pane")


def send_text(session: str, pane_id: str, text: str) -> None:
    """Send text to a pane via paste + Enter."""
    _run_zellij(session, ["write-chars", "--pane-id", pane_id, "--", text])
    # Keep Enter as a separate event from write-chars for TUI tools (e.g. Codex CLI).
    time.sleep(SEND_TEXT_ENTER_DELAY_SECONDS)
    send_enter(session, pane_id)


def send_enter(session: str, pane_id: str) -> None:
    """Send Enter key via carriage-return keycode."""
    _run_zellij(session, ["write", "13", "--pane-id", pane_id])


def read_output(session: str, pane_id: str, lines: int = 100) -> str:
    """Read recent pane output and return up to the last ``lines`` lines."""
    result = _run_zellij(session, ["dump-screen", "--pane-id", pane_id, "--full"])
    output = result.stdout or ""
    if lines <= 0:
        return ""
    split_lines = output.splitlines()
    return "\n".join(split_lines[-lines:])


def close_pane(session: str, pane_id: str) -> None:
    """Close a pane by pane_id."""
    _run_zellij(session, ["close-pane", "--pane-id", pane_id])


def list_panes(session: str) -> list[dict]:
    """List panes in a session as JSON objects."""
    result = _run_zellij(session, ["list-panes", "--json"])
    raw = (result.stdout or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ZellijError("invalid JSON returned by zellij list-panes") from exc

    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ZellijError("unexpected list-panes payload: expected list[dict]")

    normalized: list[dict] = []
    for item in data:
        pane_id = _extract_pane_id(item)
        if pane_id:
            item = {**item, "pane_id": pane_id}
        normalized.append(item)
    return normalized


def rename_pane(session: str, pane_id: str, name: str) -> None:
    """Rename an existing pane."""
    _run_zellij(session, ["rename-pane", "--pane-id", pane_id, name])


def _strip_ansi(text: str) -> str:
    """Strip ANSI color/control escape sequences from text."""
    return ANSI_ESCAPE_PATTERN.sub("", text)
