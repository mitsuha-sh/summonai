#!/usr/bin/env python3
import io
import json
import tempfile
import unittest
from unittest import mock

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import session_start_memory_context  # noqa: E402


class SessionStartMemoryContextTest(unittest.TestCase):
    def test_resolve_agent_id_uses_tmux_option_inside_tmux(self):
        payload = {"agent_id": "ignored"}
        with mock.patch.dict("os.environ", {"TMUX_PANE": "%1"}, clear=True):
            with mock.patch("hook_context.tmux_option", return_value="worker3"):
                self.assertEqual(session_start_memory_context.resolve_agent_id(payload), "worker3")

    def test_resolve_agent_id_uses_summonai_agent_id_outside_tmux(self):
        payload = {"agent_id": "ignored"}
        env = {"SUMMONAI_AGENT_ID": "coordinator"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(session_start_memory_context.resolve_agent_id(payload), "coordinator")

    def test_resolve_agent_id_defaults_to_default_outside_tmux(self):
        payload = {}
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(session_start_memory_context.resolve_agent_id(payload), "default")

    def test_resolve_role_and_task_id_without_zellij_pane_id_uses_env_fallback(self):
        env = {"SUMMONAI_ROLE": "interface", "SUMMONAI_TASK_ID": "task_from_env"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(
                session_start_memory_context._resolve_role_and_task_id(),
                ("interface", "task_from_env"),
            )

    def test_resolve_role_and_task_id_uses_terminal_pane_task_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            task_file = Path(tmpdir) / "summonai_pane_terminal_1.task_id"
            task_file.write_text("task_abc123\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"ZELLIJ_PANE_ID": "1"}, clear=True):
                with mock.patch("session_start_memory_context.tempfile.gettempdir", return_value=tmpdir):
                    self.assertEqual(
                        session_start_memory_context._resolve_role_and_task_id(),
                        ("executor", "task_abc123"),
                    )

    def test_resolve_role_and_task_id_missing_file_falls_back_to_interface(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict("os.environ", {"ZELLIJ_PANE_ID": "1"}, clear=True):
                with mock.patch("session_start_memory_context.tempfile.gettempdir", return_value=tmpdir):
                    self.assertEqual(
                        session_start_memory_context._resolve_role_and_task_id(),
                        ("interface", ""),
                    )

    def test_resolve_role_and_task_id_empty_file_falls_back_to_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            task_file = Path(tmpdir) / "summonai_pane_terminal_1.task_id"
            task_file.write_text(" \n", encoding="utf-8")
            env = {
                "ZELLIJ_PANE_ID": "1",
                "SUMMONAI_ROLE": "executor",
                "SUMMONAI_TASK_ID": "task_from_env",
            }
            with mock.patch.dict("os.environ", env, clear=True):
                with mock.patch("session_start_memory_context.tempfile.gettempdir", return_value=tmpdir):
                    self.assertEqual(
                        session_start_memory_context._resolve_role_and_task_id(),
                        ("executor", "task_from_env"),
                    )

    def test_resolve_role_and_task_id_missing_file_with_executor_env_fallback(self):
        env = {
            "ZELLIJ_PANE_ID": "1",
            "SUMMONAI_ROLE": "executor",
            "SUMMONAI_TASK_ID": "task_env_123",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict("os.environ", env, clear=True):
                with mock.patch("session_start_memory_context.tempfile.gettempdir", return_value=tmpdir):
                    self.assertEqual(
                        session_start_memory_context._resolve_role_and_task_id(),
                        ("executor", "task_env_123"),
                    )

    def test_persona_markdown_is_emitted_when_dir_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            persona_dir = Path(tmpdir) / "persona"
            persona_dir.mkdir(parents=True, exist_ok=True)
            (persona_dir / "USER.md").write_text("# USER\nalpha\n", encoding="utf-8")
            (persona_dir / "SOUL.md").write_text("# SOUL\nbeta\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"SUMMONAI_MEMORY_MCP_DIR": tmpdir}, clear=False):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    session_start_memory_context.emit_persona_markdown()

        out = stdout.getvalue()
        self.assertIn("BEGIN USER.md", out)
        self.assertIn("# USER", out)
        self.assertIn("BEGIN SOUL.md", out)
        self.assertIn("# SOUL", out)

    def test_persona_markdown_skips_when_env_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict("os.environ", {"SUMMONAI_MEMORY_MCP_DIR": tmpdir}, clear=True):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    session_start_memory_context.emit_persona_markdown()

        self.assertIn("見つからないため注入をスキップ", stdout.getvalue())

    def test_persona_markdown_uses_configured_single_source_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / "memory.toml"
            persona_dir = Path(tmpdir) / "personas" / "shared-agent"
            persona_dir.mkdir(parents=True, exist_ok=True)
            (persona_dir / "USER.md").write_text("# Shared USER\n", encoding="utf-8")
            (persona_dir / "SOUL.md").write_text("# Shared SOUL\n", encoding="utf-8")
            config.write_text(f'persona_dir = "{persona_dir}"\n', encoding="utf-8")
            with mock.patch.dict("os.environ", {"SUMMONAI_MEMORY_CONFIG": str(config)}, clear=True):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    session_start_memory_context.emit_persona_markdown({})

        out = stdout.getvalue()
        self.assertIn("# Shared USER", out)
        self.assertIn("# Shared SOUL", out)

    def test_memory_guidelines_are_emitted_when_file_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            docs_dir = Path(tmpdir) / "docs"
            docs_dir.mkdir(parents=True, exist_ok=True)
            (docs_dir / "memory_guidelines.md").write_text("# guide\nsample\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"SUMMONAI_MEMORY_MCP_DIR": tmpdir}, clear=False):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    session_start_memory_context.emit_memory_guidelines_markdown()

        out = stdout.getvalue()
        self.assertIn("SESSION_START_MEMORY_GUIDE", out)
        self.assertIn("bucket=code/knowledge/content", out)

    def test_memory_guidelines_skip_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict("os.environ", {"SUMMONAI_MEMORY_MCP_DIR": tmpdir}, clear=False):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    session_start_memory_context.emit_memory_guidelines_markdown()

        self.assertIn("見つからないため注入をスキップ", stdout.getvalue())

    def test_memory_guidelines_use_default_repo_path_without_env(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                session_start_memory_context.emit_memory_guidelines_markdown()

        out = stdout.getvalue()
        self.assertIn("docs/memory_guidelines.md", out)
        self.assertIn("memory_type=episodic/semantic/procedural/idea", out)

    def test_resolve_summonai_dir_prefers_memory_repo_parent_with_instructions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            summonai_dir = tmp_path / "summonai"
            memory_repo_dir = summonai_dir / "memory-mcp"
            memory_repo_dir.mkdir(parents=True, exist_ok=True)
            (summonai_dir / "instructions").mkdir(parents=True, exist_ok=True)

            with mock.patch.dict("os.environ", {}, clear=True):
                with mock.patch(
                    "session_start_memory_context.resolve_repo_dir",
                    return_value=memory_repo_dir,
                ):
                    resolved = session_start_memory_context.resolve_summonai_dir()

        self.assertEqual(resolved, summonai_dir.resolve())

    def test_resolve_summonai_dir_uses_parent_summonai_child_if_parent_not_repo_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            memory_repo_dir = tmp_path / "memory-mcp"
            nested_summonai_dir = tmp_path / "summonai"
            memory_repo_dir.mkdir(parents=True, exist_ok=True)
            (nested_summonai_dir / "instructions").mkdir(parents=True, exist_ok=True)

            with mock.patch.dict("os.environ", {}, clear=True):
                with mock.patch(
                    "session_start_memory_context.resolve_repo_dir",
                    return_value=memory_repo_dir,
                ):
                    resolved = session_start_memory_context.resolve_summonai_dir()

        self.assertEqual(resolved, nested_summonai_dir.resolve())

    def test_memory_l1_disabled_avoids_memory_load_guidance(self):
        payload = {}
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            with mock.patch("session_start_memory_context.memory_l1_save_enabled", return_value=False):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    rc = session_start_memory_context.main()

        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("memory_l1_save=0", out)
        self.assertNotIn("memory_load", out)
        self.assertNotIn("conversation_load_recent", out)

    def test_memory_l1_enabled_emits_memory_restore_guidance(self):
        payload = {"agent_id": "worker1", "project": "sample-project"}
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                with mock.patch("session_start_memory_context.memory_l1_save_enabled", return_value=True):
                    with mock.patch("session_start_memory_context.resolve_agent_id", return_value="worker1"):
                        with mock.patch("session_start_memory_context._resolve_role_and_task_id", return_value=("interface", "")):
                            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                                rc = session_start_memory_context.main()

        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("SESSION_START_MEMORY_GUIDE", out)
        self.assertIn("memory_load bucket=\"code\"", out)
        self.assertIn("conversation_load_recent(", out)
        self.assertIn('agent_id="worker1", limit_chunks=6, since_days=3', out)
        self.assertIn("プロジェクト横断", out)

    def test_executor_role_skips_conversation_load_recent(self):
        payload = {"agent_id": "worker1", "project": "sample-project"}
        with tempfile.TemporaryDirectory() as tmpdir:
            instructions_dir = Path(tmpdir) / "instructions"
            instructions_dir.mkdir(parents=True, exist_ok=True)
            (instructions_dir / "executor.md").write_text("# executor\nrules\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"SUMMONAI_DIR": tmpdir}, clear=True):
                with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                    with mock.patch("session_start_memory_context.memory_l1_save_enabled", return_value=True):
                        with mock.patch("session_start_memory_context.resolve_agent_id", return_value="worker1"):
                            with mock.patch("session_start_memory_context._resolve_role_and_task_id", return_value=("executor", "")):
                                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                                    rc = session_start_memory_context.main()

        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("memory_load bucket=\"code\"", out)
        self.assertIn("conversation_load_recent はスキップ", out)
        self.assertNotIn("conversation_load_recent(", out)
        self.assertIn("[SESSION_START_EXECUTOR_PROTOCOL]", out)
        self.assertIn("[SESSION_START_EXECUTOR_INSTRUCTIONS]", out)
        self.assertIn("task_get(task_id=", out)
        self.assertIn("task_complete(task_id=\"<missing-task-id>\")", out)

    def test_executor_role_emits_protocol_with_task_id(self):
        payload = {"agent_id": "worker1", "project": "sample-project"}
        with tempfile.TemporaryDirectory() as tmpdir:
            instructions_dir = Path(tmpdir) / "instructions"
            instructions_dir.mkdir(parents=True, exist_ok=True)
            (instructions_dir / "executor.md").write_text("# executor\nrules\n", encoding="utf-8")
            env = {"SUMMONAI_DIR": tmpdir}
            with mock.patch.dict("os.environ", env, clear=True):
                with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                    with mock.patch("session_start_memory_context.memory_l1_save_enabled", return_value=True):
                        with mock.patch("session_start_memory_context.resolve_agent_id", return_value="worker1"):
                            with mock.patch("session_start_memory_context._resolve_role_and_task_id", return_value=("executor", "task_abc123")):
                                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                                    rc = session_start_memory_context.main()

        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("[SESSION_START_EXECUTOR_PROTOCOL]", out)
        self.assertIn("task_get(task_id=\"task_abc123\")", out)
        self.assertIn("task_complete(task_id=\"task_abc123\")", out)

    def test_executor_role_skips_executor_instructions_when_missing(self):
        payload = {"agent_id": "worker1", "project": "sample-project"}
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"SUMMONAI_DIR": tmpdir}
            with mock.patch.dict("os.environ", env, clear=True):
                with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                    with mock.patch("session_start_memory_context.memory_l1_save_enabled", return_value=True):
                        with mock.patch("session_start_memory_context.resolve_agent_id", return_value="worker1"):
                            with mock.patch("session_start_memory_context._resolve_role_and_task_id", return_value=("executor", "")):
                                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                                    rc = session_start_memory_context.main()

        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("[SESSION_START_EXECUTOR_INSTRUCTIONS]", out)
        self.assertIn("見つからないため注入をスキップ", out)

    def test_interface_role_emits_interface_instructions(self):
        payload = {"agent_id": "worker1", "project": "sample-project"}
        with tempfile.TemporaryDirectory() as tmpdir:
            instructions_dir = Path(tmpdir) / "instructions"
            instructions_dir.mkdir(parents=True, exist_ok=True)
            (instructions_dir / "interface.md").write_text("# interface\nrules\n", encoding="utf-8")
            env = {"SUMMONAI_DIR": tmpdir}
            with mock.patch.dict("os.environ", env, clear=True):
                with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                    with mock.patch("session_start_memory_context.memory_l1_save_enabled", return_value=True):
                        with mock.patch("session_start_memory_context.resolve_agent_id", return_value="summonai"):
                            with mock.patch("session_start_memory_context._resolve_role_and_task_id", return_value=("interface", "")):
                                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                                    rc = session_start_memory_context.main()

        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("[SESSION_START_INTERFACE_INSTRUCTIONS]", out)
        self.assertIn("BEGIN interface.md", out)
        self.assertIn("# interface", out)
        self.assertIn("conversation_load_recent(", out)

    def test_unset_role_keeps_conversation_load_recent(self):
        payload = {"agent_id": "worker1", "project": "sample-project"}
        with tempfile.TemporaryDirectory() as tmpdir:
            instructions_dir = Path(tmpdir) / "instructions"
            instructions_dir.mkdir(parents=True, exist_ok=True)
            (instructions_dir / "interface.md").write_text("# interface\ndefault\n", encoding="utf-8")
            env = {"SUMMONAI_DIR": tmpdir}
            with mock.patch.dict("os.environ", env, clear=True):
                with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                    with mock.patch("session_start_memory_context.memory_l1_save_enabled", return_value=True):
                        with mock.patch("session_start_memory_context.resolve_agent_id", return_value="worker1"):
                            with mock.patch("session_start_memory_context._resolve_role_and_task_id", return_value=("interface", "")):
                                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                                    rc = session_start_memory_context.main()

        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("[SESSION_START_INTERFACE_INSTRUCTIONS]", out)
        self.assertIn("BEGIN interface.md", out)
        self.assertIn("memory_load bucket=\"code\"", out)
        self.assertIn("conversation_load_recent(", out)


if __name__ == "__main__":
    unittest.main()
