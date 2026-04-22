from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from summonai_task import pane


def _cp(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["zellij"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_list_panes_success() -> None:
    with patch("summonai_task.pane.subprocess.run", return_value=_cp(stdout='[{"id":"3","name":"worker"}]')) as run:
        panes = pane.list_panes("summonai")

        assert panes == [{"id": "3", "name": "worker", "pane_id": "terminal_3"}]
        run.assert_called_once_with(
            ["zellij", "--session", "summonai", "action", "list-panes", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )


def test_list_panes_invalid_json_raises() -> None:
    with patch("summonai_task.pane.subprocess.run", return_value=_cp(stdout="not-json")):
        with pytest.raises(pane.ZellijError, match="invalid JSON"):
            pane.list_panes("summonai")


def test_create_pane_returns_new_id() -> None:
    with patch(
        "summonai_task.pane.subprocess.run",
        side_effect=[
            _cp(stdout='[{"id":1}]'),
            _cp(),
            _cp(stdout='[{"id":1},{"id":2}]'),
        ],
    ) as run:
        pane_id = pane.create_pane("summonai", name="worker-1")

        assert pane_id == "terminal_2"
        assert run.call_args_list == [
            call(
                ["zellij", "--session", "summonai", "action", "list-panes", "--json"],
                capture_output=True,
                text=True,
                check=True,
            ),
            call(
                ["zellij", "--session", "summonai", "action", "new-pane", "--name", "worker-1"],
                capture_output=True,
                text=True,
                check=True,
            ),
            call(
                ["zellij", "--session", "summonai", "action", "list-panes", "--json"],
                capture_output=True,
                text=True,
                check=True,
            ),
        ]


def test_create_tab_returns_new_pane_id() -> None:
    with patch(
        "summonai_task.pane.subprocess.run",
        side_effect=[
            _cp(stdout='[{"id":1}]'),
            _cp(),
            _cp(stdout='[{"id":1},{"id":2}]'),
        ],
    ) as run:
        pane_id = pane.create_tab("summonai", "task-001")

        assert pane_id == "terminal_2"
        assert run.call_args_list == [
            call(
                ["zellij", "--session", "summonai", "action", "list-panes", "--json"],
                capture_output=True,
                text=True,
                check=True,
            ),
            call(
                ["zellij", "--session", "summonai", "action", "new-tab", "--name", "task-001"],
                capture_output=True,
                text=True,
                check=True,
            ),
            call(
                ["zellij", "--session", "summonai", "action", "list-panes", "--json"],
                capture_output=True,
                text=True,
                check=True,
            ),
        ]


def test_create_tab_with_cwd_passes_cwd_flag() -> None:
    with patch(
        "summonai_task.pane.subprocess.run",
        side_effect=[
            _cp(stdout='[{"id":1}]'),
            _cp(),
            _cp(stdout='[{"id":1},{"id":2}]'),
        ],
    ) as run:
        pane_id = pane.create_tab("summonai", "task-002", cwd="/home/user/project")

        assert pane_id == "terminal_2"
        assert run.call_args_list == [
            call(
                ["zellij", "--session", "summonai", "action", "list-panes", "--json"],
                capture_output=True,
                text=True,
                check=True,
            ),
            call(
                ["zellij", "--session", "summonai", "action", "new-tab", "--name", "task-002", "--cwd", "/home/user/project"],
                capture_output=True,
                text=True,
                check=True,
            ),
            call(
                ["zellij", "--session", "summonai", "action", "list-panes", "--json"],
                capture_output=True,
                text=True,
                check=True,
            ),
        ]


def test_send_text_writes_payload_and_enter() -> None:
    with (
        patch("summonai_task.pane.subprocess.run", return_value=_cp()) as run,
        patch("summonai_task.pane.time.sleep") as sleep,
    ):
        pane.send_text("summonai", "2", "hello")

        assert run.call_args_list == [
            call(
                ["zellij", "--session", "summonai", "action", "write-chars", "--pane-id", "2", "--", "hello"],
                capture_output=True,
                text=True,
                check=True,
            ),
            call(
                ["zellij", "--session", "summonai", "action", "send-keys", "--pane-id", "2", "Enter"],
                capture_output=True,
                text=True,
                check=True,
            ),
        ]
        sleep.assert_called_once_with(pane.SEND_TEXT_ENTER_DELAY_SECONDS)


def test_read_output_returns_last_n_lines() -> None:
    with patch("summonai_task.pane.subprocess.run", return_value=_cp(stdout="a\nb\nc\n")):
        output = pane.read_output("summonai", "2", lines=2)

        assert output == "b\nc"


def test_close_and_rename_pane() -> None:
    with patch("summonai_task.pane.subprocess.run", return_value=_cp()) as run:
        pane.close_pane("summonai", "2")
        pane.rename_pane("summonai", "2", "alpha")

        assert run.call_args_list == [
            call(
                ["zellij", "--session", "summonai", "action", "close-pane", "--pane-id", "2"],
                capture_output=True,
                text=True,
                check=True,
            ),
            call(
                ["zellij", "--session", "summonai", "action", "rename-pane", "--pane-id", "2", "alpha"],
                capture_output=True,
                text=True,
                check=True,
            ),
        ]


def test_zellij_not_installed_raises_clear_error() -> None:
    with patch("summonai_task.pane.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(pane.ZellijError, match="not installed"):
            pane.list_panes("summonai")


def test_session_unavailable_raises_clear_error() -> None:
    with patch(
        "summonai_task.pane.subprocess.run",
        side_effect=subprocess.CalledProcessError(
            returncode=1,
            cmd=["zellij"],
            stderr="No active zellij session found",
        ),
    ):
        with pytest.raises(pane.ZellijError, match="session is unavailable"):
            pane.list_panes("summonai")


def test_strip_ansi_removes_color_sequences() -> None:
    assert pane._strip_ansi("\x1b[32mready>\x1b[0m") == "ready>"


def test_strip_ansi_removes_cursor_shape_sequences() -> None:
    assert pane._strip_ansi("❯ \x1b[6 q") == "❯ "
    assert pane._strip_ansi("› \x1b[2 q") == "› "


@pytest.mark.parametrize("prompt", ["$", "#", ">", "%", "❯", "›"])
def test_prompt_marker_pattern_supports_common_shell_markers(prompt: str) -> None:
    assert pane.PROMPT_MARKER_PATTERN.search(f"user@host {prompt}")


def test_codex_prompt_pattern_matches_codex_input_line() -> None:
    assert pane.CODEX_PROMPT_PATTERN.search("› Find and fix a bug in @filename")
    assert pane.CODEX_PROMPT_PATTERN.search("  › Write tests for @filename")
    assert not pane.CODEX_PROMPT_PATTERN.search("some text › not at start")
