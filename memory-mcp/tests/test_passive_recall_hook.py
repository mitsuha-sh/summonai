#!/usr/bin/env python3
"""Tests for passive_recall_hook.py (socket-client architecture).

Unit tests: mock socket/_query_server, test state/dedup/output format.
Socket integration tests: real mini socket server, no model needed.
Server integration tests: real socket from running MCP server (optional).
"""

from __future__ import annotations

import io
import json
import os
import socket
import statistics
import sys
import tempfile
import threading
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
# Unit tests: helpers
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


# ---------------------------------------------------------------------------
# Unit tests: state file
# ---------------------------------------------------------------------------

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
            old_time = time.time() - 25 * 3600
            os.utime(old_file, (old_time, old_time))

            recent_file = Path(tmpdir) / "summonai_passive_recall_recent.json"
            recent_file.write_text("{}", encoding="utf-8")

            with mock.patch("tempfile.gettempdir", return_value=tmpdir):
                hook._cleanup_stale_state_files()

            self.assertFalse(old_file.exists())
            self.assertTrue(recent_file.exists())


# ---------------------------------------------------------------------------
# Unit tests: main() parsing and output — mock _query_server
# ---------------------------------------------------------------------------

class TestMainParsing(unittest.TestCase):
    def _run_main(self, stdin_text: str, server_results=None) -> tuple[int, str]:
        if server_results is None:
            server_results = []
        with mock.patch("sys.stdin", io.StringIO(stdin_text)), \
             mock.patch("builtins.print") as mock_print, \
             mock.patch.object(hook, "_query_server", return_value=server_results), \
             mock.patch.object(hook, "_state_path", return_value=None):
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

    def test_valid_payload_calls_query_server(self):
        payload = {"prompt": "test query", "session_id": "sess1"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            with mock.patch.object(
                hook, "_query_server", return_value=[(42, "memory content", 0.85)]
            ) as mock_qs:
                with mock.patch("builtins.print"):
                    rc = hook.main()
        self.assertEqual(rc, 0)
        mock_qs.assert_called_once_with("test query")

    def test_recall_output_format(self):
        payload = {"prompt": "hello", "session_id": "s1"}
        _, output = self._run_main(
            json.dumps(payload),
            server_results=[(7, "content text", 0.75)],
        )
        self.assertIn("[RECALL]", output)
        self.assertIn("memory_id=7", output)
        self.assertIn("similarity=0.750", output)
        self.assertIn("content text", output)

    def test_empty_server_results_no_output(self):
        payload = {"prompt": "hello", "session_id": "s1"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            with mock.patch.object(hook, "_query_server", return_value=[]):
                with mock.patch("builtins.print") as mock_print:
                    rc = hook.main()
        self.assertEqual(rc, 0)
        mock_print.assert_not_called()

    def test_query_server_exception_returns_0(self):
        payload = {"prompt": "hello", "session_id": "s1"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            with mock.patch.object(hook, "_query_server", side_effect=RuntimeError("boom")):
                with mock.patch("builtins.print"):
                    rc = hook.main()
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Unit tests: recall() dedup logic — mock _query_server
# ---------------------------------------------------------------------------

class TestRecallDedup(unittest.TestCase):
    def test_dedup_suppresses_recently_seen_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = "dedup_test_session"
            state_file = Path(tmpdir) / f"summonai_passive_recall_{session_id}.json"
            hook._save_state(state_file, {"recent_ids": [[7]]})

            with mock.patch(
                "tempfile.gettempdir", return_value=tmpdir
            ), mock.patch.object(
                hook, "_query_server", return_value=[(7, "already seen", 0.9)]
            ):
                results = hook.recall("any query", session_id)
            self.assertEqual(results, [])

    def test_dedup_allows_unseen_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = "dedup_allow_session"
            state_file = Path(tmpdir) / f"summonai_passive_recall_{session_id}.json"
            hook._save_state(state_file, {"recent_ids": [[7]]})

            with mock.patch(
                "tempfile.gettempdir", return_value=tmpdir
            ), mock.patch.object(
                hook, "_query_server", return_value=[(42, "new memory", 0.9)]
            ):
                results = hook.recall("any query", session_id)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][0], 42)

    def test_token_budget_respected(self):
        long_content = "x" * 2001  # 500 tokens
        with mock.patch.object(
            hook, "_query_server", return_value=[(1, long_content, 0.9), (2, "short", 0.8)]
        ):
            results = hook.recall("query", "budget_session")
        # First entry alone exceeds budget, so only it (or nothing if it busts) is returned
        total = sum(hook._estimate_tokens(c) for _, c, _ in results)
        self.assertLessEqual(total, hook.TOKEN_BUDGET)

    def test_sliding_window_persists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = "sliding_window_session"
            with mock.patch(
                "tempfile.gettempdir", return_value=tmpdir
            ), mock.patch.object(
                hook, "_query_server", return_value=[(99, "memory", 0.9)]
            ):
                hook.recall("query", session_id)
                state_file = hook._state_path(session_id)
            assert state_file is not None
            real_state_file = Path(tmpdir) / state_file.name
            state = hook._load_state(real_state_file)
            self.assertIn([99], state["recent_ids"])


# ---------------------------------------------------------------------------
# Socket integration tests: real mini server, no model needed
# ---------------------------------------------------------------------------

def _start_mock_socket_server(sock_path: str, responses: list[dict]) -> threading.Thread:
    """Spin up a tiny Unix socket server that returns canned responses."""
    response_iter = iter(responses)

    def serve():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.bind(sock_path)
            s.listen(4)
            s.settimeout(5.0)
            for _ in range(len(responses)):
                try:
                    conn, _ = s.accept()
                    with conn:
                        buf = b""
                        conn.settimeout(2.0)
                        while b"\n" not in buf:
                            chunk = conn.recv(4096)
                            if not chunk:
                                break
                            buf += chunk
                        resp = next(response_iter, {"results": []})
                        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                except Exception:
                    break

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


class TestSocketIntegration(unittest.TestCase):
    """Test hook client against a real Unix socket mock server."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _sock_path(self, name: str) -> str:
        return str(Path(self.tmpdir) / name)

    def test_query_server_returns_results(self):
        sock_path = self._sock_path("summonai_recall_test1.sock")
        expected = {"results": [[10, "hello memory", 0.80]]}
        _start_mock_socket_server(sock_path, [expected])
        time.sleep(0.05)  # let server bind

        with mock.patch.object(hook, "_find_sockets", return_value=[Path(sock_path)]):
            results = hook._query_server("test prompt")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], 10)
        self.assertAlmostEqual(results[0][2], 0.80)

    def test_query_server_empty_results(self):
        sock_path = self._sock_path("summonai_recall_test2.sock")
        _start_mock_socket_server(sock_path, [{"results": []}])
        time.sleep(0.05)

        with mock.patch.object(hook, "_find_sockets", return_value=[Path(sock_path)]):
            results = hook._query_server("test prompt")

        self.assertEqual(results, [])

    def test_query_server_no_sockets_returns_empty(self):
        with mock.patch.object(hook, "_find_sockets", return_value=[]):
            results = hook._query_server("test prompt")
        self.assertEqual(results, [])

    def test_query_server_falls_back_on_bad_socket(self):
        """First socket doesn't exist; second socket works."""
        bad_path = Path(self.tmpdir) / "summonai_recall_bad.sock"  # doesn't exist
        good_path = self._sock_path("summonai_recall_good.sock")
        _start_mock_socket_server(good_path, [{"results": [[5, "ok", 0.7]]}])
        time.sleep(0.05)

        with mock.patch.object(hook, "_find_sockets", return_value=[bad_path, Path(good_path)]):
            results = hook._query_server("prompt")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], 5)

    def test_latency_10_runs_warm_socket(self):
        """p95 of 10 hook calls against a mock socket must be < 5000ms."""
        sock_path = self._sock_path("summonai_recall_latency.sock")
        responses = [{"results": [[i + 1, f"memory {i}", 0.9]]} for i in range(10)]
        _start_mock_socket_server(sock_path, responses)
        time.sleep(0.05)

        times = []
        for _ in range(10):
            t0 = time.perf_counter()
            with mock.patch.object(hook, "_find_sockets", return_value=[Path(sock_path)]):
                hook._query_server("タスクのワークフロー")
            times.append(time.perf_counter() - t0)

        p50_ms = statistics.median(times) * 1000
        p95_ms = sorted(times)[9] * 1000

        print(f"\n[LATENCY mock-socket] p50={p50_ms:.1f}ms  p95={p95_ms:.1f}ms")
        self.assertLess(p95_ms, 5000, f"Mock socket p95={p95_ms:.1f}ms exceeds 5000ms")

    def test_main_end_to_end_with_mock_socket(self):
        sock_path = self._sock_path("summonai_recall_e2e.sock")
        _start_mock_socket_server(sock_path, [{"results": [[3, "e2e content", 0.77]]}])
        time.sleep(0.05)

        payload = json.dumps({"prompt": "end to end", "session_id": "e2e_sess"})
        captured = []
        with mock.patch("sys.stdin", io.StringIO(payload)), \
             mock.patch("builtins.print", side_effect=lambda *a, **kw: captured.append(a)), \
             mock.patch.object(hook, "_find_sockets", return_value=[Path(sock_path)]), \
             mock.patch.object(hook, "_state_path", return_value=None):
            rc = hook.main()

        self.assertEqual(rc, 0)
        output = "\n".join(" ".join(str(x) for x in line) for line in captured)
        self.assertIn("[RECALL]", output)
        self.assertIn("memory_id=3", output)
        self.assertIn("e2e content", output)


if __name__ == "__main__":
    unittest.main()
