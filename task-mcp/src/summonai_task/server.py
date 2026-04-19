"""SummonAI Task MCP server."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from mcp.server.fastmcp import FastMCP

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[no-redef]
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

from summonai_task import pane

DEFAULT_DB_PATH = os.environ.get(
    "SUMMONAI_TASK_DB",
    str(Path(__file__).parent / "db" / "summonai_task.db"),
)
MIGRATIONS_DIR = Path(__file__).parent / "db" / "migrations"
MIGRATION_PATTERN = re.compile(r"^V(?P<version>\d{3})_[A-Za-z0-9_]+\.sql$")

mcp = FastMCP("summonai-task-mcp")

VALID_STATUSES = {"pending", "assigned", "in_progress", "review", "done", "redo", "cancelled"}
ARTIFACTS_DIR_PREFIX = ".summonai/artifacts/"


def _validate_artifact_paths(artifact_paths: list[str]) -> None:
    for path in artifact_paths:
        if not path.startswith(ARTIFACTS_DIR_PREFIX):
            raise ValueError(
                f"artifact_paths must be under {ARTIFACTS_DIR_PREFIX!r}, got: {path!r}"
            )


ALLOWED_TRANSITIONS = {
    "pending": {"assigned", "cancelled"},
    "assigned": {"pending", "in_progress", "review", "cancelled"},
    "in_progress": {"assigned", "review", "cancelled"},
    "review": {"in_progress", "done", "redo", "cancelled"},
    "done": {"redo"},
    "redo": set(),
    "cancelled": set(),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _db_path() -> str:
    return os.environ.get("SUMMONAI_TASK_DB", DEFAULT_DB_PATH)

def _load_runner_config() -> dict:
    file_config: dict = {}
    config_path = os.environ.get("SUMMONAI_TASK_RUNNER_CONFIG")
    if config_path:
        raw = Path(config_path).read_text(encoding="utf-8")
        file_config = json.loads(raw)
        if not isinstance(file_config, dict):
            raise ValueError("SUMMONAI_TASK_RUNNER_CONFIG must be a JSON object")

    runner = str(file_config.get("runner") or os.environ.get("SUMMONAI_TASK_RUNNER") or "").strip().lower()

    project_dir = str(
        file_config.get("project_dir")
        or os.environ.get("SUMMONAI_TASK_RUNNER_PROJECT_DIR")
        or os.getcwd()
    ).strip()
    zellij_session_raw = file_config.get("zellij_session")
    if zellij_session_raw is None:
        zellij_session_raw = os.environ.get("ZELLIJ_SESSION_NAME")
    zellij_session = str(zellij_session_raw).strip() if zellij_session_raw is not None else ""

    enabled_raw = file_config.get("enabled")
    if enabled_raw is None:
        enabled_raw = os.environ.get("SUMMONAI_TASK_RUNNER_ENABLED")
    if enabled_raw is None:
        enabled = runner == "zellij"
    elif isinstance(enabled_raw, bool):
        enabled = enabled_raw
    else:
        enabled = str(enabled_raw).strip().lower() in {"1", "true", "yes", "on"}

    max_concurrent_raw = file_config.get("max_concurrent_executors")
    if max_concurrent_raw is None:
        max_concurrent_raw = os.environ.get("SUMMONAI_MAX_CONCURRENT_EXECUTORS")
    try:
        max_concurrent_executors = max(1, int(max_concurrent_raw)) if max_concurrent_raw is not None else 5
    except (ValueError, TypeError):
        max_concurrent_executors = 5

    return {
        "enabled": enabled,
        "runner": runner,
        "project_dir": project_dir,
        "zellij_session": zellij_session or None,
        "env": file_config.get("env") or {},
        "max_concurrent_executors": max_concurrent_executors,
    }


def _load_executors_config(project_dir: str | None = None) -> dict:
    """Load executors TOML config from SUMMONAI_EXECUTORS_CONFIG or {project_dir}/.summonai/executors.toml."""
    path_str = os.environ.get("SUMMONAI_EXECUTORS_CONFIG")
    if not path_str and project_dir:
        candidate = Path(project_dir) / ".summonai" / "executors.toml"
        if candidate.exists():
            path_str = str(candidate)

    if not path_str:
        return {"capability_tiers": [], "runners": {}, "defaults": {}, "config_loaded": False}

    try:
        with open(path_str, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return {"capability_tiers": [], "runners": {}, "defaults": {}, "config_loaded": False}

    tiers = data.get("capability_tiers", [])
    if not isinstance(tiers, list):
        tiers = []
    runners = data.get("runners", {})
    if not isinstance(runners, dict):
        runners = {}
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}
    return {"capability_tiers": tiers, "runners": runners, "defaults": defaults, "config_loaded": True}


def _select_model_tier(
    bloom_level: int,
    executor: str | None,
    tiers: list[dict],
) -> tuple[dict | None, bool]:
    """Select a capability tier for the given bloom_level and optional executor filter.

    Returns (tier, is_gap) where is_gap=True means no tier fully covered the bloom_level.
    """
    if not tiers:
        return None, False

    candidates = [t for t in tiers if t.get("executor") == executor] if executor else list(tiers)

    covering = [t for t in candidates if isinstance(t.get("max_bloom"), int) and t["max_bloom"] >= bloom_level]
    if covering:
        return min(covering, key=lambda t: t["max_bloom"]), False

    # Coverage gap
    fallback_pool = candidates if candidates else tiers
    fallback = max(fallback_pool, key=lambda t: t.get("max_bloom", 0))
    return fallback, True


def _build_executor_command(
    tier: dict | None,
    runners: dict,
    is_gap: bool,
    bloom_level: int,
) -> str:
    """Build the shell command to launch an executor, substituting {model} from the selected tier."""
    if tier is None:
        return "claude --dangerously-skip-permissions"

    if is_gap:
        print(
            f"WARN: bloom_level={bloom_level} exceeds max_bloom for all matching tiers; "
            f"falling back to executor={tier.get('executor')!r} (max_bloom={tier.get('max_bloom')})",
            file=sys.stderr,
        )

    model = tier.get("model", "")
    executor_name = tier.get("executor", "")
    runner_cfg = runners.get(executor_name) or runners.get("default")
    if runner_cfg and isinstance(runner_cfg, dict) and "template" in runner_cfg:
        return runner_cfg["template"].replace("{model}", model)
    if model:
        return f"claude --model {model} --dangerously-skip-permissions"
    return "claude --dangerously-skip-permissions"


def _has_prompt_marker(output: str) -> bool:
    stripped = pane._strip_ansi(output)
    lines = stripped.splitlines()
    return any(pane.PROMPT_MARKER_PATTERN.search(line) for line in lines) or any(
        pane.CODEX_PROMPT_PATTERN.search(line) for line in lines
    )


def _wait_for_any_output(session: str, pane_id: str, timeout: float = 30.0, interval: float = 0.5) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        output = pane.read_output(session, pane_id, lines=80)
        if _has_prompt_marker(output):
            return output
        time.sleep(interval)
    raise TimeoutError(f"pane {pane_id} prompt marker was not detected within {int(timeout)}s")


_TASK_PANE_PATTERN = re.compile(r"^task-([0-9a-f]{8}|[0-9]{3,})$")


def _cleanup_panes_without_tasks(session: str, conn: sqlite3.Connection) -> None:
    """Close zellij panes named task-XXXX that have no corresponding DB record."""
    existing_ids = {
        row["id"]
        for row in conn.execute("SELECT id FROM tasks").fetchall()
    }
    try:
        panes = pane.list_panes(session)
    except Exception:
        return
    for entry in panes:
        name = str(entry.get("name") or entry.get("title") or "")
        match = _TASK_PANE_PATTERN.match(name)
        if not match:
            continue
        task_id = match.group(1)
        if task_id not in existing_ids:
            pane_id = str(
                entry.get("pane_id") or entry.get("paneId") or entry.get("id") or ""
            )
            if pane_id:
                try:
                    pane.close_pane(session, pane_id)
                except Exception:
                    pass


def _cleanup_orphan_panes(conn: sqlite3.Connection, session: str) -> None:
    active_pane_ids = {
        str(pane_id)
        for pane_id in (entry.get("pane_id") or entry.get("paneId") or entry.get("id") for entry in pane.list_panes(session))
        if pane_id
    }
    stale_rows = conn.execute(
        "SELECT id, pane_id FROM tasks WHERE pane_id IS NOT NULL AND pane_id <> ''"
    ).fetchall()
    for row in stale_rows:
        if row["pane_id"] in active_pane_ids:
            continue
        conn.execute("UPDATE tasks SET pane_id = NULL, updated_at = ? WHERE id = ?", (_utc_now(), row["id"]))
        _log_event(
            conn,
            task_id=row["id"],
            event_type="pane_orphan_cleanup",
            actor_id="system",
            payload={"stale_pane_id": row["pane_id"]},
        )


def _active_pane_ids(session: str) -> set[str]:
    return {
        str(pane_id)
        for pane_id in (entry.get("pane_id") or entry.get("paneId") or entry.get("id") for entry in pane.list_panes(session))
        if pane_id
    }


def _executor_start_prompt(task_id: str) -> str:
    return (
        f'task_id="{task_id}" のタスクを開始せよ。'
        f'task_get(task_id="{task_id}") でタスク詳細を確認し、'
        "acceptance_criteria を満たして task_complete を呼べ。"
    )


def _executor_resume_prompt(task_id: str) -> str:
    return (
        f'task_id="{task_id}" のタスクを再開せよ。'
        f'task_get(task_id="{task_id}") で現在の要件を確認し、'
        "まず git status と既存成果物を確認して不足分のみ実装せよ。"
        "acceptance_criteria を満たしたら task_complete を呼べ。"
    )


EXECUTOR_TAB_NAME = "executors"
INTERFACE_TAB_NAME = "interface"


def _worktree_path(project_dir: str, task_id: str) -> Path:
    return Path(project_dir) / ".worktrees" / task_id


def _create_worktree(project_dir: str, task_id: str) -> Path:
    worktree = _worktree_path(project_dir, task_id)
    branch = f"feature/{task_id}"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), "-b", branch, "origin/main"],
        cwd=project_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return worktree


def _remove_worktree(project_dir: str, task_id: str) -> None:
    worktree = _worktree_path(project_dir, task_id)
    if not worktree.exists():
        return
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree)],
        cwd=project_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def _spawn_executor_pane(
    session: str,
    task_id: str,
    prompt: str,
    extra_env: dict[str, str] | None = None,
    launch_command: str = "claude --dangerously-skip-permissions",
) -> str:
    pane_id: str | None = None
    try:
        pane_id = pane.create_tab(session, f"task-{task_id}")
        # Restore focus to the interface tab so it is not displaced by the new
        # executor tab.  Best-effort: if the tab does not exist yet (e.g. old
        # layout without tab name), skip rather than aborting the spawn.
        try:
            pane.go_to_tab(session, INTERFACE_TAB_NAME)
        except pane.ZellijError:
            pass
        # Persist task_id by pane identifier so the pane session_start hook can
        # resolve which task this executor pane belongs to.
        task_id_file = Path(tempfile.gettempdir()) / f"summonai_pane_{pane_id}.task_id"
        task_id_file.write_text(task_id, encoding="utf-8")
        _wait_for_any_output(session, pane_id, timeout=30.0)
        env_prefix = ""
        if extra_env:
            env_prefix = " ".join(f"{k}={v}" for k, v in extra_env.items()) + " "
        pane.send_text(session, pane_id, f"{env_prefix}{launch_command}")
        _wait_for_any_output(session, pane_id, timeout=60.0)
        pane.send_text(session, pane_id, prompt)
        return pane_id
    except Exception:
        if pane_id:
            try:
                pane.close_pane(session, pane_id)
            except Exception:
                pass
            task_id_file_cleanup = Path(tempfile.gettempdir()) / f"summonai_pane_{pane_id}.task_id"
            task_id_file_cleanup.unlink(missing_ok=True)
        raise


def _spawn_task_runner_if_configured(conn: sqlite3.Connection, task_id: str) -> tuple[bool, str | None]:
    config = _load_runner_config()
    if not config["enabled"]:
        return False, None

    if config["runner"] not in {"zellij", "claude"}:
        return False, None

    session = config["zellij_session"]
    if not session:
        raise ValueError("Task runner enabled but zellij_session is not configured")

    _cleanup_orphan_panes(conn, session)
    _cleanup_panes_without_tasks(session, conn)

    # Guard against cascade spawning: count ALL pending executor work (with or
    # without a pane_id) so that tasks queued in a tight loop are included before
    # their pane_id is written back.  Exclude the current task itself because it
    # is already 'assigned' at this point.
    max_concurrent = config["max_concurrent_executors"]
    active_count = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status IN ('assigned', 'in_progress') AND id != ?",
        (task_id,),
    ).fetchone()[0]
    if active_count >= max_concurrent:
        _log_event(
            conn,
            task_id=task_id,
            event_type="spawn_skipped",
            actor_id="system",
            payload={"reason": "max_concurrent_executors", "active": active_count, "limit": max_concurrent},
        )
        return False, None

    task_row = _get_task_row(conn, task_id)
    extra_env: dict[str, str] = {}
    project_dir = config["project_dir"]
    if task_row["needs_worktree"] and project_dir:
        worktree = _create_worktree(project_dir, task_id)
        extra_env["SUMMONAI_WORKTREE_PATH"] = str(worktree)

    executors_cfg = _load_executors_config(project_dir)
    tiers = executors_cfg["capability_tiers"]
    runners = executors_cfg["runners"]
    bloom_level = int(task_row["bloom_level"] if task_row["bloom_level"] is not None else 3)
    executor_name = task_row["executor"] or None
    tier, is_gap = _select_model_tier(bloom_level, executor_name, tiers)
    launch_command = _build_executor_command(tier, runners, is_gap, bloom_level)

    pane_id = _spawn_executor_pane(
        session, task_id, _executor_start_prompt(task_id),
        extra_env=extra_env or None,
        launch_command=launch_command,
    )

    conn.execute("UPDATE tasks SET pane_id = ?, updated_at = ? WHERE id = ?", (pane_id, _utc_now(), task_id))
    _log_event(
        conn,
        task_id=task_id,
        event_type="pane_started",
        actor_id="system",
        payload={"pane_id": pane_id, "session": session},
    )
    return True, pane_id


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


def _load_migration_files() -> list[tuple[int, Path]]:
    files: list[tuple[int, Path]] = []
    if not MIGRATIONS_DIR.exists():
        return files
    for path in MIGRATIONS_DIR.iterdir():
        if not path.is_file():
            continue
        match = MIGRATION_PATTERN.match(path.name)
        if not match:
            continue
        files.append((int(match.group("version")), path))
    files.sort(key=lambda item: item[0])
    return files


def _ensure_schema_versions_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_versions (
            version INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            checksum TEXT NOT NULL
        )
        """
    )


def _fetch_applied_migrations(conn: sqlite3.Connection) -> dict[int, str]:
    rows = conn.execute("SELECT version, checksum FROM schema_versions").fetchall()
    return {int(row["version"]): row["checksum"] for row in rows}


def ensure_schema(conn: sqlite3.Connection) -> None:
    _ensure_schema_versions_table(conn)
    migrations = _load_migration_files()
    applied = _fetch_applied_migrations(conn)

    for version, migration_file in migrations:
        checksum = hashlib.sha256(migration_file.read_bytes()).hexdigest()
        if version in applied:
            if applied[version] != checksum:
                raise RuntimeError(f"Migration checksum mismatch for V{version:03d}")
            continue

        script = migration_file.read_text(encoding="utf-8")
        statements = list(_iter_sql_statements(script))
        disable_fk = any(stmt.strip().lower() == "pragma foreign_keys=off;" for stmt in statements)
        enable_fk = any(stmt.strip().lower() == "pragma foreign_keys=on;" for stmt in statements)
        tx_statements = [
            stmt
            for stmt in statements
            if stmt.strip().lower() not in {"pragma foreign_keys=off;", "pragma foreign_keys=on;"}
        ]

        if disable_fk:
            conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        try:
            for statement in tx_statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_versions(version, filename, applied_at, checksum) VALUES (?, ?, ?, ?)",
                (version, migration_file.name, _utc_now(), checksum),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if disable_fk or enable_fk:
                conn.execute("PRAGMA foreign_keys=ON")


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(_db_path())
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    ensure_schema(db)
    try:
        config = _load_runner_config()
        if config["enabled"] and config["zellij_session"]:
            _cleanup_panes_without_tasks(config["zellij_session"], db)
    except Exception:
        pass
    return db



def _ensure_transition(current: str, target: str) -> None:
    if target not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {target}")
    if target == current:
        return
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"Invalid transition: {current} -> {target}")


def _task_row_to_dict(row: sqlite3.Row) -> dict:
    payload = dict(row)
    payload["acceptance_criteria"] = json.loads(payload.pop("acceptance_criteria_json"))
    payload["metadata"] = json.loads(payload.pop("metadata_json"))
    destructive = payload.pop("destructive_safety_json")
    payload["destructive_safety"] = json.loads(destructive) if destructive else None
    payload["batch1_qc_required"] = bool(payload["batch1_qc_required"])
    payload["needs_worktree"] = bool(payload["needs_worktree"])
    return payload


def _log_event(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    event_type: str,
    actor_id: str,
    payload: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO task_events(task_id, event_type, actor_id, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (task_id, event_type, actor_id, json.dumps(payload, ensure_ascii=True), _utc_now()),
    )


def _get_task_row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"Task not found: {task_id}")
    return row


def _close_pane_for_review_exit(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    pane_id: str | None,
    session: str | None,
    actor_id: str,
) -> None:
    if not pane_id or not session:
        return
    try:
        pane.close_pane(session, pane_id)
        _log_event(
            conn,
            task_id=task_id,
            event_type="pane_closed",
            actor_id=actor_id,
            payload={"pane_id": pane_id},
        )
    except Exception as exc:
        _log_event(
            conn,
            task_id=task_id,
            event_type="pane_close_failed",
            actor_id=actor_id,
            payload={"pane_id": pane_id, "error": str(exc)},
        )


@mcp.tool()
def task_create(
    title: str,
    north_star: str,
    purpose: str,
    acceptance_criteria: list[str],
    project: str,
    priority: str,
    creator_role: str,
    assignee_id: str | None = None,
    assignee_role: str | None = None,
    assignment_role: str | None = None,
    parent_task_id: str | None = None,
    redo_of: str | None = None,
    batch1_qc_required: bool = False,
    needs_worktree: bool = False,
    bloom_level: int | None = None,
    executor: str | None = None,
    destructive_safety: dict | None = None,
    metadata: dict | None = None,
    actor_id: str = "system",
) -> dict:
    if not acceptance_criteria:
        raise ValueError("acceptance_criteria must not be empty")

    # Apply [defaults] from executors.toml when caller omitted bloom_level / executor (None sentinel).
    try:
        _project_dir: str | None = _load_runner_config().get("project_dir")
    except Exception:
        _project_dir = None
    _ecfg = _load_executors_config(_project_dir)
    _cfg_defaults = _ecfg.get("defaults", {})
    if bloom_level is None:
        bloom_level = int(_cfg_defaults["bloom_level"]) if "bloom_level" in _cfg_defaults else 3
    if executor is None and "executor" in _cfg_defaults:
        executor = str(_cfg_defaults["executor"])

    if not (1 <= bloom_level <= 6):
        raise ValueError(f"bloom_level must be between 1 and 6, got: {bloom_level}")

    # Reject unknown executors when config was loaded (legacy env without config passes through).
    if executor is not None and _ecfg.get("config_loaded"):
        _known = {t["executor"] for t in _ecfg["capability_tiers"] if t.get("executor")}
        if _known and executor not in _known:
            raise ValueError(
                f"Unknown executor: {executor!r}. "
                f"Available executors: {sorted(_known)}"
            )

    now = _utc_now()
    metadata = metadata or {}
    status = "assigned" if (assignee_id or assignee_role) else "pending"
    runner_started = False
    runner_error: str | None = None

    with get_db() as conn:
        task_number_row = conn.execute(
            "SELECT COALESCE(MAX(task_number), 0) + 1 AS next_num FROM tasks",
        ).fetchone()
        task_number = task_number_row["next_num"]
        task_id = f"{task_number:03d}"

        root_task_id = task_id
        if parent_task_id:
            parent = _get_task_row(conn, parent_task_id)
            root_task_id = parent["root_task_id"] or parent["id"]
        elif redo_of:
            original = _get_task_row(conn, redo_of)
            root_task_id = original["root_task_id"] or original["id"]
        conn.execute(
            """
            INSERT INTO tasks(
                id, title, north_star, purpose, acceptance_criteria_json,
                project, priority, status, creator_role, assignee_id,
                assignee_role, assignment_role, parent_task_id, root_task_id,
                redo_of, pane_id, batch1_qc_required, needs_worktree, bloom_level, executor,
                destructive_safety_json, purpose_gap,
                metadata_json, created_at, updated_at, completed_at, task_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                title,
                north_star,
                purpose,
                json.dumps(acceptance_criteria, ensure_ascii=True),
                project,
                priority,
                status,
                creator_role,
                assignee_id,
                assignee_role,
                assignment_role,
                parent_task_id,
                root_task_id,
                redo_of,
                None,
                int(batch1_qc_required),
                int(needs_worktree),
                bloom_level,
                executor,
                json.dumps(destructive_safety, ensure_ascii=True) if destructive_safety else None,
                None,
                json.dumps(metadata, ensure_ascii=True),
                now,
                now,
                None,
                task_number,
            ),
        )
        _log_event(
            conn,
            task_id=task_id,
            event_type="create",
            actor_id=actor_id,
            payload={"status": status, "project": project},
        )
        # Persist creation before any runner startup. Spawning a pane can take
        # tens of seconds and must not hold a write transaction open.
        conn.commit()

    if status == "assigned":
        try:
            with get_db() as conn:
                runner_started, _ = _spawn_task_runner_if_configured(conn, task_id)
        except Exception as exc:
            runner_error = str(exc)
            runner_started = False

    with get_db() as conn:
        row = _get_task_row(conn, task_id)

    data = _task_row_to_dict(row)
    return {
        "task_id": task_id,
        "status": data["status"],
        "created_at": data["created_at"],
        "runner_started": runner_started,
        "runner_error": runner_error,
    }


@mcp.tool()
def task_get(task_id: str, include_history: bool = True) -> dict:
    with get_db() as conn:
        task_row = _get_task_row(conn, task_id)
        task = _task_row_to_dict(task_row)
        response = {"task": task}
        if include_history:
            events = conn.execute(
                "SELECT id, task_id, event_type, actor_id, payload_json, created_at FROM task_events WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
            response["events"] = [
                {
                    "id": row["id"],
                    "task_id": row["task_id"],
                    "event_type": row["event_type"],
                    "actor_id": row["actor_id"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in events
            ]
            reviews = conn.execute(
                "SELECT id, task_id, reviewer_id, decision, acceptance_results_json, notes, created_at FROM task_reviews WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
            response["reviews"] = [
                {
                    "id": row["id"],
                    "task_id": row["task_id"],
                    "reviewer_id": row["reviewer_id"],
                    "decision": row["decision"],
                    "acceptance_results": json.loads(row["acceptance_results_json"]),
                    "notes": row["notes"],
                    "created_at": row["created_at"],
                }
                for row in reviews
            ]
    return response


_SUMMARY_FIELDS = {"id", "title", "status", "priority", "updated_at"}


@mcp.tool()
def task_list(
    status: str | None = None,
    project: str | None = None,
    assignee_id: str | None = None,
    assignee_role: str | None = None,
    creator_role: str | None = None,
    parent_task_id: str | None = None,
    order_by: str | None = None,
    limit: int = 50,
    summary: bool = False,
    exclude_status: list[str] | None = None,
) -> list[dict]:
    clauses = []
    params: list[object] = []

    if status:
        clauses.append("status = ?")
        params.append(status)
    if project:
        clauses.append("project = ?")
        params.append(project)
    if assignee_id:
        clauses.append("assignee_id = ?")
        params.append(assignee_id)
    if assignee_role:
        clauses.append("assignee_role = ?")
        params.append(assignee_role)
    if creator_role:
        clauses.append("creator_role = ?")
        params.append(creator_role)
    if parent_task_id:
        clauses.append("parent_task_id = ?")
        params.append(parent_task_id)
    if exclude_status:
        placeholders = ", ".join("?" * len(exclude_status))
        clauses.append(f"status NOT IN ({placeholders})")
        params.extend(exclude_status)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(limit, 200)))

    if order_by == "task_number":
        order_clause = "ORDER BY task_number ASC"
    else:
        order_clause = "ORDER BY updated_at DESC"
    query = f"SELECT * FROM tasks {where} {order_clause} LIMIT ?"

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()

    tasks = [_task_row_to_dict(row) for row in rows]
    if summary:
        return [{k: v for k, v in t.items() if k in _SUMMARY_FIELDS} for t in tasks]
    return tasks


@mcp.tool()
def task_update(
    task_id: str,
    status: str | None = None,
    progress_note: str | None = None,
    artifact_paths: list[str] | None = None,
    blocked_reason: str | None = None,
    metadata_patch: dict | None = None,
    purpose_gap: str | None = None,
    actor_id: str = "system",
) -> dict:
    metadata_patch = metadata_patch or {}
    now = _utc_now()

    if artifact_paths is not None:
        _validate_artifact_paths(artifact_paths)

    with get_db() as conn:
        row = _get_task_row(conn, task_id)
        current_status = row["status"]
        new_status = status or current_status
        _ensure_transition(current_status, new_status)
        pane_id = row["pane_id"]

        merged_metadata = json.loads(row["metadata_json"])
        if progress_note:
            merged_metadata["progress_note"] = progress_note
        if artifact_paths is not None:
            merged_metadata["artifact_paths"] = artifact_paths
        if blocked_reason is not None:
            merged_metadata["blocked_reason"] = blocked_reason
            merged_metadata["is_blocked"] = bool(blocked_reason)
        merged_metadata.update(metadata_patch)

        pane_id_for_update = pane_id
        if current_status == "review" and new_status != "review":
            session = _load_runner_config()["zellij_session"]
            _close_pane_for_review_exit(
                conn,
                task_id=task_id,
                pane_id=pane_id,
                session=session,
                actor_id=actor_id,
            )
            pane_id_for_update = None

        completed_at = now if new_status in ("done", "cancelled") else row["completed_at"]
        conn.execute(
            """
            UPDATE tasks
            SET status = ?, metadata_json = ?, pane_id = ?, purpose_gap = COALESCE(?, purpose_gap),
                updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                new_status,
                json.dumps(merged_metadata, ensure_ascii=True),
                pane_id_for_update,
                purpose_gap,
                now,
                completed_at,
                task_id,
            ),
        )
        _log_event(
            conn,
            task_id=task_id,
            event_type="update",
            actor_id=actor_id,
            payload={"from": current_status, "to": new_status},
        )

        updated = _get_task_row(conn, task_id)
    return {"task": _task_row_to_dict(updated)}


@mcp.tool()
def task_complete(
    task_id: str,
    summary: str,
    artifact_paths: list[str],
    verification: str,
    purpose_gap: str | None = None,
    next_risks: str | None = None,
    actor_id: str = "system",
) -> dict:
    if not summary.strip():
        raise ValueError("summary must not be empty")
    _validate_artifact_paths(artifact_paths)

    now = _utc_now()
    with get_db() as conn:
        row = _get_task_row(conn, task_id)
        current_status = row["status"]
        _ensure_transition(current_status, "review")

        merged_metadata = json.loads(row["metadata_json"])
        merged_metadata["completion_summary"] = summary
        merged_metadata["artifact_paths"] = artifact_paths
        merged_metadata["verification"] = verification
        if next_risks is not None:
            merged_metadata["next_risks"] = next_risks

        conn.execute(
            """
            UPDATE tasks
            SET status = 'review', metadata_json = ?, purpose_gap = COALESCE(?, purpose_gap), updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(merged_metadata, ensure_ascii=True), purpose_gap, now, task_id),
        )
        _log_event(
            conn,
            task_id=task_id,
            event_type="complete",
            actor_id=actor_id,
            payload={"from": current_status, "to": "review"},
        )
        updated = _get_task_row(conn, task_id)

    if row["needs_worktree"]:
        project_dir = _load_runner_config()["project_dir"]
        try:
            _remove_worktree(project_dir, task_id)
        except Exception:
            pass

    return {"task": _task_row_to_dict(updated)}


@mcp.tool()
def task_cancel(task_id: str, reason: str = "", actor_id: str = "system") -> dict:
    now = _utc_now()
    with get_db() as conn:
        row = _get_task_row(conn, task_id)
        current_status = row["status"]
        if current_status == "done":
            raise ValueError("done status task cannot be cancelled")
        _ensure_transition(current_status, "cancelled")

        pane_id = row["pane_id"]
        pane_closed = False
        pane_close_error: str | None = None
        session: str | None = None
        if pane_id:
            session = _load_runner_config()["zellij_session"]

        if pane_id and session:
            try:
                pane.close_pane(session, pane_id)
                pane_closed = True
            except Exception as exc:
                pane_close_error = str(exc)

        conn.execute(
            """
            UPDATE tasks
            SET status = 'cancelled', pane_id = NULL, updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (now, now, task_id),
        )
        _log_event(
            conn,
            task_id=task_id,
            event_type="cancel",
            actor_id=actor_id,
            payload={
                "from": current_status,
                "to": "cancelled",
                "reason": reason,
                "pane_id": pane_id,
                "pane_closed": pane_closed,
                "pane_close_error": pane_close_error,
            },
        )
        updated = _get_task_row(conn, task_id)

    if row["needs_worktree"]:
        project_dir = _load_runner_config()["project_dir"]
        try:
            _remove_worktree(project_dir, task_id)
        except Exception:
            pass

    return {
        "task": _task_row_to_dict(updated),
        "cancelled": True,
        "pane_closed": pane_closed,
        "pane_close_error": pane_close_error,
    }


@mcp.tool()
def task_resume(task_id: str, actor_id: str = "system") -> dict:
    config = _load_runner_config()
    if not config["enabled"]:
        raise ValueError("task runner is disabled")

    session = config["zellij_session"]
    if not session:
        raise ValueError("zellij_session is not configured")

    with get_db() as conn:
        row = _get_task_row(conn, task_id)
        if row["status"] != "assigned":
            raise ValueError(f"Task {task_id} must be assigned to resume")

        current_pane_id = row["pane_id"]
        active_pane_ids = _active_pane_ids(session)
        if current_pane_id and current_pane_id in active_pane_ids:
            _log_event(
                conn,
                task_id=task_id,
                event_type="resume_skipped",
                actor_id=actor_id,
                payload={"reason": "pane_already_active", "pane_id": current_pane_id},
            )
            return {
                "task": _task_row_to_dict(row),
                "resumed": False,
                "skipped": True,
                "reason": "pane_already_active",
                "pane_id": current_pane_id,
            }

        resume_cfg = _load_executors_config(config.get("project_dir"))
        resume_bloom = int(row["bloom_level"] if row["bloom_level"] is not None else 3)
        resume_executor = row["executor"] or None
        resume_tier, resume_gap = _select_model_tier(resume_bloom, resume_executor, resume_cfg["capability_tiers"])
        resume_cmd = _build_executor_command(resume_tier, resume_cfg["runners"], resume_gap, resume_bloom)

        pane_id = _spawn_executor_pane(
            session, task_id, _executor_resume_prompt(task_id), launch_command=resume_cmd
        )
        conn.execute("UPDATE tasks SET pane_id = ?, updated_at = ? WHERE id = ?", (pane_id, _utc_now(), task_id))
        _log_event(
            conn,
            task_id=task_id,
            event_type="resume",
            actor_id=actor_id,
            payload={"from_pane_id": current_pane_id, "pane_id": pane_id, "session": session},
        )
        updated = _get_task_row(conn, task_id)

    return {
        "task": _task_row_to_dict(updated),
        "resumed": True,
        "skipped": False,
        "reason": None,
        "pane_id": updated["pane_id"],
    }


@mcp.tool()
def task_peek(task_id: str, lines: int = 100) -> dict:
    with get_db() as conn:
        row = _get_task_row(conn, task_id)
        pane_id = row["pane_id"]

    if not pane_id:
        raise ValueError(f"Task {task_id} has no pane_id")

    session = _load_runner_config()["zellij_session"]
    if not session:
        raise ValueError("zellij_session is not configured")

    output = pane.read_output(session, pane_id, lines=lines)
    return {
        "task_id": task_id,
        "status": row["status"],
        "pane_id": pane_id,
        "lines": max(0, lines),
        "output": output,
    }


@mcp.tool()
def task_reopen(task_id: str, message: str, actor_id: str = "system") -> dict:
    """Reopen a review-status task with an additional instruction, restarting the Claude session."""
    if not message.strip():
        raise ValueError("message must not be empty")

    config = _load_runner_config()
    session = config["zellij_session"]

    with get_db() as conn:
        row = _get_task_row(conn, task_id)
        if row["status"] != "review":
            raise ValueError(f"task_reopen only accepts review status tasks, got: {row['status']}")

        task = _task_row_to_dict(row)
        metadata = task.get("metadata", {})
        completion_summary = metadata.get("completion_summary", "（なし）")
        context_message = (
            f'task_id="{task_id}" のタスクに追加指示がある。\n'
            f'タイトル: {task["title"]}\n'
            f'目的: {task["purpose"]}\n'
            f'完了サマリー: {completion_summary}\n\n'
            f'追加指示:\n{message}'
        )

        if session:
            _cleanup_orphan_panes(conn, session)
        row = _get_task_row(conn, task_id)
        existing_pane_id = row["pane_id"]

        now = _utc_now()
        conn.execute(
            "UPDATE tasks SET status = 'in_progress', updated_at = ? WHERE id = ?",
            (now, task_id),
        )
        _log_event(
            conn,
            task_id=task_id,
            event_type="reopen",
            actor_id=actor_id,
            payload={"from": "review", "to": "in_progress", "pane_id": existing_pane_id, "message": message},
        )

        runner_started = False
        runner_error: str | None = None
        result_pane_id = existing_pane_id

        executors_cfg = _load_executors_config(config.get("project_dir"))
        reopen_bloom = int(row["bloom_level"] if row["bloom_level"] is not None else 3)
        reopen_executor = row["executor"] or None
        reopen_tier, reopen_gap = _select_model_tier(reopen_bloom, reopen_executor, executors_cfg["capability_tiers"])
        reopen_cmd = _build_executor_command(reopen_tier, executors_cfg["runners"], reopen_gap, reopen_bloom)

        if config["enabled"]:
            if not session:
                raise ValueError("Task runner enabled but zellij_session is not configured")

            if existing_pane_id:
                try:
                    pane.send_text(session, existing_pane_id, context_message)
                    runner_started = True
                    _log_event(
                        conn,
                        task_id=task_id,
                        event_type="pane_restarted",
                        actor_id="system",
                        payload={"pane_id": existing_pane_id, "session": session},
                    )
                except Exception as exc:
                    runner_error = str(exc)
            else:
                new_pane_id: str | None = None
                try:
                    new_pane_id = pane.create_tab(session, f"task-{task_id}")
                    pane.go_to_tab(session, INTERFACE_TAB_NAME)
                    task_id_file = Path(tempfile.gettempdir()) / f"summonai_pane_{new_pane_id}.task_id"
                    task_id_file.write_text(task_id, encoding="utf-8")
                    _wait_for_any_output(session, new_pane_id, timeout=30.0)
                    pane.send_text(session, new_pane_id, reopen_cmd)
                    _wait_for_any_output(session, new_pane_id, timeout=60.0)
                    pane.send_text(session, new_pane_id, context_message)
                    runner_started = True
                    result_pane_id = new_pane_id
                    conn.execute(
                        "UPDATE tasks SET pane_id = ?, updated_at = ? WHERE id = ?",
                        (new_pane_id, _utc_now(), task_id),
                    )
                    _log_event(
                        conn,
                        task_id=task_id,
                        event_type="pane_started",
                        actor_id="system",
                        payload={"pane_id": new_pane_id, "session": session},
                    )
                except Exception as exc:
                    runner_error = str(exc)
                    if new_pane_id:
                        try:
                            pane.close_pane(session, new_pane_id)
                        except Exception:
                            pass
                        (Path(tempfile.gettempdir()) / f"summonai_pane_{new_pane_id}.task_id").unlink(missing_ok=True)

        updated = _get_task_row(conn, task_id)

    return {
        "task": _task_row_to_dict(updated),
        "runner_started": runner_started,
        "runner_error": runner_error,
        "pane_id": result_pane_id,
    }


@mcp.tool()
def task_message(task_id: str, message: str, actor_id: str = "system") -> dict:
    if not message.strip():
        raise ValueError("message must not be empty")

    session = _load_runner_config()["zellij_session"]
    if not session:
        raise ValueError("zellij_session is not configured")

    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _get_task_row(conn, task_id)
        if row["status"] != "in_progress":
            raise ValueError(f"Task {task_id} must be in_progress to accept messages")

        pane_id = row["pane_id"]
        if not pane_id:
            raise ValueError(f"Task {task_id} has no pane_id")

        pane.send_text(session, pane_id, message)
        conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (_utc_now(), task_id))
        _log_event(
            conn,
            task_id=task_id,
            event_type="message",
            actor_id=actor_id,
            payload={"pane_id": pane_id, "message": message},
        )
        updated = _get_task_row(conn, task_id)

    return {
        "task": _task_row_to_dict(updated),
        "sent": True,
        "pane_id": pane_id,
    }


if __name__ == "__main__":
    mcp.run()
