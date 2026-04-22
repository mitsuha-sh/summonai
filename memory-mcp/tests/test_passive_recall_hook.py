#!/usr/bin/env python3
"""Tests for passive_recall_hook.py.

Unit tests: mock DB/model, test stdin parsing and output format.
Integration tests: real DB, real model (skipped when DB is unavailable).
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import passive_recall_hook as hook


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):
    def test_estimate_tokens_empty(self):
        self.assertEqual(hook._estimate_tokens(""), 0)

    def test_estimate_tokens_basic(self):
        text = "a" * 100
        self.assertEqual(hook._estimate_tokens(text), 25)

    def test_state_path_normal(self):
        p = hook._state_path("abc123")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertIn("summonai_passive_recall_", p.name)

    def test_state_path_unknown_returns_none(self):
        self.assertIsNone(hook._state_path(""))
        self.assertIsNone(hook._state_path("unknown"))


class TestStateFile(unittest.TestCase):
    def test_load_state_missing_file(self):
        p = Path(tempfile.gettempdir()) / "summonai_passive_recall_no_such_file.json"
        state = hook._load_state(p)
        self.assertEqual(state, {"recent_ids": []})

    def test_save_and_load_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "state.json"
            hook._save_state(p, {"recent_ids": [[1, 2], [3]]})
            loaded = hook._load_state(p)
            self.assertEqual(loaded, {"recent_ids": [[1, 2], [3]]})

    def test_cleanup_removes_old_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_file = Path(tmpdir) / "summonai_passive_recall_old.json"
            old_file.write_text("{}", encoding="utf-8")
            # Set mtime to 25 hours ago
            old_time = time.time() - 25 * 3600
            os.utime(old_file, (old_time, old_time))

            recent_file = Path(tmpdir) / "summonai_passive_recall_recent.json"
            recent_file.write_text("{}", encoding="utf-8")

            with mock.patch("tempfile.gettempdir", return_value=tmpdir):
                hook._cleanup_stale_state_files()

            self.assertFalse(old_file.exists())
            self.assertTrue(recent_file.exists())


class TestMainParsing(unittest.TestCase):
    """Test main() stdin parsing and error handling."""

    def _run_main(self, stdin_text: str) -> tuple[int, str]:
        with mock.patch("sys.stdin", io.StringIO(stdin_text)):
            with mock.patch("builtins.print") as mock_print:
                with mock.patch.object(hook, "recall", return_value=[]) as _:
                    rc = hook.main()
                    output = "\n".join(
                        " ".join(str(a) for a in call.args)
                        for call in mock_print.call_args_list
                    )
        return rc, output

    def test_empty_stdin_returns_0(self):
        rc, _ = self._run_main("")
        self.assertEqual(rc, 0)

    def test_invalid_json_returns_0(self):
        rc, _ = self._run_main("not json")
        self.assertEqual(rc, 0)

    def test_missing_prompt_returns_0(self):
        rc, _ = self._run_main(json.dumps({"session_id": "abc"}))
        self.assertEqual(rc, 0)

    def test_valid_payload_calls_recall(self):
        payload = {"prompt": "test query", "session_id": "sess1"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            with mock.patch.object(
                hook, "recall", return_value=[(42, "memory content", 0.85)]
            ) as mock_recall:
                with mock.patch("builtins.print") as mock_print:
                    rc = hook.main()
        self.assertEqual(rc, 0)
        mock_recall.assert_called_once()
        call_kwargs = mock_recall.call_args
        self.assertEqual(call_kwargs.args[0], "test query")
        self.assertEqual(call_kwargs.args[1], "sess1")

    def test_recall_output_format(self):
        payload = {"prompt": "hello", "session_id": "s1"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            with mock.patch.object(
                hook, "recall", return_value=[(7, "content text", 0.75)]
            ):
                captured = []
                with mock.patch("builtins.print", side_effect=lambda *a, **kw: captured.append(a)):
                    hook.main()

        full_output = "\n".join(" ".join(str(x) for x in line) for line in captured)
        self.assertIn("[RECALL]", full_output)
        self.assertIn("memory_id=7", full_output)
        self.assertIn("similarity=0.750", full_output)
        self.assertIn("content text", full_output)

    def test_empty_recall_produces_no_output(self):
        payload = {"prompt": "hello", "session_id": "s1"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            with mock.patch.object(hook, "recall", return_value=[]):
                with mock.patch("builtins.print") as mock_print:
                    rc = hook.main()
        self.assertEqual(rc, 0)
        mock_print.assert_not_called()

    def test_recall_exception_returns_0(self):
        payload = {"prompt": "hello", "session_id": "s1"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            with mock.patch.object(hook, "recall", side_effect=RuntimeError("boom")):
                with mock.patch("builtins.print"):
                    rc = hook.main()
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Integration tests (require real DB with memories_vec)
# ---------------------------------------------------------------------------

def _find_real_db() -> str | None:
    env = os.environ.get("SUMMONAI_MEMORY_DB", "").strip()
    if env and Path(env).is_file():
        return env
    repo_root = PROJECT_ROOT.parent
    candidate = repo_root / ".data" / "summonai_memory.db"
    if candidate.is_file():
        return str(candidate)
    return None


REAL_DB = _find_real_db()
SKIP_INTEGRATION = REAL_DB is None
SKIP_REASON = "Real DB not found; set SUMMONAI_MEMORY_DB to enable integration tests"


@unittest.skipIf(SKIP_INTEGRATION, SKIP_REASON)
class TestRecallIntegration(unittest.TestCase):
    """Integration tests against the real DB. Require sqlite_vec and SentenceTransformer."""

    @classmethod
    def setUpClass(cls):
        # Verify prerequisites
        try:
            import sqlite_vec  # noqa: F401
            from sentence_transformers import SentenceTransformer  # noqa: F401
        except ImportError as e:
            raise unittest.SkipTest(f"Missing dependency: {e}")

        import sqlite3 as _sqlite3
        import sqlite_vec as _sqlite_vec
        conn = _sqlite3.connect(REAL_DB)
        conn.enable_load_extension(True)
        _sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        try:
            count = conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0]
            if count == 0:
                raise unittest.SkipTest("memories_vec is empty; run backfill_embeddings.py first")
        finally:
            conn.close()

    def test_recall_returns_list(self):
        results = hook.recall("タスクのワークフロー", "test_session_int", REAL_DB)
        self.assertIsInstance(results, list)

    def test_recall_format(self):
        results = hook.recall("記憶の設計方針", "test_session_int2", REAL_DB)
        for memory_id, content, similarity in results:
            self.assertIsInstance(memory_id, int)
            self.assertIsInstance(content, str)
            self.assertGreater(similarity, hook.SIM_THRESHOLD)
            self.assertGreaterEqual(1.0, similarity)

    def test_recall_no_more_than_top_k(self):
        results = hook.recall("任意のクエリ", "test_session_int3", REAL_DB)
        self.assertLessEqual(len(results), hook.TOP_K)

    def test_recall_total_tokens_within_budget(self):
        results = hook.recall("ルールポリシー", "test_session_int4", REAL_DB)
        total = sum(hook._estimate_tokens(c) for _, c, _ in results)
        self.assertLessEqual(total, hook.TOKEN_BUDGET)

    def test_recall_dedup_suppresses_repeated_ids(self):
        """Re-injecting the same memory_id should be suppressed in the next call."""
        session_id = "test_dedup_session_071"
        state_file = hook._state_path(session_id)
        # Seed state with a recent turn that includes all possible memory IDs
        if state_file is not None:
            import sqlite3, sqlite_vec
            conn = sqlite3.connect(REAL_DB)
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            all_ids = [r[0] for r in conn.execute("SELECT memory_id FROM memories_vec LIMIT 50").fetchall()]
            conn.close()
            hook._save_state(state_file, {"recent_ids": [all_ids]})
            try:
                results = hook.recall("タスクのルール", session_id, REAL_DB)
                self.assertEqual(results, [], "All IDs suppressed; expected empty result")
            finally:
                if state_file.exists():
                    state_file.unlink()

    def test_latency_10_runs_warm(self):
        """Measure p50/p95 within a single warm process (10 sequential calls).

        First call loads model (cold). Subsequent calls reuse the singleton.
        This reflects steady-state performance within one hook process lifetime.
        """
        import statistics
        import time as _time

        # Reset model singleton to ensure clean measurement
        hook._embed_model = None

        times = []
        for i in range(10):
            t0 = _time.perf_counter()
            hook.recall("タスクのワークフローとルール", f"latency_test_{i}", REAL_DB)
            times.append(_time.perf_counter() - t0)

        p50_ms = statistics.median(times) * 1000
        p95_ms = sorted(times)[9] * 1000  # max of 10 = p100, conservatively called p95

        print(f"\n[LATENCY warm-process] p50={p50_ms:.1f}ms  p95={p95_ms:.1f}ms")

        # Warm-process gate: p95 < 5s
        self.assertLess(p95_ms, 5000, f"Warm-process p95={p95_ms:.1f}ms exceeds 5000ms")


if __name__ == "__main__":
    unittest.main()
