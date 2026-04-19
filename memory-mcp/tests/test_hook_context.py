#!/usr/bin/env python3
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from hook_context import (  # noqa: E402
    load_runtime_config,
    memory_l1_save_enabled,
    resolve_agent_id,
    resolve_persona_dir,
    resolve_scope,
)


class HookContextScopeTest(unittest.TestCase):
    def test_resolve_scope_inside_git_repo_is_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "sample-project"
            repo.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)

            with mock.patch.dict(os.environ, {"PWD": str(repo)}, clear=False):
                scope = resolve_scope({})

            self.assertEqual(scope["scope_type"], "project")
            self.assertEqual(scope["scope_id"], "sample-project")
            self.assertEqual(scope["project"], "sample-project")

    def test_resolve_scope_outside_project_is_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "tmp"
            outside.mkdir(parents=True, exist_ok=True)

            with mock.patch.dict(os.environ, {"PWD": str(outside)}, clear=False):
                scope = resolve_scope({})

            self.assertEqual(scope["scope_type"], "user")
            self.assertEqual(scope["scope_id"], "global")
            self.assertIsNone(scope["project"])

    def test_explicit_project_has_highest_priority(self):
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": "/tmp/random"}, clear=True):
            scope = resolve_scope({"project": "sample-project"})
        self.assertEqual(scope["scope_type"], "project")
        self.assertEqual(scope["scope_id"], "sample-project")

    def test_runtime_config_file_sets_agent_project_scope_and_persona(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "memory.toml"
            persona_dir = Path(tmp) / "personas" / "shared"
            config.write_text(
                "\n".join(
                    [
                        'agent_id = "shared-agent"',
                        'project = "summonai"',
                        'scope_type = "project"',
                        'scope_id = "summonai"',
                        f'persona_dir = "{persona_dir}"',
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"SUMMONAI_MEMORY_CONFIG": str(config)}, clear=True):
                loaded = load_runtime_config({})
                scope = resolve_scope({})
                self.assertEqual(loaded["agent_id"], "shared-agent")
                self.assertEqual(resolve_agent_id({}), "shared-agent")
                self.assertEqual(scope["project"], "summonai")
                self.assertEqual(scope["scope_id"], "summonai")
                self.assertEqual(resolve_persona_dir({}), persona_dir)

    def test_env_overrides_runtime_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "memory.toml"
            config.write_text('agent_id = "from-config"\nproject = "from-config"\n', encoding="utf-8")
            env = {
                "SUMMONAI_MEMORY_CONFIG": str(config),
                "SUMMONAI_AGENT_ID": "from-env",
                "SUMMONAI_PROJECT": "env-project",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                scope = resolve_scope({})
                self.assertEqual(resolve_agent_id({}), "from-env")
                self.assertEqual(scope["project"], "env-project")
                self.assertEqual(scope["scope_id"], "env-project")

    def test_memory_l1_save_enabled_defaults_true_outside_tmux(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(memory_l1_save_enabled({}))

    def test_memory_l1_save_enabled_false_in_tmux_when_option_zero(self):
        with mock.patch.dict(os.environ, {"TMUX_PANE": "%1"}, clear=False):
            with mock.patch("hook_context.tmux_option", return_value="0"):
                self.assertFalse(memory_l1_save_enabled({}))

    def test_memory_l1_save_enabled_true_in_tmux_when_option_one(self):
        with mock.patch.dict(os.environ, {"TMUX_PANE": "%1"}, clear=False):
            with mock.patch("hook_context.tmux_option", return_value="1"):
                self.assertTrue(memory_l1_save_enabled({}))


if __name__ == "__main__":
    unittest.main()
