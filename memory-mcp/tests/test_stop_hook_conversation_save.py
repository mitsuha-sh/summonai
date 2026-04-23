#!/usr/bin/env python3
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

_MODULE_TMPDIR = tempfile.TemporaryDirectory()
os.environ["SUMMONAI_MEMORY_DB"] = str(Path(_MODULE_TMPDIR.name) / "test_memory.db")

import stop_hook_conversation_save  # noqa: E402


class StopHookConversationSaveTest(unittest.TestCase):
    def test_main_defaults_agent_id_to_summonai_when_unset(self):
        payload = {"transcript": "assistant: hello"}

        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                with mock.patch(
                    "stop_hook_conversation_save.memory_l1_save_enabled", return_value=True
                ):
                    with mock.patch(
                        "stop_hook_conversation_save.resolve_scope",
                        return_value={
                            "project": "sample-project",
                            "scope_type": "project",
                            "scope_id": "sample-project",
                        },
                    ):
                        with mock.patch(
                            "stop_hook_conversation_save.server.conversation_save",
                            return_value="saved",
                        ) as save_mock:
                            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                                rc = stop_hook_conversation_save.main()

        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue().strip(), "saved")
        self.assertEqual(save_mock.call_args.kwargs["agent_id"], "summonai")

    def test_main_uses_agent_id_from_runtime_config(self):
        payload = {"transcript": "assistant: hello"}
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / "memory.toml"
            config.write_text('agent_id = "shared-agent"\nproject = "summonai"\n', encoding="utf-8")
            with mock.patch.dict("os.environ", {"SUMMONAI_MEMORY_CONFIG": str(config)}, clear=True):
                with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                    with mock.patch(
                        "stop_hook_conversation_save.memory_l1_save_enabled", return_value=True
                    ):
                        with mock.patch(
                            "stop_hook_conversation_save.server.conversation_save",
                            return_value="saved",
                        ) as save_mock:
                            with mock.patch("sys.stdout", new_callable=io.StringIO):
                                rc = stop_hook_conversation_save.main()

        self.assertEqual(rc, 0)
        self.assertEqual(save_mock.call_args.kwargs["agent_id"], "shared-agent")
        self.assertEqual(save_mock.call_args.kwargs["project"], "summonai")

    def test_main_replaces_existing_chunks_for_same_session(self):
        first_transcript = """\
user: first stop-hook marker
assistant: first stop-hook reply
user: keep old?
assistant: should be replaced
"""
        second_transcript = """\
user: second stop-hook marker
assistant: second stop-hook reply
user: overwrite snapshot
assistant: old chunk must disappear
"""

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "stop_hook_test.db"
            with mock.patch.dict("os.environ", {"SUMMONAI_MEMORY_DB": str(db_path)}, clear=False):
                stop_hook_conversation_save.server.MIGRATIONS_DIR = PROJECT_ROOT / "db" / "migrations"
                stop_hook_conversation_save.server.ensure_schema()

                for payload in (
                    {
                        "session_id": "sess-stop-replace-1",
                        "ended_at": "2026-04-04T11:00:00",
                        "transcript": first_transcript,
                    },
                    {
                        "session_id": "sess-stop-replace-1",
                        "ended_at": "2026-04-04T11:05:00",
                        "transcript": second_transcript,
                    },
                ):
                    with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                        with mock.patch(
                            "stop_hook_conversation_save.memory_l1_save_enabled", return_value=True
                        ):
                            with mock.patch(
                                "stop_hook_conversation_save.resolve_scope",
                                return_value={
                                    "project": "sample-project",
                                    "scope_type": "project",
                                    "scope_id": "sample-project",
                                },
                            ):
                                with mock.patch("stop_hook_conversation_save.pick_env", return_value=None):
                                    with mock.patch("stop_hook_conversation_save.resolve_agent_id", return_value="default"):
                                        rc = stop_hook_conversation_save.main()
                    self.assertEqual(rc, 0)

                raw_recent = stop_hook_conversation_save.server.conversation_load_recent(
                    agent_id="default",
                    project="sample-project",
                    since_days=365,
                    limit_chunks=10,
                )
                rows = json.loads(raw_recent)
                joined = "\n".join(item["content"] for item in rows)

        self.assertIn("second stop-hook marker", joined)
        self.assertNotIn("first stop-hook marker", joined)


if __name__ == "__main__":
    unittest.main()
