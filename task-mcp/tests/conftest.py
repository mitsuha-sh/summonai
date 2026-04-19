"""Test-isolation guards shared across all test modules.

Root cause of the guarded issue
--------------------------------
Several runner tests (e.g. ``test_task_resume_skips_when_existing_pane_is_active``,
``test_task_reopen_with_existing_pane_restarts_claude``) configure the zellij
runner with ``zellij_session: "summonai"`` **before** applying pane-level mocks,
then call ``task_create(assignee_role="executor")``.  That triggers
``_spawn_task_runner_if_configured`` → ``pane.list_panes`` → ``subprocess.run``
while no mock is in place.  When pytest runs inside the real "summonai" zellij
session this creates actual panes.

Fix: autouse guard replaces ``subprocess.run`` for every test function.
Any call whose first argument is ``"zellij"`` raises ``AssertionError`` unless
a test-level mock (e.g. ``unittest.mock.patch``) has already replaced it.
"""

from __future__ import annotations

import subprocess

import pytest


@pytest.fixture(autouse=True)
def block_real_zellij_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent any test from accidentally invoking the real zellij binary.

    Tests that need to control ``subprocess.run`` behaviour patch it
    individually via ``unittest.mock.patch``, which shadows this guard
    for the duration of the ``with`` block.
    """
    _real_run = subprocess.run

    def _guard(cmd, **kwargs):
        exe = (cmd[0] if cmd else "") if not isinstance(cmd, str) else cmd.split()[0]
        if exe == "zellij":
            raise AssertionError(
                f"Test invoked real zellij without a subprocess mock. cmd={cmd!r}"
            )
        return _real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", _guard)
