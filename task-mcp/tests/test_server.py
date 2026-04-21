from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from summonai_task import server


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "summonai_task_test.db"
    monkeypatch.setenv("SUMMONAI_TASK_DB", str(db_path))
    monkeypatch.delenv("SUMMONAI_TASK_RUNNER_CONFIG", raising=False)
    monkeypatch.delenv("SUMMONAI_TASK_RUNNER", raising=False)
    monkeypatch.delenv("SUMMONAI_TASK_RUNNER_ENABLED", raising=False)
    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
    monkeypatch.delenv("SUMMONAI_EXECUTORS_CONFIG", raising=False)
    return db_path


def test_schema_and_wal_enabled(isolated_db: Path) -> None:
    conn = server.get_db()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"

        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"tasks", "task_events", "task_reviews", "schema_versions"}.issubset(table_names)

        task_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        assert "pane_id" in task_columns
        assert "task_number" in task_columns
        assert "needs_worktree" in task_columns

        applied_versions = {
            int(row[0])
            for row in conn.execute("SELECT version FROM schema_versions").fetchall()
        }
        assert 3 in applied_versions
        assert 4 in applied_versions
        assert 5 in applied_versions
    finally:
        conn.close()


def test_task_create_and_get(isolated_db: Path) -> None:
    created = server.task_create(
        title="Implement API",
        north_star="Ship P0",
        purpose="Implement minimum API",
        acceptance_criteria=["tests pass", "docs updated"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
        actor_id="tester",
    )

    assert created["task_id"] == "001"
    assert created["status"] == "assigned"
    assert created["created_at"].endswith("Z")

    detail = server.task_get(created["task_id"], include_history=True)
    assert detail["task"]["id"] == created["task_id"]
    assert detail["task"]["acceptance_criteria"] == ["tests pass", "docs updated"]
    assert detail["events"][0]["event_type"] == "create"


def test_task_update_transition_validation(isolated_db: Path) -> None:
    created = server.task_create(
        title="Transition test",
        north_star="State machine",
        purpose="Validate transitions",
        acceptance_criteria=["invalid transition rejected"],
        project="summonai-task",
        priority="medium",
        creator_role="interface",
        assignee_role="executor",
    )

    task_id = created["task_id"]
    updated = server.task_update(task_id=task_id, status="in_progress", progress_note="started")
    assert updated["task"]["status"] == "in_progress"

    with pytest.raises(ValueError):
        server.task_update(task_id=task_id, status="done")


def test_task_complete_moves_to_review_and_logs_event(isolated_db: Path) -> None:
    created = server.task_create(
        title="Complete test",
        north_star="Review flow",
        purpose="Move to review",
        acceptance_criteria=["review state"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )

    task_id = created["task_id"]
    server.task_update(task_id=task_id, status="in_progress")
    result = server.task_complete(
        task_id=task_id,
        summary="Implemented endpoints",
        artifact_paths=[f".summonai/artifacts/{task_id}/server.py"],
        verification="pytest passed",
        next_risks="review API not implemented",
    )
    assert result["task"]["status"] == "review"

    detail = server.task_get(task_id, include_history=True)
    event_types = [e["event_type"] for e in detail["events"]]
    assert event_types == ["create", "update", "complete"]


def test_task_list_filters(isolated_db: Path) -> None:
    a = server.task_create(
        title="A",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="proj-a",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )
    b = server.task_create(
        title="B",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="proj-b",
        priority="low",
        creator_role="interface",
    )

    by_project = server.task_list(project="proj-a")
    assert len(by_project) == 1
    assert by_project[0]["id"] == a["task_id"]

    pending = server.task_list(status="pending")
    assert len(pending) == 1
    assert pending[0]["id"] == b["task_id"]


def test_task_create_starts_zellij_runner_and_persists_pane_id(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({"enabled": True, "runner": "zellij", "zellij_session": "summonai"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))

    sent_payloads: list[tuple[str, str, str]] = []
    monkeypatch.setattr(server.pane, "list_panes", lambda _session: [])
    monkeypatch.setattr(server.pane, "create_tab", lambda _session, name, cwd=None: "terminal_42")
    monkeypatch.setattr(server.pane, "go_to_tab", lambda _session, _tab_name: None)
    monkeypatch.setattr(
        server.pane,
        "send_text",
        lambda session, pane_id, text: sent_payloads.append((session, pane_id, text)),
    )
    wait_calls: list[float] = []

    def _wait_for_any_output(_session: str, _pane_id: str, timeout: float = 30.0, interval: float = 0.5) -> str:
        wait_calls.append(timeout)
        return "\x1b[32mexecutor >\x1b[0m"

    monkeypatch.setattr(server, "_wait_for_any_output", _wait_for_any_output)

    created = server.task_create(
        title="Auto run",
        north_star="task_create -> zellij pane",
        purpose="spawn runner into pane",
        acceptance_criteria=["runner starts"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )

    assert created["runner_started"] is True
    assert created["runner_error"] is None

    detail = server.task_get(created["task_id"], include_history=True)
    assert detail["task"]["pane_id"] == "terminal_42"
    assert sent_payloads == [
        (
            "summonai",
            "terminal_42",
            "claude --dangerously-skip-permissions",
        ),
        (
            "summonai",
            "terminal_42",
            f'start task_id="{created["task_id"]}"。'
            f'開始したら最初に task_update(task_id="{created["task_id"]}", status="in_progress") を呼べ。',
        ),
    ]
    assert wait_calls == [30.0, 60.0]
    task_id_file = tmp_path / "summonai_pane_terminal_42.task_id"
    assert task_id_file.exists()
    assert task_id_file.read_text(encoding="utf-8") == created["task_id"]

    event_types = [e["event_type"] for e in detail["events"]]
    assert "pane_started" in event_types


def test_task_create_commits_before_runner_spawn(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({"enabled": True, "runner": "zellij", "zellij_session": "summonai"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))

    visible_counts: list[int] = []

    def _fake_spawn(_conn, task_id: str) -> tuple[bool, str | None]:
        with server.get_db() as verify_conn:
            count = verify_conn.execute("SELECT COUNT(*) FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
            visible_counts.append(int(count))
        return False, None

    monkeypatch.setattr(server, "_spawn_task_runner_if_configured", _fake_spawn)

    created = server.task_create(
        title="Commit before spawn",
        north_star="task_create commit timing",
        purpose="runner observes committed row",
        acceptance_criteria=["task row visible from separate connection"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )

    assert created["runner_started"] is False
    assert created["runner_error"] is None
    assert visible_counts == [1]


def test_task_create_returns_runner_error_on_ready_timeout(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({"enabled": True, "runner": "zellij", "zellij_session": "summonai"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.setattr(server.pane, "list_panes", lambda _session: [])
    monkeypatch.setattr(server.pane, "create_tab", lambda _session, name, cwd=None: "42")
    monkeypatch.setattr(server.pane, "go_to_tab", lambda _session, _tab_name: None)
    monkeypatch.setattr(server.pane, "send_text", lambda _session, _pane_id, _text: None)
    closed: list[str] = []
    monkeypatch.setattr(server.pane, "close_pane", lambda _session, pane_id: closed.append(pane_id))

    def _raise_timeout(_session: str, _pane_id: str, timeout: float = 30.0, interval: float = 0.5) -> str:
        raise TimeoutError("pane 42 prompt marker was not detected within 30s")

    monkeypatch.setattr(server, "_wait_for_any_output", _raise_timeout)

    created = server.task_create(
        title="Auto run",
        north_star="task_create -> timeout",
        purpose="runner timeout handling",
        acceptance_criteria=["runner returns error"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )

    assert created["runner_started"] is False
    assert "prompt marker" in (created["runner_error"] or "")
    assert closed == ["42"]


def test_task_create_returns_runner_error_when_executor_prompt_timeout(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({"enabled": True, "runner": "zellij", "zellij_session": "summonai"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.setattr(server.pane, "list_panes", lambda _session: [])
    monkeypatch.setattr(server.pane, "create_tab", lambda _session, name, cwd=None: "42")
    monkeypatch.setattr(server.pane, "go_to_tab", lambda _session, _tab_name: None)
    sent_payloads: list[str] = []
    monkeypatch.setattr(server.pane, "send_text", lambda _session, _pane_id, text: sent_payloads.append(text))
    closed: list[str] = []
    monkeypatch.setattr(server.pane, "close_pane", lambda _session, pane_id: closed.append(pane_id))

    wait_calls: list[float] = []

    def _wait_for_any_output(_session: str, _pane_id: str, timeout: float = 30.0, interval: float = 0.5) -> str:
        wait_calls.append(timeout)
        if timeout == 60.0:
            raise TimeoutError("pane 42 prompt marker was not detected within 60s")
        return "shell ready >"

    monkeypatch.setattr(server, "_wait_for_any_output", _wait_for_any_output)

    created = server.task_create(
        title="Auto run",
        north_star="task_create -> timeout",
        purpose="executor prompt timeout handling",
        acceptance_criteria=["runner returns error"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )

    assert created["runner_started"] is False
    assert "prompt marker" in (created["runner_error"] or "")
    assert closed == ["42"]
    assert wait_calls == [30.0, 60.0]
    assert len(sent_payloads) == 1


def test_task_create_returns_runner_error_when_zellij_session_missing(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({"enabled": True, "runner": "zellij"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)

    created = server.task_create(
        title="Auto run",
        north_star="task_create -> config validation",
        purpose="session missing validation",
        acceptance_criteria=["runner returns error"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )

    assert created["runner_started"] is False
    assert "zellij_session" in (created["runner_error"] or "")


def test_orphan_pane_cleanup_clears_stale_pane_id(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created = server.task_create(
        title="Cleanup",
        north_star="remove stale panes",
        purpose="cleanup stale pane id",
        acceptance_criteria=["stale pane cleared"],
        project="summonai-task",
        priority="low",
        creator_role="interface",
    )
    task_id = created["task_id"]
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET pane_id = ? WHERE id = ?", ("stale-pane", task_id))
        monkeypatch.setattr(server.pane, "list_panes", lambda _session: [{"id": "other-pane"}])
        server._cleanup_orphan_panes(conn, "summonai")

    detail = server.task_get(task_id, include_history=True)
    assert detail["task"]["pane_id"] is None
    assert any(event["event_type"] == "pane_orphan_cleanup" for event in detail["events"])

def test_task_complete_keeps_pane_open_during_review(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created = server.task_create(
        title="Keep pane",
        north_star="complete keeps pane for review",
        purpose="keep pane on complete",
        acceptance_criteria=["pane preserved"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET pane_id = ? WHERE id = ?", ("55", task_id))

    server.task_update(task_id=task_id, status="in_progress")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "summonai")
    closed: list[tuple[str, str]] = []
    monkeypatch.setattr(server.pane, "close_pane", lambda session, pane_id: closed.append((session, pane_id)))

    result = server.task_complete(
        task_id=task_id,
        summary="done",
        artifact_paths=[],
        verification="ok",
    )

    assert result["task"]["status"] == "review"
    assert closed == []
    detail = server.task_get(task_id, include_history=True)
    assert detail["task"]["pane_id"] == "55"


def test_task_update_from_review_closes_pane_and_clears_pane_id(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = server.task_create(
        title="Close on review exit",
        north_star="review exit closes pane",
        purpose="close pane when review is complete",
        acceptance_criteria=["pane closed on review exit"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET status = ?, pane_id = ? WHERE id = ?", ("review", "55", task_id))

    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "summonai")
    closed: list[tuple[str, str]] = []
    monkeypatch.setattr(server.pane, "close_pane", lambda session, pane_id: closed.append((session, pane_id)))

    result = server.task_update(task_id=task_id, status="done")

    assert result["task"]["status"] == "done"
    assert closed == [("summonai", "55")]
    detail = server.task_get(task_id, include_history=True)
    assert detail["task"]["pane_id"] is None
    assert any(event["event_type"] == "pane_closed" for event in detail["events"])


def test_task_cancel_closes_pane_clears_pane_id_and_logs_event(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = server.task_create(
        title="Cancel pane",
        north_star="cancel closes pane",
        purpose="close pane on cancel",
        acceptance_criteria=["pane closed on cancel"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET status = ?, pane_id = ? WHERE id = ?", ("in_progress", "88", task_id))

    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "summonai")
    closed: list[tuple[str, str]] = []
    monkeypatch.setattr(server.pane, "close_pane", lambda session, pane_id: closed.append((session, pane_id)))

    result = server.task_cancel(task_id=task_id, reason="stopped by operator")

    assert result["cancelled"] is True
    assert result["pane_closed"] is True
    assert result["pane_close_error"] is None
    assert result["task"]["status"] == "cancelled"
    assert closed == [("summonai", "88")]

    detail = server.task_get(task_id, include_history=True)
    assert detail["task"]["pane_id"] is None
    assert detail["events"][-1]["event_type"] == "cancel"
    assert detail["events"][-1]["payload"]["reason"] == "stopped by operator"


def test_task_cancel_handles_missing_pane_gracefully(isolated_db: Path) -> None:
    created = server.task_create(
        title="Cancel without pane",
        north_star="graceful cancel",
        purpose="cancel even when no pane",
        acceptance_criteria=["cancel works without pane"],
        project="summonai-task",
        priority="medium",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET status = ?, pane_id = NULL WHERE id = ?", ("assigned", task_id))

    result = server.task_cancel(task_id=task_id, reason="no pane case")
    assert result["task"]["status"] == "cancelled"
    assert result["pane_closed"] is False
    assert result["pane_close_error"] is None


def test_task_cancel_without_pane_ignores_broken_runner_config(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text("{invalid", encoding="utf-8")
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))

    created = server.task_create(
        title="Cancel without pane + broken config",
        north_star="graceful cancel",
        purpose="cancel must not parse config when pane is missing",
        acceptance_criteria=["cancel works without pane"],
        project="summonai-task",
        priority="medium",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET status = ?, pane_id = NULL WHERE id = ?", ("assigned", task_id))

    result = server.task_cancel(task_id=task_id, reason="no pane case")
    assert result["task"]["status"] == "cancelled"
    assert result["pane_closed"] is False
    assert result["pane_close_error"] is None


def test_task_cancel_rejects_done_status(isolated_db: Path) -> None:
    created = server.task_create(
        title="Done task",
        north_star="done is immutable for cancel",
        purpose="done reject",
        acceptance_criteria=["done cannot be cancelled"],
        project="summonai-task",
        priority="low",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", ("done", task_id))

    with pytest.raises(ValueError, match="done status task cannot be cancelled"):
        server.task_cancel(task_id=task_id, reason="must fail")


def test_task_update_non_review_transition_ignores_broken_runner_config(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text("{invalid", encoding="utf-8")
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))

    created = server.task_create(
        title="Update without review + broken config",
        north_star="non-review update survives broken config",
        purpose="avoid runner config dependency for non-review transitions",
        acceptance_criteria=["in_progress transition succeeds"],
        project="summonai-task",
        priority="medium",
        creator_role="interface",
        assignee_role="executor",
    )

    updated = server.task_update(task_id=created["task_id"], status="in_progress")
    assert updated["task"]["status"] == "in_progress"


def test_task_resume_skips_when_existing_pane_is_active(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({"enabled": True, "runner": "zellij", "zellij_session": "summonai"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))

    created = server.task_create(
        title="Resume skip",
        north_star="resume",
        purpose="skip when pane is alive",
        acceptance_criteria=["skip works"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET status = ?, pane_id = ? WHERE id = ?", ("assigned", "terminal_77", task_id))

    monkeypatch.setattr(server.pane, "list_panes", lambda _session: [{"pane_id": "terminal_77"}])
    monkeypatch.setattr(server.pane, "create_tab", lambda *_args, **_kwargs: pytest.fail("must not create tab"))

    result = server.task_resume(task_id=task_id, actor_id="tester")

    assert result["resumed"] is False
    assert result["skipped"] is True
    assert result["reason"] == "pane_already_active"
    assert result["pane_id"] == "terminal_77"


def test_task_resume_recreates_missing_pane(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({"enabled": True, "runner": "zellij", "zellij_session": "summonai"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))

    created = server.task_create(
        title="Resume",
        north_star="resume",
        purpose="recreate pane and continue",
        acceptance_criteria=["resume works"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
    )
    task_id = created["task_id"]
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET status = ?, pane_id = ? WHERE id = ?", ("assigned", "terminal_10", task_id))

    monkeypatch.setattr(server.pane, "list_panes", lambda _session: [{"pane_id": "terminal_99"}])
    monkeypatch.setattr(server.pane, "create_tab", lambda _session, name, cwd=None: "terminal_42")
    monkeypatch.setattr(server.pane, "go_to_tab", lambda _session, _tab_name: None)
    sent_payloads: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        server.pane,
        "send_text",
        lambda session, pane_id, text: sent_payloads.append((session, pane_id, text)),
    )
    monkeypatch.setattr(server, "_wait_for_any_output", lambda *_args, **_kwargs: "ready >")

    result = server.task_resume(task_id=task_id, actor_id="tester")

    assert result["resumed"] is True
    assert result["skipped"] is False
    assert result["pane_id"] == "terminal_42"
    assert sent_payloads == [
        ("summonai", "terminal_42", "claude --dangerously-skip-permissions"),
        (
            "summonai",
            "terminal_42",
            f'task_id="{task_id}" のタスクを再開せよ。'
            f'task_get(task_id="{task_id}") で現在の要件を確認し、'
            "まず git status と既存成果物を確認して不足分のみ実装せよ。"
            "acceptance_criteria を満たしたら task_complete を呼べ。",
        ),
    ]
    detail = server.task_get(task_id, include_history=True)
    assert detail["task"]["pane_id"] == "terminal_42"
    assert detail["events"][-1]["event_type"] == "resume"


def test_task_resume_requires_assigned_status(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({"enabled": True, "runner": "zellij", "zellij_session": "summonai"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))

    created = server.task_create(
        title="Resume invalid",
        north_star="resume",
        purpose="status check",
        acceptance_criteria=["must be assigned"],
        project="summonai-task",
        priority="low",
        creator_role="interface",
    )

    with pytest.raises(ValueError, match="must be assigned"):
        server.task_resume(task_id=created["task_id"])


def test_load_runner_config_zellij_session_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps(
            {
                "enabled": True,
                "command": ["echo", "ok"],
                "zellij_session": "from-config",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "from-env")

    loaded = server._load_runner_config()
    assert loaded["zellij_session"] == "from-config"


def test_load_runner_config_zellij_session_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps(
            {
                "enabled": True,
                "command": ["echo", "ok"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "from-env")

    loaded = server._load_runner_config()
    assert loaded["zellij_session"] == "from-env"


def test_load_runner_config_zellij_session_none_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps(
            {
                "enabled": True,
                "command": ["echo", "ok"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)

    loaded = server._load_runner_config()
    assert loaded["zellij_session"] is None


def test_task_peek_reads_output(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created = server.task_create(
        title="Peek output",
        north_star="peek",
        purpose="read pane output",
        acceptance_criteria=["peek works"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET pane_id = ? WHERE id = ?", ("42", task_id))

    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "summonai")
    monkeypatch.setattr(server.pane, "read_output", lambda session, pane_id, lines=100: f"{session}:{pane_id}:{lines}")

    result = server.task_peek(task_id=task_id, lines=25)

    assert result["task_id"] == task_id
    assert result["pane_id"] == "42"
    assert result["lines"] == 25
    assert result["output"] == "summonai:42:25"


def test_task_peek_works_for_done_status(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created = server.task_create(
        title="Peek done",
        north_star="peek done",
        purpose="allow done status",
        acceptance_criteria=["peek works for done"],
        project="summonai-task",
        priority="medium",
        creator_role="interface",
    )
    task_id = created["task_id"]
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET status = ?, pane_id = ? WHERE id = ?", ("done", "55", task_id))

    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "summonai")
    monkeypatch.setattr(server.pane, "read_output", lambda _session, _pane_id, lines=100: f"tail={lines}")

    result = server.task_peek(task_id=task_id, lines=10)

    assert result["status"] == "done"
    assert result["output"] == "tail=10"


def test_task_peek_raises_when_pane_id_missing(isolated_db: Path) -> None:
    created = server.task_create(
        title="Peek error",
        north_star="peek error",
        purpose="pane_id required",
        acceptance_criteria=["error when pane missing"],
        project="summonai-task",
        priority="low",
        creator_role="interface",
    )

    with pytest.raises(ValueError, match="has no pane_id"):
        server.task_peek(task_id=created["task_id"])


def test_task_message_sends_text_and_logs_event(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created = server.task_create(
        title="Send message",
        north_star="message",
        purpose="send to running pane",
        acceptance_criteria=["message sent"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    server.task_update(task_id=task_id, status="in_progress")
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET pane_id = ? WHERE id = ?", ("42", task_id))

    sent_payloads: list[tuple[str, str, str]] = []
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "summonai")
    monkeypatch.setattr(
        server.pane,
        "send_text",
        lambda session, pane_id, text: sent_payloads.append((session, pane_id, text)),
    )

    result = server.task_message(task_id=task_id, message="continue with tests", actor_id="tester")

    assert result["sent"] is True
    assert result["pane_id"] == "42"
    assert sent_payloads == [("summonai", "42", "continue with tests")]
    detail = server.task_get(task_id, include_history=True)
    assert detail["events"][-1]["event_type"] == "message"
    assert detail["events"][-1]["payload"]["message"] == "continue with tests"


def test_task_message_requires_in_progress_status(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created = server.task_create(
        title="Send message",
        north_star="message",
        purpose="status validation",
        acceptance_criteria=["status checked"],
        project="summonai-task",
        priority="low",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET pane_id = ? WHERE id = ?", ("42", task_id))

    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "summonai")
    with pytest.raises(ValueError, match="must be in_progress"):
        server.task_message(task_id=task_id, message="hello")


def test_task_message_requires_pane_id(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created = server.task_create(
        title="Send message",
        north_star="message",
        purpose="pane validation",
        acceptance_criteria=["pane checked"],
        project="summonai-task",
        priority="low",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    server.task_update(task_id=task_id, status="in_progress")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "summonai")

    with pytest.raises(ValueError, match="has no pane_id"):
        server.task_message(task_id=task_id, message="hello")


def test_task_message_requires_zellij_session(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created = server.task_create(
        title="Send message",
        north_star="message",
        purpose="session validation",
        acceptance_criteria=["session checked"],
        project="summonai-task",
        priority="low",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    server.task_update(task_id=task_id, status="in_progress")
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET pane_id = ? WHERE id = ?", ("42", task_id))

    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
    with pytest.raises(ValueError, match="zellij_session is not configured"):
        server.task_message(task_id=task_id, message="hello")


def test_task_message_serializes_parallel_calls(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created = server.task_create(
        title="Send message",
        north_star="message",
        purpose="serialize writes",
        acceptance_criteria=["parallel calls serialized"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    server.task_update(task_id=task_id, status="in_progress")
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET pane_id = ? WHERE id = ?", ("42", task_id))

    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "summonai")

    gate = threading.Barrier(2)
    state_lock = threading.Lock()
    active_calls = 0
    max_active_calls = 0

    def _send_text(_session: str, _pane_id: str, _text: str) -> None:
        nonlocal active_calls, max_active_calls
        with state_lock:
            active_calls += 1
            if active_calls > max_active_calls:
                max_active_calls = active_calls
        time.sleep(0.15)
        with state_lock:
            active_calls -= 1

    monkeypatch.setattr(server.pane, "send_text", _send_text)
    errors: list[Exception] = []

    def _worker(msg: str) -> None:
        try:
            gate.wait(timeout=2.0)
            server.task_message(task_id=task_id, message=msg)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    first = threading.Thread(target=_worker, args=("first",))
    second = threading.Thread(target=_worker, args=("second",))
    first.start()
    second.start()
    first.join()
    second.join()

    assert errors == []
    assert max_active_calls == 1

    detail = server.task_get(task_id, include_history=True)
    message_events = [event for event in detail["events"] if event["event_type"] == "message"]
    assert len(message_events) == 2


# ── task_number tests ─────────────────────────────────────────────────────────

def test_task_number_increments_globally(isolated_db: Path) -> None:
    a = server.task_create(
        title="First",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="proj-x",
        priority="high",
        creator_role="interface",
    )
    b = server.task_create(
        title="Second",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="proj-x",
        priority="high",
        creator_role="interface",
    )
    c = server.task_create(
        title="Other project",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="proj-y",
        priority="high",
        creator_role="interface",
    )

    detail_a = server.task_get(a["task_id"])
    detail_b = server.task_get(b["task_id"])
    detail_c = server.task_get(c["task_id"])

    assert detail_a["task"]["task_number"] == 1
    assert detail_b["task"]["task_number"] == 2
    assert detail_c["task"]["task_number"] == 3  # global sequential, not per-project
    assert detail_a["task"]["id"] == "001"
    assert detail_b["task"]["id"] == "002"
    assert detail_c["task"]["id"] == "003"


def test_task_number_in_task_list(isolated_db: Path) -> None:
    server.task_create(
        title="T1",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="proj-z",
        priority="high",
        creator_role="interface",
    )
    server.task_create(
        title="T2",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="proj-z",
        priority="low",
        creator_role="interface",
    )

    tasks = server.task_list(project="proj-z", order_by="task_number")
    assert len(tasks) == 2
    assert tasks[0]["task_number"] == 1
    assert tasks[1]["task_number"] == 2


def test_task_list_summary_mode(isolated_db: Path) -> None:
    server.task_create(
        title="Summary test task",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="proj-summary",
        priority="high",
        creator_role="interface",
    )

    tasks = server.task_list(project="proj-summary", summary=True)
    assert len(tasks) == 1
    task = tasks[0]
    assert set(task.keys()) == {"id", "title", "status", "priority", "updated_at"}


def test_task_list_summary_mode_size(isolated_db: Path) -> None:
    import json as _json

    for i in range(20):
        server.task_create(
            title=f"Task {i:02d} with a somewhat longer title to simulate real data",
            north_star="Improve quality",
            purpose="This is a detailed purpose that would normally add many tokens to the response.",
            acceptance_criteria=["criterion one", "criterion two", "criterion three"],
            project="proj-size",
            priority="medium",
            creator_role="interface",
        )

    tasks = server.task_list(project="proj-size", summary=True)
    assert len(tasks) == 20
    serialized = _json.dumps(tasks)
    # Rough token estimate: ~4 chars per token; must be well under 1k tokens
    estimated_tokens = len(serialized) / 4
    assert estimated_tokens < 1000, f"Too large: ~{estimated_tokens:.0f} tokens (raw {len(serialized)} chars)"


def test_task_list_summary_backward_compat(isolated_db: Path) -> None:
    server.task_create(
        title="Compat task",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="proj-compat",
        priority="low",
        creator_role="interface",
    )

    tasks = server.task_list(project="proj-compat")
    assert len(tasks) == 1
    # Full mode must include detailed fields
    assert "purpose" in tasks[0]
    assert "acceptance_criteria" in tasks[0]
    assert "metadata" in tasks[0]


def test_task_list_exclude_status(isolated_db: Path) -> None:
    server.task_create(
        title="Pending task",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="proj-excl",
        priority="medium",
        creator_role="interface",
    )
    done_task = server.task_create(
        title="Done task",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="proj-excl",
        priority="medium",
        creator_role="interface",
        assignee_role="executor",
    )
    # Advance to done via status transitions
    server.task_update(task_id=done_task["task_id"], status="in_progress", actor_id="executor")
    server.task_complete(
        task_id=done_task["task_id"],
        summary="done",
        artifact_paths=[],
        verification="verified",
        actor_id="executor",
    )
    server.task_update(task_id=done_task["task_id"], status="done", actor_id="interface")

    all_tasks = server.task_list(project="proj-excl")
    assert len(all_tasks) == 2

    active_tasks = server.task_list(project="proj-excl", exclude_status=["done", "cancelled"])
    assert len(active_tasks) == 1
    assert active_tasks[0]["title"] == "Pending task"


def test_task_list_exclude_status_multiple(isolated_db: Path) -> None:
    for title, status_seq in [
        ("T-pending", []),
        ("T-inprog", ["in_progress"]),
    ]:
        created = server.task_create(
            title=title,
            north_star="N",
            purpose="P",
            acceptance_criteria=["ok"],
            project="proj-excl2",
            priority="low",
            creator_role="interface",
            assignee_role="executor",
        )
        for st in status_seq:
            server.task_update(task_id=created["task_id"], status=st, actor_id="executor")

    tasks = server.task_list(project="proj-excl2", exclude_status=["in_progress"])
    assert len(tasks) == 1
    assert tasks[0]["title"] == "T-pending"


# ── artifact_paths validation tests ───────────────────────────────────────────

def test_task_complete_rejects_invalid_artifact_path(isolated_db: Path) -> None:
    created = server.task_create(
        title="Artifact test",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    server.task_update(task_id=task_id, status="in_progress")

    with pytest.raises(ValueError, match=r"\.summonai/artifacts/"):
        server.task_complete(
            task_id=task_id,
            summary="done",
            artifact_paths=["src/some_file.py"],
            verification="ok",
        )


def test_task_complete_allows_empty_artifact_paths(isolated_db: Path) -> None:
    created = server.task_create(
        title="No artifacts",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    server.task_update(task_id=task_id, status="in_progress")

    result = server.task_complete(
        task_id=task_id,
        summary="done",
        artifact_paths=[],
        verification="ok",
    )
    assert result["task"]["status"] == "review"


def test_task_update_rejects_invalid_artifact_path(isolated_db: Path) -> None:
    created = server.task_create(
        title="Artifact update test",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    server.task_update(task_id=task_id, status="in_progress")

    with pytest.raises(ValueError, match=r"\.summonai/artifacts/"):
        server.task_update(
            task_id=task_id,
            artifact_paths=["outputs/result.json"],
        )


def test_task_update_allows_valid_artifact_path(isolated_db: Path) -> None:
    created = server.task_create(
        title="Valid artifact",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    server.task_update(task_id=task_id, status="in_progress")

    result = server.task_update(
        task_id=task_id,
        artifact_paths=[f".summonai/artifacts/{task_id}/output.json"],
    )
    assert result["task"]["metadata"]["artifact_paths"] == [
        f".summonai/artifacts/{task_id}/output.json"
    ]


# ── task_reopen tests ──────────────────────────────────────────────────────────

def _make_review_task(title: str = "Reopen test") -> str:
    """Helper: create a task and advance it to review status."""
    created = server.task_create(
        title=title,
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    server.task_update(task_id=task_id, status="in_progress")
    server.task_complete(
        task_id=task_id,
        summary="First pass done",
        artifact_paths=[],
        verification="pytest passed",
    )
    return task_id


def test_task_reopen_rejects_non_review_status(isolated_db: Path) -> None:
    created = server.task_create(
        title="Not review",
        north_star="N",
        purpose="P",
        acceptance_criteria=["ok"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    with pytest.raises(ValueError, match="review"):
        server.task_reopen(task_id=task_id, message="add more tests")


def test_task_reopen_transitions_review_to_in_progress(isolated_db: Path) -> None:
    task_id = _make_review_task()
    result = server.task_reopen(task_id=task_id, message="fix edge case")
    assert result["task"]["status"] == "in_progress"

    detail = server.task_get(task_id, include_history=True)
    assert detail["task"]["status"] == "in_progress"


def test_task_reopen_logs_reopen_event(isolated_db: Path) -> None:
    task_id = _make_review_task()
    server.task_reopen(task_id=task_id, message="handle timeout", actor_id="tester")

    detail = server.task_get(task_id, include_history=True)
    reopen_events = [e for e in detail["events"] if e["event_type"] == "reopen"]
    assert len(reopen_events) == 1
    payload = reopen_events[0]["payload"]
    assert payload["from"] == "review"
    assert payload["to"] == "in_progress"
    assert payload["message"] == "handle timeout"


def test_task_reopen_with_existing_pane_restarts_claude(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({"enabled": True, "runner": "zellij", "zellij_session": "summonai"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))

    task_id = _make_review_task("Existing pane reopen")
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET pane_id = ? WHERE id = ?", ("terminal_99", task_id))

    sent_payloads: list[tuple[str, str, str]] = []
    monkeypatch.setattr(server.pane, "list_panes", lambda _session: [{"id": "terminal_99"}])
    monkeypatch.setattr(
        server.pane, "send_text",
        lambda session, pane_id, text: sent_payloads.append((session, pane_id, text)),
    )
    monkeypatch.setattr(
        server, "_wait_for_any_output",
        lambda _s, _p, timeout=30.0, interval=0.5: "\x1b[32mexecutor >\x1b[0m",
    )

    result = server.task_reopen(task_id=task_id, message="add retry logic", actor_id="interface")

    assert result["runner_started"] is True
    assert result["runner_error"] is None
    assert result["pane_id"] == "terminal_99"

    assert len(sent_payloads) == 1
    assert sent_payloads[0][0] == "summonai"
    assert sent_payloads[0][1] == "terminal_99"
    assert "add retry logic" in sent_payloads[0][2]
    assert task_id in sent_payloads[0][2]

    detail = server.task_get(task_id, include_history=True)
    assert any(e["event_type"] == "pane_restarted" for e in detail["events"])


def test_task_reopen_without_pane_creates_new_pane(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({"enabled": True, "runner": "zellij", "zellij_session": "summonai"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))

    task_id = _make_review_task("New pane reopen")

    sent_payloads: list[tuple[str, str, str]] = []
    monkeypatch.setattr(server.pane, "list_panes", lambda _session: [])
    monkeypatch.setattr(server.pane, "create_tab", lambda _session, name, cwd=None: "terminal_77")
    monkeypatch.setattr(server.pane, "go_to_tab", lambda _session, _tab_name: None)
    monkeypatch.setattr(
        server.pane, "send_text",
        lambda session, pane_id, text: sent_payloads.append((session, pane_id, text)),
    )
    monkeypatch.setattr(
        server, "_wait_for_any_output",
        lambda _s, _p, timeout=30.0, interval=0.5: "\x1b[32mexecutor >\x1b[0m",
    )

    result = server.task_reopen(task_id=task_id, message="handle edge case", actor_id="interface")

    assert result["runner_started"] is True
    assert result["runner_error"] is None
    assert result["pane_id"] == "terminal_77"

    assert sent_payloads[0] == ("summonai", "terminal_77", "claude --dangerously-skip-permissions")
    assert "handle edge case" in sent_payloads[1][2]

    detail = server.task_get(task_id, include_history=True)
    assert detail["task"]["pane_id"] == "terminal_77"
    assert any(e["event_type"] == "pane_started" for e in detail["events"])


def test_task_reopen_reopen_event_contains_pane_id(isolated_db: Path) -> None:
    task_id = _make_review_task("Event pane_id test")
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET pane_id = ? WHERE id = ?", ("pane_42", task_id))

    server.task_reopen(task_id=task_id, message="check pane_id in event")

    detail = server.task_get(task_id, include_history=True)
    reopen_event = next(e for e in detail["events"] if e["event_type"] == "reopen")
    assert reopen_event["payload"]["pane_id"] == "pane_42"


def test_task_reopen_requires_nonempty_message(isolated_db: Path) -> None:
    task_id = _make_review_task()
    with pytest.raises(ValueError):
        server.task_reopen(task_id=task_id, message="   ")


# ---------------------------------------------------------------------------
# needs_worktree tests
# ---------------------------------------------------------------------------

def test_needs_worktree_defaults_false(isolated_db: Path) -> None:
    created = server.task_create(
        title="No worktree",
        north_star="no worktree needed",
        purpose="verify default",
        acceptance_criteria=["needs_worktree=False"],
        project="summonai-task",
        priority="low",
        creator_role="interface",
    )
    detail = server.task_get(created["task_id"])
    assert detail["task"]["needs_worktree"] is False


def test_needs_worktree_stored_and_returned(isolated_db: Path) -> None:
    created = server.task_create(
        title="With worktree",
        north_star="worktree needed",
        purpose="verify flag stored",
        acceptance_criteria=["needs_worktree=True"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        needs_worktree=True,
    )
    detail = server.task_get(created["task_id"])
    assert detail["task"]["needs_worktree"] is True

    tasks = server.task_list(project="summonai-task")
    assert any(t["needs_worktree"] is True for t in tasks)


def test_spawn_creates_worktree_when_needs_worktree_true(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({
            "enabled": True,
            "runner": "zellij",
            "zellij_session": "summonai",
            "project_dir": str(tmp_path),
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))

    worktree_calls: list[tuple[str, str]] = []

    def _fake_create_worktree(project_dir: str, task_id: str) -> Path:
        worktree_calls.append((project_dir, task_id))
        worktree = Path(project_dir) / ".worktrees" / task_id
        worktree.mkdir(parents=True, exist_ok=True)
        return worktree

    monkeypatch.setattr(server, "_create_worktree", _fake_create_worktree)
    monkeypatch.setattr(server.pane, "list_panes", lambda _session: [])
    monkeypatch.setattr(server.pane, "create_tab", lambda _session, name, cwd=None: "terminal_99")
    monkeypatch.setattr(server.pane, "go_to_tab", lambda _session, _tab_name: None)

    sent_payloads: list[str] = []
    monkeypatch.setattr(
        server.pane,
        "send_text",
        lambda _session, _pane_id, text: sent_payloads.append(text),
    )
    monkeypatch.setattr(
        server,
        "_wait_for_any_output",
        lambda _session, _pane_id, timeout=30.0, interval=0.5: "executor >",
    )

    created = server.task_create(
        title="Worktree task",
        north_star="auto worktree",
        purpose="verify worktree created on spawn",
        acceptance_criteria=["worktree add called"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
        needs_worktree=True,
    )

    assert created["runner_started"] is True
    assert len(worktree_calls) == 1
    assert worktree_calls[0][1] == created["task_id"]

    worktree_path = str(Path(str(tmp_path)) / ".worktrees" / created["task_id"])
    launch_cmd = sent_payloads[0]
    assert f"SUMMONAI_WORKTREE_PATH={worktree_path}" in launch_cmd
    assert "claude --dangerously-skip-permissions" in launch_cmd


def test_spawn_does_not_create_worktree_when_needs_worktree_false(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({
            "enabled": True,
            "runner": "zellij",
            "zellij_session": "summonai",
            "project_dir": str(tmp_path),
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))

    worktree_calls: list[tuple[str, str]] = []

    def _fake_create_worktree(project_dir: str, task_id: str) -> Path:
        worktree_calls.append((project_dir, task_id))
        return Path(project_dir) / ".worktrees" / task_id

    monkeypatch.setattr(server, "_create_worktree", _fake_create_worktree)
    monkeypatch.setattr(server.pane, "list_panes", lambda _session: [])
    monkeypatch.setattr(server.pane, "create_tab", lambda _session, name, cwd=None: "terminal_88")
    monkeypatch.setattr(server.pane, "go_to_tab", lambda _session, _tab_name: None)
    monkeypatch.setattr(server.pane, "send_text", lambda *a, **kw: None)
    monkeypatch.setattr(
        server,
        "_wait_for_any_output",
        lambda _session, _pane_id, timeout=30.0, interval=0.5: "executor >",
    )

    server.task_create(
        title="No worktree task",
        north_star="no auto worktree",
        purpose="verify worktree NOT created when flag=False",
        acceptance_criteria=["worktree add not called"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
        needs_worktree=False,
    )

    assert worktree_calls == []


def test_task_complete_removes_worktree_when_needs_worktree_true(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({
            "enabled": False,
            "project_dir": str(tmp_path),
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))

    remove_calls: list[tuple[str, str]] = []

    def _fake_remove(project_dir: str, task_id: str) -> None:
        remove_calls.append((project_dir, task_id))

    monkeypatch.setattr(server, "_remove_worktree", _fake_remove)

    created = server.task_create(
        title="Worktree complete",
        north_star="remove worktree on complete",
        purpose="verify worktree removed on task_complete",
        acceptance_criteria=["worktree remove called"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
        needs_worktree=True,
    )
    task_id = created["task_id"]
    server.task_update(task_id=task_id, status="in_progress")
    server.task_complete(
        task_id=task_id,
        summary="done",
        artifact_paths=[],
        verification="ok",
    )

    assert len(remove_calls) == 1
    assert remove_calls[0][1] == task_id


def test_task_cancel_removes_worktree_when_needs_worktree_true(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({
            "enabled": False,
            "project_dir": str(tmp_path),
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))

    remove_calls: list[tuple[str, str]] = []

    def _fake_remove(project_dir: str, task_id: str) -> None:
        remove_calls.append((project_dir, task_id))

    monkeypatch.setattr(server, "_remove_worktree", _fake_remove)

    created = server.task_create(
        title="Worktree cancel",
        north_star="remove worktree on cancel",
        purpose="verify worktree removed on task_cancel",
        acceptance_criteria=["worktree remove called on cancel"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
        needs_worktree=True,
    )
    task_id = created["task_id"]

    result = server.task_cancel(task_id=task_id, reason="test")
    assert result["task"]["status"] == "cancelled"
    assert len(remove_calls) == 1
    assert remove_calls[0][1] == task_id


def test_task_complete_does_not_remove_worktree_when_flag_false(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({"enabled": False, "project_dir": str(tmp_path)}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))

    remove_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        server,
        "_remove_worktree",
        lambda project_dir, task_id: remove_calls.append((project_dir, task_id)),
    )

    created = server.task_create(
        title="No worktree complete",
        north_star="no remove on complete",
        purpose="verify no worktree removal when flag=False",
        acceptance_criteria=["worktree remove NOT called"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
        needs_worktree=False,
    )
    task_id = created["task_id"]
    server.task_update(task_id=task_id, status="in_progress")
    server.task_complete(
        task_id=task_id,
        summary="done",
        artifact_paths=[],
        verification="ok",
    )

    assert remove_calls == []


def test_create_worktree_stderr_propagated_to_runner_error(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({
            "enabled": True,
            "runner": "zellij",
            "zellij_session": "summonai",
            "project_dir": str(tmp_path),
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(server.pane, "list_panes", lambda _session: [])

    import subprocess as _subprocess

    original_run = _subprocess.run

    def _failing_run(cmd: list[str], **kwargs):
        if cmd[:2] == ["git", "worktree"]:
            exc = _subprocess.CalledProcessError(1, cmd)
            exc.stderr = "fatal: branch already exists"
            raise exc
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(server.subprocess, "run", _failing_run)

    created = server.task_create(
        title="Stderr test",
        north_star="stderr propagation",
        purpose="verify stderr in runner_error",
        acceptance_criteria=["stderr in runner_error"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
        needs_worktree=True,
    )

    assert created["runner_started"] is False
    assert created["runner_error"] is not None
    assert "fatal: branch already exists" in created["runner_error"]


def test_task_resume_recreates_worktree_when_missing(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({
            "enabled": True,
            "runner": "zellij",
            "zellij_session": "summonai",
            "project_dir": str(tmp_path),
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))

    created = server.task_create(
        title="Resume worktree",
        north_star="worktree recovery",
        purpose="verify worktree recreation on resume",
        acceptance_criteria=["worktree recreated"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
        needs_worktree=True,
    )
    task_id = created["task_id"]

    worktree_calls: list[tuple[str, str]] = []

    def _fake_create_worktree(project_dir: str, tid: str) -> Path:
        worktree_calls.append((project_dir, tid))
        wt = Path(project_dir) / ".worktrees" / tid
        wt.mkdir(parents=True, exist_ok=True)
        return wt

    monkeypatch.setattr(server, "_create_worktree", _fake_create_worktree)
    monkeypatch.setattr(server.pane, "list_panes", lambda _session: [])
    monkeypatch.setattr(server.pane, "create_tab", lambda _session, name, cwd=None: "terminal_77")
    monkeypatch.setattr(server.pane, "go_to_tab", lambda _session, _tab_name: None)

    sent_payloads: list[str] = []
    monkeypatch.setattr(
        server.pane,
        "send_text",
        lambda _session, _pane_id, text: sent_payloads.append(text),
    )
    monkeypatch.setattr(
        server,
        "_wait_for_any_output",
        lambda _session, _pane_id, timeout=30.0, interval=0.5: "executor >",
    )

    result = server.task_resume(task_id=task_id, actor_id="interface")

    assert result["resumed"] is True
    assert len(worktree_calls) == 1
    assert worktree_calls[0][1] == task_id

    worktree_path = str(Path(str(tmp_path)) / ".worktrees" / task_id)
    launch_cmd = sent_payloads[0]
    assert f"SUMMONAI_WORKTREE_PATH={worktree_path}" in launch_cmd


def test_task_resume_skips_worktree_creation_when_exists(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({
            "enabled": True,
            "runner": "zellij",
            "zellij_session": "summonai",
            "project_dir": str(tmp_path),
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))

    created = server.task_create(
        title="Resume existing worktree",
        north_star="worktree skip",
        purpose="verify worktree not recreated when exists",
        acceptance_criteria=["worktree not recreated"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
        needs_worktree=True,
    )
    task_id = created["task_id"]

    existing_worktree = tmp_path / ".worktrees" / task_id
    existing_worktree.mkdir(parents=True, exist_ok=True)

    worktree_calls: list[tuple[str, str]] = []

    def _fake_create_worktree(project_dir: str, tid: str) -> Path:
        worktree_calls.append((project_dir, tid))
        return Path(project_dir) / ".worktrees" / tid

    monkeypatch.setattr(server, "_create_worktree", _fake_create_worktree)
    monkeypatch.setattr(server.pane, "list_panes", lambda _session: [])
    monkeypatch.setattr(server.pane, "create_tab", lambda _session, name, cwd=None: "terminal_78")
    monkeypatch.setattr(server.pane, "go_to_tab", lambda _session, _tab_name: None)

    sent_payloads: list[str] = []
    monkeypatch.setattr(
        server.pane,
        "send_text",
        lambda _session, _pane_id, text: sent_payloads.append(text),
    )
    monkeypatch.setattr(
        server,
        "_wait_for_any_output",
        lambda _session, _pane_id, timeout=30.0, interval=0.5: "executor >",
    )

    result = server.task_resume(task_id=task_id, actor_id="interface")

    assert result["resumed"] is True
    assert worktree_calls == []

    worktree_path = str(existing_worktree)
    launch_cmd = sent_payloads[0]
    assert f"SUMMONAI_WORKTREE_PATH={worktree_path}" in launch_cmd


# ── _cleanup_panes_without_tasks tests ────────────────────────────────────────

def test_cleanup_panes_without_tasks_closes_orphan_hex_pane(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pane named task-abc12345 (old 8-char hex) with no DB record → closed."""
    closed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        server.pane,
        "list_panes",
        lambda _session: [{"pane_id": "terminal_10", "name": "task-abc12345"}],
    )
    monkeypatch.setattr(server.pane, "close_pane", lambda s, p: closed.append((s, p)))

    with server.get_db() as conn:
        server._cleanup_panes_without_tasks("summonai", conn)

    assert closed == [("summonai", "terminal_10")]


def test_cleanup_panes_without_tasks_closes_orphan_numeric_pane(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pane named task-099 (new 3-digit sequential) with no DB record → closed."""
    closed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        server.pane,
        "list_panes",
        lambda _session: [{"pane_id": "terminal_20", "name": "task-099"}],
    )
    monkeypatch.setattr(server.pane, "close_pane", lambda s, p: closed.append((s, p)))

    with server.get_db() as conn:
        server._cleanup_panes_without_tasks("summonai", conn)

    assert closed == [("summonai", "terminal_20")]


def test_cleanup_panes_without_tasks_keeps_pane_with_db_record(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pane whose task_id exists in DB → must NOT be closed."""
    created = server.task_create(
        title="Active task",
        north_star="keep pane",
        purpose="pane should survive cleanup",
        acceptance_criteria=["pane not closed"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
    )
    task_id = created["task_id"]  # e.g. "001"

    closed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        server.pane,
        "list_panes",
        lambda _session: [{"pane_id": "terminal_30", "name": f"task-{task_id}"}],
    )
    monkeypatch.setattr(server.pane, "close_pane", lambda s, p: closed.append((s, p)))

    with server.get_db() as conn:
        server._cleanup_panes_without_tasks("summonai", conn)

    assert closed == []


def test_cleanup_panes_without_tasks_best_effort_survives_close_error(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """close_pane failure must not propagate — best-effort."""
    monkeypatch.setattr(
        server.pane,
        "list_panes",
        lambda _session: [{"pane_id": "terminal_40", "name": "task-deadbeef"}],
    )
    monkeypatch.setattr(server.pane, "close_pane", lambda _s, _p: (_ for _ in ()).throw(RuntimeError("fail")))

    with server.get_db() as conn:
        server._cleanup_panes_without_tasks("summonai", conn)  # must not raise


def test_cleanup_panes_without_tasks_called_by_get_db(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """get_db() must trigger _cleanup_panes_without_tasks when runner is enabled."""
    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({"enabled": True, "runner": "zellij", "zellij_session": "summonai"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))

    closed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        server.pane,
        "list_panes",
        lambda _session: [{"pane_id": "terminal_50", "name": "task-01234567"}],
    )
    monkeypatch.setattr(server.pane, "close_pane", lambda s, p: closed.append((s, p)))

    server.get_db()

    assert closed == [("summonai", "terminal_50")]


def test_task_update_sets_completed_at_on_done(isolated_db: Path) -> None:
    created = server.task_create(
        title="completed_at done test",
        north_star="completed_at correctness",
        purpose="verify completed_at is set when status=done",
        acceptance_criteria=["completed_at non-null after done transition"],
        project="summonai-task",
        priority="medium",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]
    with server.get_db() as conn:
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))

    before = server.task_get(task_id)["task"]
    assert before["completed_at"] is None

    result = server.task_update(task_id=task_id, status="done")
    assert result["task"]["completed_at"] is not None

    from datetime import datetime, timezone
    completed_at = datetime.fromisoformat(result["task"]["completed_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    assert abs((now - completed_at).total_seconds()) < 5

    detail = server.task_get(task_id)["task"]
    assert detail["completed_at"] is not None
    assert detail["completed_at"] == result["task"]["completed_at"]


def test_task_cancel_sets_completed_at(isolated_db: Path) -> None:
    created = server.task_create(
        title="completed_at cancel test",
        north_star="completed_at correctness",
        purpose="verify completed_at is set when task is cancelled",
        acceptance_criteria=["completed_at non-null after cancel"],
        project="summonai-task",
        priority="medium",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]

    result = server.task_cancel(task_id=task_id, reason="test cancel")
    assert result["task"]["completed_at"] is not None

    from datetime import datetime, timezone
    completed_at = datetime.fromisoformat(result["task"]["completed_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    assert abs((now - completed_at).total_seconds()) < 5

    detail = server.task_get(task_id)["task"]
    assert detail["completed_at"] is not None
    assert detail["completed_at"] == result["task"]["completed_at"]


def test_task_update_does_not_set_completed_at_for_non_terminal_status(isolated_db: Path) -> None:
    created = server.task_create(
        title="completed_at non-terminal test",
        north_star="completed_at correctness",
        purpose="verify completed_at stays null for non-terminal transitions",
        acceptance_criteria=["completed_at remains null for in_progress"],
        project="summonai-task",
        priority="low",
        creator_role="interface",
        assignee_role="executor",
    )
    task_id = created["task_id"]

    result = server.task_update(task_id=task_id, status="in_progress")
    assert result["task"]["completed_at"] is None

    detail = server.task_get(task_id)["task"]
    assert detail["completed_at"] is None


# ── bloom_level / executor model selection tests ──────────────────────────────

SAMPLE_TIERS = [
    {"executor": "haiku", "model": "claude-haiku-4-5", "max_bloom": 3, "cost_group": "low"},
    {"executor": "sonnet", "model": "claude-sonnet-4-6", "max_bloom": 5, "cost_group": "medium"},
    {"executor": "opus", "model": "claude-opus-4-7", "max_bloom": 6, "cost_group": "high"},
]

SAMPLE_RUNNERS = {
    "default": {"template": "claude --model {model} --dangerously-skip-permissions"},
}


def test_select_model_tier_no_executor_bloom2() -> None:
    tier, is_gap = server._select_model_tier(2, None, SAMPLE_TIERS)
    assert tier is not None
    assert tier["executor"] == "haiku"
    assert is_gap is False


def test_select_model_tier_no_executor_bloom4() -> None:
    tier, is_gap = server._select_model_tier(4, None, SAMPLE_TIERS)
    assert tier is not None
    assert tier["executor"] == "sonnet"
    assert is_gap is False


def test_select_model_tier_no_executor_bloom6() -> None:
    tier, is_gap = server._select_model_tier(6, None, SAMPLE_TIERS)
    assert tier is not None
    assert tier["executor"] == "opus"
    assert is_gap is False


def test_select_model_tier_executor_specified_covers() -> None:
    tier, is_gap = server._select_model_tier(3, "sonnet", SAMPLE_TIERS)
    assert tier is not None
    assert tier["executor"] == "sonnet"
    assert is_gap is False


def test_select_model_tier_no_executor_gap() -> None:
    tier, is_gap = server._select_model_tier(7, None, SAMPLE_TIERS)
    assert tier is not None
    assert tier["executor"] == "opus"
    assert is_gap is True


def test_select_model_tier_executor_specified_gap() -> None:
    tier, is_gap = server._select_model_tier(6, "haiku", SAMPLE_TIERS)
    assert tier is not None
    assert tier["executor"] == "haiku"
    assert is_gap is True


def test_select_model_tier_empty_tiers() -> None:
    tier, is_gap = server._select_model_tier(3, None, [])
    assert tier is None
    assert is_gap is False


def test_build_executor_command_no_tier() -> None:
    cmd = server._build_executor_command(None, {}, False, 3)
    assert cmd == "claude --dangerously-skip-permissions"


def test_build_executor_command_with_tier_and_default_runner() -> None:
    tier = SAMPLE_TIERS[0]  # haiku
    cmd = server._build_executor_command(tier, SAMPLE_RUNNERS, False, 2)
    assert cmd == "claude --model claude-haiku-4-5 --dangerously-skip-permissions"


def test_build_executor_command_gap_emits_warn(capsys: pytest.CaptureFixture) -> None:
    tier = SAMPLE_TIERS[2]  # opus (fallback)
    server._build_executor_command(tier, SAMPLE_RUNNERS, True, 7)
    captured = capsys.readouterr()
    assert "WARN" in captured.err
    assert "bloom_level=7" in captured.err


def test_build_executor_command_per_executor_runner() -> None:
    runners = {
        "haiku": {"template": "claude-fast --model {model}"},
        "default": {"template": "claude --model {model} --dangerously-skip-permissions"},
    }
    tier = SAMPLE_TIERS[0]  # haiku
    cmd = server._build_executor_command(tier, runners, False, 2)
    assert cmd == "claude-fast --model claude-haiku-4-5"


def test_task_create_stores_bloom_level_and_executor(isolated_db: Path) -> None:
    created = server.task_create(
        title="Bloom task",
        north_star="test bloom",
        purpose="store bloom and executor",
        acceptance_criteria=["bloom_level stored", "executor stored"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        bloom_level=5,
        executor="sonnet",
    )
    detail = server.task_get(created["task_id"])
    assert detail["task"]["bloom_level"] == 5
    assert detail["task"]["executor"] == "sonnet"


def test_task_create_default_bloom_level(isolated_db: Path) -> None:
    created = server.task_create(
        title="Default bloom",
        north_star="test",
        purpose="default bloom_level=3",
        acceptance_criteria=["bloom_level defaults to 3"],
        project="summonai-task",
        priority="low",
        creator_role="interface",
    )
    detail = server.task_get(created["task_id"])
    assert detail["task"]["bloom_level"] == 3
    assert detail["task"]["executor"] is None


def test_task_create_rejects_invalid_bloom_level(isolated_db: Path) -> None:
    with pytest.raises(ValueError, match="bloom_level"):
        server.task_create(
            title="Bad bloom",
            north_star="test",
            purpose="invalid bloom_level",
            acceptance_criteria=["rejected"],
            project="summonai-task",
            priority="low",
            creator_role="interface",
            bloom_level=7,
        )


def test_load_executors_config_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    toml_content = b"""
[[capability_tiers]]
executor = "haiku"
model = "claude-haiku-4-5"
max_bloom = 3
cost_group = "low"

[runners.default]
template = "claude --model {model} --dangerously-skip-permissions"
"""
    config_file = tmp_path / "executors.toml"
    config_file.write_bytes(toml_content)
    monkeypatch.setenv("SUMMONAI_EXECUTORS_CONFIG", str(config_file))

    cfg = server._load_executors_config()
    assert len(cfg["capability_tiers"]) == 1
    assert cfg["capability_tiers"][0]["executor"] == "haiku"
    assert cfg["runners"]["default"]["template"].startswith("claude")


def test_load_executors_config_from_project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summonai_dir = tmp_path / ".summonai"
    summonai_dir.mkdir()
    toml_content = b"""
[[capability_tiers]]
executor = "opus"
model = "claude-opus-4-7"
max_bloom = 6
cost_group = "high"
"""
    (summonai_dir / "executors.toml").write_bytes(toml_content)
    monkeypatch.delenv("SUMMONAI_EXECUTORS_CONFIG", raising=False)

    cfg = server._load_executors_config(str(tmp_path))
    assert len(cfg["capability_tiers"]) == 1
    assert cfg["capability_tiers"][0]["executor"] == "opus"


def test_load_executors_config_missing_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SUMMONAI_EXECUTORS_CONFIG", raising=False)
    cfg = server._load_executors_config(str(tmp_path))
    assert cfg["capability_tiers"] == []
    assert cfg["runners"] == {}


def test_spawn_uses_bloom_model_selection(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    toml_content = b"""
[[capability_tiers]]
executor = "haiku"
model = "claude-haiku-4-5"
max_bloom = 3
cost_group = "low"

[[capability_tiers]]
executor = "opus"
model = "claude-opus-4-7"
max_bloom = 6
cost_group = "high"

[runners.default]
template = "claude --model {model} --dangerously-skip-permissions"
"""
    summonai_dir = tmp_path / ".summonai"
    summonai_dir.mkdir()
    (summonai_dir / "executors.toml").write_bytes(toml_content)

    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({
            "enabled": True,
            "runner": "zellij",
            "zellij_session": "summonai",
            "project_dir": str(tmp_path),
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.delenv("SUMMONAI_EXECUTORS_CONFIG", raising=False)
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(server.pane, "list_panes", lambda _session: [])
    monkeypatch.setattr(server.pane, "create_tab", lambda _session, name, cwd=None: "terminal_42")
    monkeypatch.setattr(server.pane, "go_to_tab", lambda _session, _tab_name: None)

    sent_payloads: list[str] = []
    monkeypatch.setattr(
        server.pane, "send_text",
        lambda _session, _pane_id, text: sent_payloads.append(text),
    )
    monkeypatch.setattr(
        server, "_wait_for_any_output",
        lambda _s, _p, timeout=30.0, interval=0.5: "ready >",
    )

    # bloom_level=2 → haiku (max_bloom=3 >= 2, cheapest)
    server.task_create(
        title="Bloom routing test",
        north_star="test",
        purpose="verify model selected by bloom",
        acceptance_criteria=["haiku selected"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
        bloom_level=2,
    )

    assert any("claude-haiku-4-5" in p for p in sent_payloads), f"Expected haiku in: {sent_payloads}"


def test_spawn_bloom_gap_uses_fallback(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    toml_content = b"""
[[capability_tiers]]
executor = "haiku"
model = "claude-haiku-4-5"
max_bloom = 3
cost_group = "low"

[runners.default]
template = "claude --model {model} --dangerously-skip-permissions"
"""
    summonai_dir = tmp_path / ".summonai"
    summonai_dir.mkdir()
    (summonai_dir / "executors.toml").write_bytes(toml_content)

    config = tmp_path / "runner_config.json"
    config.write_text(
        json.dumps({
            "enabled": True,
            "runner": "zellij",
            "zellij_session": "summonai",
            "project_dir": str(tmp_path),
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUMMONAI_TASK_RUNNER_CONFIG", str(config))
    monkeypatch.delenv("SUMMONAI_EXECUTORS_CONFIG", raising=False)
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(server.pane, "list_panes", lambda _session: [])
    monkeypatch.setattr(server.pane, "create_tab", lambda _session, name, cwd=None: "terminal_42")
    monkeypatch.setattr(server.pane, "go_to_tab", lambda _session, _tab_name: None)

    sent_payloads: list[str] = []
    monkeypatch.setattr(
        server.pane, "send_text",
        lambda _session, _pane_id, text: sent_payloads.append(text),
    )
    monkeypatch.setattr(
        server, "_wait_for_any_output",
        lambda _s, _p, timeout=30.0, interval=0.5: "ready >",
    )

    # bloom_level=6 > haiku max_bloom=3 → gap, fallback to haiku (only tier)
    server.task_create(
        title="Gap fallback test",
        north_star="test",
        purpose="verify gap fallback",
        acceptance_criteria=["haiku used as fallback"],
        project="summonai-task",
        priority="high",
        creator_role="interface",
        assignee_role="executor",
        bloom_level=6,
    )

    assert any("claude-haiku-4-5" in p for p in sent_payloads), f"Expected haiku fallback in: {sent_payloads}"


def test_schema_includes_bloom_level_executor(isolated_db: Path) -> None:
    conn = server.get_db()
    try:
        task_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        assert "bloom_level" in task_columns
        assert "executor" in task_columns

        applied_versions = {
            int(row[0])
            for row in conn.execute("SELECT version FROM schema_versions").fetchall()
        }
        assert 6 in applied_versions
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bloom-routing-fix: CLI-tool-name executor semantics
# ---------------------------------------------------------------------------

CLAUDE_TIERS = [
    {"executor": "claude", "model": "claude-haiku-4-5-20251001", "max_bloom": 3, "cost_group": "low"},
    {"executor": "claude", "model": "claude-sonnet-4-6", "max_bloom": 5, "cost_group": "medium"},
    {"executor": "claude", "model": "claude-opus-4-7", "max_bloom": 6, "cost_group": "high"},
]


def test_select_model_tier_same_executor_bloom2_picks_haiku() -> None:
    tier, is_gap = server._select_model_tier(2, "claude", CLAUDE_TIERS)
    assert tier is not None
    assert tier["model"] == "claude-haiku-4-5-20251001"
    assert is_gap is False


def test_select_model_tier_same_executor_bloom4_picks_sonnet() -> None:
    tier, is_gap = server._select_model_tier(4, "claude", CLAUDE_TIERS)
    assert tier is not None
    assert tier["model"] == "claude-sonnet-4-6"
    assert is_gap is False


def test_select_model_tier_same_executor_bloom5_picks_sonnet() -> None:
    tier, is_gap = server._select_model_tier(5, "claude", CLAUDE_TIERS)
    assert tier is not None
    assert tier["model"] == "claude-sonnet-4-6"
    assert is_gap is False


def test_select_model_tier_same_executor_bloom6_picks_opus() -> None:
    tier, is_gap = server._select_model_tier(6, "claude", CLAUDE_TIERS)
    assert tier is not None
    assert tier["model"] == "claude-opus-4-7"
    assert is_gap is False


def test_load_executors_config_reads_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    toml_content = b"""
[[capability_tiers]]
executor = "claude"
model = "claude-sonnet-4-6"
max_bloom = 5

[runners.claude]
template = "claude --model {model} --dangerously-skip-permissions"

[defaults]
bloom_level = 5
executor = "claude"
"""
    config_file = tmp_path / "executors.toml"
    config_file.write_bytes(toml_content)
    monkeypatch.setenv("SUMMONAI_EXECUTORS_CONFIG", str(config_file))

    cfg = server._load_executors_config()
    assert cfg["defaults"]["bloom_level"] == 5
    assert cfg["defaults"]["executor"] == "claude"
    assert cfg["config_loaded"] is True


def test_load_executors_config_missing_config_loaded_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SUMMONAI_EXECUTORS_CONFIG", raising=False)
    cfg = server._load_executors_config(str(tmp_path))
    assert cfg["config_loaded"] is False
    assert cfg["defaults"] == {}


def test_task_create_applies_defaults_bloom_level(
    isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml_content = b"""
[[capability_tiers]]
executor = "claude"
model = "claude-sonnet-4-6"
max_bloom = 5

[defaults]
bloom_level = 5
"""
    config_file = tmp_path / "executors.toml"
    config_file.write_bytes(toml_content)
    monkeypatch.setenv("SUMMONAI_EXECUTORS_CONFIG", str(config_file))

    created = server.task_create(
        title="Defaults test",
        north_star="test defaults",
        purpose="verify bloom_level default from config",
        acceptance_criteria=["bloom_level matches config default"],
        project="summonai-task",
        priority="low",
        creator_role="interface",
    )
    detail = server.task_get(created["task_id"])
    assert detail["task"]["bloom_level"] == 5


def test_task_create_applies_defaults_executor(
    isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml_content = b"""
[[capability_tiers]]
executor = "claude"
model = "claude-haiku-4-5-20251001"
max_bloom = 3

[defaults]
executor = "claude"
"""
    config_file = tmp_path / "executors.toml"
    config_file.write_bytes(toml_content)
    monkeypatch.setenv("SUMMONAI_EXECUTORS_CONFIG", str(config_file))

    created = server.task_create(
        title="Executor defaults test",
        north_star="test executor default",
        purpose="verify executor default from config",
        acceptance_criteria=["executor matches config default"],
        project="summonai-task",
        priority="low",
        creator_role="interface",
    )
    detail = server.task_get(created["task_id"])
    assert detail["task"]["executor"] == "claude"


def test_task_create_rejects_unknown_executor_when_config_loaded(
    isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml_content = b"""
[[capability_tiers]]
executor = "claude"
model = "claude-haiku-4-5-20251001"
max_bloom = 3
"""
    config_file = tmp_path / "executors.toml"
    config_file.write_bytes(toml_content)
    monkeypatch.setenv("SUMMONAI_EXECUTORS_CONFIG", str(config_file))

    with pytest.raises(ValueError, match="Unknown executor.*codex"):
        server.task_create(
            title="Unknown executor",
            north_star="test",
            purpose="reject unknown executor",
            acceptance_criteria=["ValueError raised"],
            project="summonai-task",
            priority="low",
            creator_role="interface",
            executor="codex",
        )


def test_task_create_unknown_executor_error_lists_available(
    isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml_content = b"""
[[capability_tiers]]
executor = "claude"
model = "claude-haiku-4-5-20251001"
max_bloom = 3

[[capability_tiers]]
executor = "opencode"
model = "gpt-4o"
max_bloom = 5
"""
    config_file = tmp_path / "executors.toml"
    config_file.write_bytes(toml_content)
    monkeypatch.setenv("SUMMONAI_EXECUTORS_CONFIG", str(config_file))

    with pytest.raises(ValueError) as exc_info:
        server.task_create(
            title="Unknown executor",
            north_star="test",
            purpose="error message lists available executors",
            acceptance_criteria=["Available executors listed"],
            project="summonai-task",
            priority="low",
            creator_role="interface",
            executor="codex",
        )
    msg = str(exc_info.value)
    assert "claude" in msg
    assert "opencode" in msg


def test_task_create_accepts_unknown_executor_without_config(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SUMMONAI_EXECUTORS_CONFIG", raising=False)

    created = server.task_create(
        title="Legacy executor",
        north_star="test",
        purpose="accept arbitrary executor when no config",
        acceptance_criteria=["no ValueError raised"],
        project="summonai-task",
        priority="low",
        creator_role="interface",
        executor="some-unknown-tool",
    )
    detail = server.task_get(created["task_id"])
    assert detail["task"]["executor"] == "some-unknown-tool"


def test_task_create_explicit_bloom3_not_overridden_by_defaults(
    isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicitly passing bloom_level=3 must NOT be overridden by defaults.bloom_level=5."""
    toml_content = b"""
[[capability_tiers]]
executor = "claude"
model = "claude-sonnet-4-6"
max_bloom = 5

[defaults]
bloom_level = 5
"""
    config_file = tmp_path / "executors.toml"
    config_file.write_bytes(toml_content)
    monkeypatch.setenv("SUMMONAI_EXECUTORS_CONFIG", str(config_file))

    created = server.task_create(
        title="Explicit bloom=3",
        north_star="test",
        purpose="explicit bloom_level=3 must not be overridden by defaults",
        acceptance_criteria=["bloom_level stays 3"],
        project="summonai-task",
        priority="low",
        creator_role="interface",
        bloom_level=3,
    )
    detail = server.task_get(created["task_id"])
    assert detail["task"]["bloom_level"] == 3, (
        "Explicit bloom_level=3 was silently overridden by defaults.bloom_level"
    )


def test_task_create_unspecified_bloom_gets_defaults(
    isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting bloom_level (None sentinel) must pick up defaults.bloom_level."""
    toml_content = b"""
[[capability_tiers]]
executor = "claude"
model = "claude-sonnet-4-6"
max_bloom = 5

[defaults]
bloom_level = 5
"""
    config_file = tmp_path / "executors.toml"
    config_file.write_bytes(toml_content)
    monkeypatch.setenv("SUMMONAI_EXECUTORS_CONFIG", str(config_file))

    created = server.task_create(
        title="Unspecified bloom",
        north_star="test",
        purpose="omitting bloom_level should use defaults.bloom_level",
        acceptance_criteria=["bloom_level matches config default"],
        project="summonai-task",
        priority="low",
        creator_role="interface",
    )
    detail = server.task_get(created["task_id"])
    assert detail["task"]["bloom_level"] == 5
