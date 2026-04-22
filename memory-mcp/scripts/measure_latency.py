#!/usr/bin/env python3
"""Latency measurement for Phase 2 (Unix socket IPC) passive recall hook.

Runs two measurements:
1. In-process: server socket round-trip only (excludes Python startup)
2. Subprocess: full hook cold-start (Python startup + socket query)
"""

from __future__ import annotations

import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
REPO_ROOT = SCRIPTS_DIR.parent.parent
SERVER_PY = SCRIPTS_DIR.parent / "server.py"
HOOK_PY = SCRIPTS_DIR / "passive_recall_hook.py"
PYTHON = sys.executable
DB_PATH = str(REPO_ROOT / ".data" / "summonai_memory.db")


def _load_server_module():
    """Import server.py, loading the SentenceTransformer model."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("server", str(SERVER_PY))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def measure_socket_roundtrip(server_mod, n: int = 10) -> tuple[float, float]:
    """In-process: measure N query round-trips (model already loaded)."""
    sock_path = str(server_mod._RECALL_SOCKET_PATH)
    query = json.dumps({"prompt": "タスクのワークフローとルール"}) + "\n"
    request = query.encode("utf-8")

    times = []
    for i in range(n):
        t0 = time.perf_counter()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5.0)
            s.connect(sock_path)
            s.sendall(request)
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        elapsed = time.perf_counter() - t0
        times.append(elapsed * 1000)
        print(f"  run {i+1:2d}: {elapsed*1000:.1f}ms", flush=True)

    p50 = statistics.median(times)
    p95 = sorted(times)[int(n * 0.95) - 1] if n >= 20 else max(times)
    print(f"\n[LATENCY socket-roundtrip] p50={p50:.1f}ms  p95={p95:.1f}ms", flush=True)
    return p50, p95


def measure_subprocess_coldstart(sock_path: str, n: int = 10) -> tuple[float, float]:
    """Subprocess: full hook cold-start measured as wall-clock time."""
    print(f"\nMeasuring subprocess cold-start ({n} runs)...", flush=True)
    payload = json.dumps({
        "prompt": "タスクのワークフローとルール",
        "session_id": f"latency_measure_{os.getpid()}",
    })

    times = []
    for i in range(n):
        t0 = time.perf_counter()
        proc = subprocess.run(
            [PYTHON, str(HOOK_PY)],
            input=payload,
            capture_output=True,
            text=True,
        )
        elapsed = time.perf_counter() - t0
        times.append(elapsed * 1000)
        output_preview = proc.stdout[:60].replace("\n", " ") or "(no output)"
        print(f"  run {i+1:2d}: {elapsed*1000:.1f}ms  stdout={output_preview}", flush=True)

    p50 = statistics.median(times)
    p95 = sorted(times)[int(n * 0.95) - 1] if n >= 20 else max(times)
    print(f"\n[LATENCY subprocess-coldstart] p50={p50:.1f}ms  p95={p95:.1f}ms", flush=True)
    return p50, p95


if __name__ == "__main__":
    print("=== Phase 2 Latency Measurement ===")
    print(f"Python: {PYTHON}")

    # Load server module once (loads model ~7s)
    print("\nLoading server module (model load ~7s)...", flush=True)
    os.environ.setdefault("SUMMONAI_MEMORY_DB", DB_PATH)
    server = _load_server_module()

    print("Starting socket server...", flush=True)
    server.start_recall_socket_server()
    time.sleep(0.2)  # let socket bind

    sock_path = str(server._RECALL_SOCKET_PATH)
    print(f"Socket: {sock_path}")

    # Part 1: In-process round-trip
    print("\n--- Part 1: Socket round-trip (model warm in process) ---")
    rtt_p50, rtt_p95 = measure_socket_roundtrip(server, 10)

    # Part 2: Subprocess cold-start (socket still alive)
    print(f"\n--- Part 2: Subprocess cold-start (hook connects to socket) ---")
    sub_p50, sub_p95 = measure_subprocess_coldstart(sock_path, 10)

    # Clean up socket
    try:
        server._RECALL_SOCKET_PATH.unlink()
    except Exception:
        pass

    print("\n=== Summary ===")
    print(f"Socket round-trip:    p50={rtt_p50:.1f}ms  p95={rtt_p95:.1f}ms")
    print(f"Subprocess coldstart: p50={sub_p50:.1f}ms  p95={sub_p95:.1f}ms")
    gate_ms = 5000
    result = "PASS" if sub_p95 < gate_ms else "FAIL"
    print(f"Gate (p95 < {gate_ms}ms): {result}")
