#!/usr/bin/env python3
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


class _DummyEmbedding:
    def __init__(self, text: str):
        self._text = text

    def tobytes(self) -> bytes:
        return self._text.encode("utf-8")

    def tolist(self) -> list[float]:
        return [0.0] * 512


class _DummyModel:
    def __init__(self, *args, **kwargs):
        pass

    def encode(self, text: str) -> _DummyEmbedding:
        return _DummyEmbedding(text)


def _install_test_stubs() -> None:
    st_mod = types.ModuleType("sentence_transformers")
    st_mod.SentenceTransformer = _DummyModel
    sys.modules["sentence_transformers"] = st_mod


class ConversationMemoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmpdir.name) / "test_memory.db"
        os.environ["SUMMONAI_MEMORY_DB"] = str(cls.db_path)
        _install_test_stubs()

        import importlib

        cls.server = importlib.import_module("server")
        cls.server.ensure_schema()

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def _insert_chunk(
        self,
        session_id: str,
        chunk_index: int,
        ended_at: str,
        agent_id: str = "worker6",
        project: str = "summonai-memory-mcp",
        scope_type: str = "project",
        scope_id: str | None = None,
        recall_count: int = 0,
        retention_score: float = 1.0,
        content: str | None = None,
        summary: str | None = None,
    ) -> int:
        if scope_id is None:
            scope_id = project if project else "global"
        if content is None:
            content = f"content-{session_id}-{chunk_index}"
        if summary is None:
            summary = f"summary-{session_id}-{chunk_index}"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO conversation_sessions
                    (session_id, agent_id, project, task_id, started_at, ended_at, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    agent_id,
                    project,
                    "subtask-test",
                    ended_at,
                    ended_at,
                    "summary",
                ),
            )
            cur = conn.execute(
                """
                INSERT INTO conversation_chunks
                    (session_id, chunk_index, chunk_hash, agent_id, project, task_id,
                     started_at, ended_at, message_count, token_estimate, summary, content,
                     scope_type, scope_id, retention_score, recall_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    chunk_index,
                    f"{session_id}-{chunk_index}-hash",
                    agent_id,
                    project,
                    "subtask-test",
                    ended_at,
                    ended_at,
                    2,
                    10,
                    summary,
                    content,
                    scope_type,
                    scope_id,
                    retention_score,
                    recall_count,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def test_schema_contains_conversation_tables(self):
        conn = sqlite3.connect(self.db_path)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                ).fetchall()
            }
        finally:
            conn.close()

        self.assertIn("conversation_sessions", names)
        self.assertIn("conversation_chunks", names)
        self.assertIn("conversation_chunks_fts", names)

    def test_conversation_save_and_load_recent(self):
        transcript = """\
user: 今日の課題を確認したい
assistant: phase_oneのPhase1を実装する
user: まずschemaを追加する
assistant: conversation tablesを作る
user: 次にAPIを追加する
assistant: conversation_saveとconversation_load_recentを作る
user: stop hook連携も必要
assistant: 専用スクリプトで発火させる
"""
        result = self.server.conversation_save(
            session_id="sess-001",
            agent_id="worker4",
            project="summonai-memory-mcp",
            task_id="subtask_643a",
            transcript=transcript,
            ended_at="2026-03-26T13:00:00",
        )
        self.assertIn("conversation_save ok", result)

        raw_recent = self.server.conversation_load_recent(
            agent_id="worker4",
            project="summonai-memory-mcp",
            limit_chunks=6,
            since_days=30,
        )
        data = json.loads(raw_recent)
        self.assertGreaterEqual(len(data), 1)

        conn = sqlite3.connect(self.db_path)
        try:
            started_at = conn.execute(
                """
                SELECT started_at
                FROM conversation_chunks
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                ("sess-001",),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIn(started_at, {row["started_at"] for row in data})

    def test_conversation_load_recent_returns_only_min_fields(self):
        now = datetime.now().isoformat(timespec="seconds")
        self._insert_chunk(
            "sess-min-fields-1",
            0,
            now,
            content="minimal-fields-content",
            summary="minimal-fields-summary",
        )

        raw = self.server.conversation_load_recent(
            agent_id="worker6",
            project="summonai-memory-mcp",
            since_days=30,
            limit_chunks=10,
        )
        rows = json.loads(raw)
        target_rows = [r for r in rows if r["content"] in {"minimal-fields-content", "minimal-fields-summary"}]
        self.assertGreaterEqual(len(target_rows), 1)
        for row in target_rows:
            self.assertEqual(set(row.keys()), {"content", "started_at"})

    def test_chunk_hash_dedup(self):
        transcript = """\
user: A
assistant: B
user: C
assistant: D
"""
        self.server.conversation_save(
            session_id="sess-dedup",
            agent_id="worker4",
            project="summonai-memory-mcp",
            task_id="subtask_643a",
            transcript=transcript,
            ended_at="2026-03-26T13:10:00",
        )
        self.server.conversation_save(
            session_id="sess-dedup",
            agent_id="worker4",
            project="summonai-memory-mcp",
            task_id="subtask_643a",
            transcript=transcript,
            ended_at="2026-03-26T13:10:00",
        )

        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM conversation_chunks WHERE session_id = ?",
                ("sess-dedup",),
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(count, 1)

    def test_conversation_save_replaces_existing_chunks_for_same_session(self):
        first_transcript = """\
user: first marker
assistant: first reply
user: keep old?
assistant: should be replaced
"""
        second_transcript = """\
user: second marker
assistant: second reply
user: overwrite snapshot
assistant: old chunk must disappear
"""
        self.server.conversation_save(
            session_id="sess-replace-1",
            agent_id="worker4",
            project="summonai-memory-mcp",
            task_id="subtask-replace",
            transcript=first_transcript,
            ended_at="2026-04-04T10:00:00",
        )
        self.server.conversation_save(
            session_id="sess-replace-1",
            agent_id="worker4",
            project="summonai-memory-mcp",
            task_id="subtask-replace",
            transcript=second_transcript,
            ended_at="2026-04-04T10:05:00",
        )

        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT chunk_index, content, ended_at
                FROM conversation_chunks
                WHERE session_id = ?
                ORDER BY chunk_index
                """,
                ("sess-replace-1",),
            ).fetchall()
        finally:
            conn.close()

        self.assertGreaterEqual(len(rows), 1)
        combined = "\n".join(row[1] for row in rows)
        self.assertIn("second marker", combined)
        self.assertNotIn("first marker", combined)
        self.assertTrue(all(row[2] == "2026-04-04T10:05:00" for row in rows))

        loaded = json.loads(
            self.server.conversation_load_recent(
                agent_id="worker4",
                project="summonai-memory-mcp",
                since_days=365,
                limit_chunks=10,
            )
        )
        loaded_text = "\n".join(item["content"] for item in loaded)
        self.assertIn("second marker", loaded_text)
        self.assertNotIn("first marker", loaded_text)

    def test_existing_memory_apis_still_work(self):
        save_result = self.server.memory_save(
            content="既存API非破壊確認",
            memory_type="semantic",
            category="test",
            source_agent="worker4",
            tags_csv="test:phase1",
        )
        self.assertIn("Memory saved with id=", save_result)

        loaded = json.loads(self.server.memory_load(tags="test:phase1"))
        self.assertTrue(any(item["content"] == "既存API非破壊確認" for item in loaded))
        for row in loaded:
            self.assertEqual(set(row.keys()), {"id", "content", "tags"})

    def test_conversation_load_recent_orders_by_recency(self):
        now = datetime.now()
        one_day_ago = (now - timedelta(days=1)).isoformat(timespec="seconds")
        seven_days_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
        self._insert_chunk("sess-recency-old", 0, seven_days_ago, recall_count=0)
        self._insert_chunk("sess-recency-new", 0, one_day_ago, recall_count=0)

        raw = self.server.conversation_load_recent(
            agent_id="worker6",
            project="summonai-memory-mcp",
            since_days=30,
            limit_chunks=100,
        )
        rows = json.loads(raw)
        target = [
            r
            for r in rows
            if "sess-recency-old" in r["content"] or "sess-recency-new" in r["content"]
        ]
        self.assertEqual(len(target), 2)
        self.assertIn("sess-recency-new", target[0]["content"])

    def test_conversation_load_recent_newer_wins_over_high_retention(self):
        """Newer chunks must come first even when an older chunk has high recall_count."""
        now = datetime.now()
        one_day_ago = (now - timedelta(days=1)).isoformat(timespec="seconds")
        seven_days_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
        # Older chunk with high recall_count that would inflate retention_score
        self._insert_chunk("sess-highretention-old", 0, seven_days_ago, recall_count=100, retention_score=9.9)
        # Newer chunk with zero recall_count
        self._insert_chunk("sess-highretention-new", 0, one_day_ago, recall_count=0, retention_score=0.1)

        raw = self.server.conversation_load_recent(
            agent_id="worker6",
            project="summonai-memory-mcp",
            since_days=30,
            limit_chunks=100,
        )
        rows = json.loads(raw)
        target = [
            r
            for r in rows
            if "sess-highretention-old" in r["content"] or "sess-highretention-new" in r["content"]
        ]
        self.assertEqual(len(target), 2)
        # Newer chunk must win regardless of retention_score
        self.assertIn("sess-highretention-new", target[0]["content"])

    def test_conversation_load_recent_switches_detail_level_by_24h_boundary(self):
        now = datetime.now()
        within_24h = (now - timedelta(hours=23, minutes=59)).isoformat(timespec="seconds")
        over_24h = (now - timedelta(hours=24, minutes=1)).isoformat(timespec="seconds")
        summary_source = "summary-only-snapshot"
        fallback_source = "X" * 250

        self._insert_chunk(
            "sess-detail-full",
            0,
            within_24h,
            content="full-content-within-24h",
            summary=summary_source,
        )
        self._insert_chunk(
            "sess-detail-summary",
            0,
            over_24h,
            content=fallback_source,
            summary="",
        )

        raw = self.server.conversation_load_recent(
            agent_id="worker6",
            project="summonai-memory-mcp",
            since_days=7,
            limit_chunks=100,
        )
        rows = json.loads(raw)
        targets = {
            row["started_at"]: row
            for row in rows
            if row["started_at"] in {within_24h, over_24h}
        }
        self.assertEqual(len(targets), 2)

        full_row = targets[within_24h]
        self.assertEqual(full_row["content"], "full-content-within-24h")

        summary_row = targets[over_24h]
        self.assertEqual(summary_row["content"], fallback_source[:200])

    def test_conversation_load_recent_normalizes_newlines_in_response_only(self):
        now = datetime.now()
        within_24h = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        over_24h = (now - timedelta(days=2)).isoformat(timespec="seconds")
        full_multiline = "user: hello\nassistant: world\nuser: again"
        summary_multiline = "line1\nline2"

        self._insert_chunk(
            "sess-oneline-full",
            0,
            within_24h,
            content=full_multiline,
            summary="ignored-summary",
        )
        self._insert_chunk(
            "sess-oneline-summary",
            0,
            over_24h,
            content="content-not-used-for-summary-mode",
            summary=summary_multiline,
        )

        raw = self.server.conversation_load_recent(
            agent_id="worker6",
            project="summonai-memory-mcp",
            since_days=30,
            limit_chunks=100,
        )
        rows = json.loads(raw)
        by_started_at = {row["started_at"]: row["content"] for row in rows}
        self.assertEqual(
            by_started_at[within_24h],
            "user: hello assistant: world user: again",
        )
        self.assertEqual(by_started_at[over_24h], "line1 line2")

        conn = sqlite3.connect(self.db_path)
        try:
            stored = conn.execute(
                """
                SELECT content, summary
                FROM conversation_chunks
                WHERE session_id IN ('sess-oneline-full', 'sess-oneline-summary')
                ORDER BY session_id ASC
                """
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(stored[0][0], full_multiline)
        self.assertEqual(stored[1][1], summary_multiline)

    def test_conversation_chunk_older_than_31_days_gets_archived(self):
        old_ts = (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
        old_id = self._insert_chunk("sess-archive-1", 0, old_ts, recall_count=0)

        raw = self.server.conversation_load_recent(
            agent_id="worker6",
            project="summonai-memory-mcp",
            since_days=365,
            limit_chunks=20,
        )
        rows = json.loads(raw)
        self.assertFalse(any("sess-archive-1" in r["content"] for r in rows))

        conn = sqlite3.connect(self.db_path)
        try:
            archived_at = conn.execute(
                "SELECT archived_at FROM conversation_chunks WHERE id = ?",
                (old_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNotNone(archived_at)

    def test_conversation_load_recent_consolidates_within_24h(self):
        recent_ts = (datetime.now() - timedelta(hours=6)).isoformat(timespec="seconds")
        chunk_id = self._insert_chunk("sess-consolidate-1", 0, recent_ts, recall_count=0)

        raw = self.server.conversation_load_recent(
            agent_id="worker6",
            project="summonai-memory-mcp",
            since_days=7,
            limit_chunks=20,
        )
        _ = json.loads(raw)
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT recall_count, retention_score, consolidated_at
                FROM conversation_chunks
                WHERE id = ?
                """,
                (chunk_id,),
            ).fetchone()
        finally:
            conn.close()
        # recall_count must NOT be incremented
        self.assertEqual(row[0], 0)
        # consolidated_at must be set for chunks loaded within 24h
        self.assertIsNotNone(row[2])

    def test_conversation_scope_filters(self):
        now = datetime.now().isoformat(timespec="seconds")
        self._insert_chunk(
            "sess-scope-user",
            0,
            now,
            project="",
            scope_type="user",
            scope_id="global",
        )
        self._insert_chunk(
            "sess-scope-project",
            0,
            now,
            project="alpha",
            scope_type="project",
            scope_id="alpha",
        )

        raw_user = self.server.conversation_load_recent(
            agent_id="worker6",
            scope_type="user",
            scope_id="global",
            since_days=30,
            limit_chunks=10,
        )
        rows_user = json.loads(raw_user)
        self.assertTrue(any("sess-scope-user" in r["content"] for r in rows_user))
        self.assertFalse(any("sess-scope-project" in r["content"] for r in rows_user))

        raw_project = self.server.conversation_load_recent(
            agent_id="worker6",
            scope_type="project",
            scope_id="alpha",
            since_days=30,
            limit_chunks=10,
        )
        rows_project = json.loads(raw_project)
        self.assertTrue(any("sess-scope-project" in r["content"] for r in rows_project))

    def test_conversation_load_recent_without_project_crosses_projects_for_agent(self):
        now = datetime.now().isoformat(timespec="seconds")
        self._insert_chunk(
            "sess-cross-alpha",
            0,
            now,
            agent_id="shared-agent",
            project="alpha-project",
            scope_type="project",
            scope_id="alpha-project",
        )
        self._insert_chunk(
            "sess-cross-beta",
            0,
            now,
            agent_id="shared-agent",
            project="summonai",
            scope_type="project",
            scope_id="summonai",
        )

        raw = self.server.conversation_load_recent(
            agent_id="shared-agent",
            since_days=30,
            limit_chunks=10,
        )
        joined = "\n".join(item["content"] for item in json.loads(raw))
        self.assertIn("sess-cross-alpha", joined)
        self.assertIn("sess-cross-beta", joined)

        raw_project = self.server.conversation_load_recent(
            agent_id="shared-agent",
            project="summonai",
            since_days=30,
            limit_chunks=10,
        )
        project_joined = "\n".join(item["content"] for item in json.loads(raw_project))
        self.assertNotIn("sess-cross-alpha", project_joined)
        self.assertIn("sess-cross-beta", project_joined)

    def test_memory_save_uses_runtime_config_for_default_source_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / "memory.toml"
            config.write_text('agent_id = "shared-agent"\nproject = "summonai"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"SUMMONAI_MEMORY_CONFIG": str(config)}, clear=False):
                result = self.server.memory_save(
                    content="runtime config source agent smoke marker",
                    memory_type="semantic",
                    memory_bucket="knowledge",
                    tags_csv="runtime-config-test",
                )
        self.assertIn("Memory saved", result)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT source_agent, source_context
                FROM memories
                WHERE content = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                ("runtime config source agent smoke marker",),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["source_agent"], "shared-agent")
        self.assertIn("runtime_project=summonai", row["source_context"])

    def test_memory_save_auto_creates_semantic_sim_links(self):
        self.server.memory_save(
            content="alpha policy guardrail for release quality",
            memory_type="semantic",
            category="test",
            source_agent="worker6",
        )
        self.server.memory_save(
            content="alpha policy guardrail for deployment safety",
            memory_type="semantic",
            category="test",
            source_agent="worker6",
        )

        conn = sqlite3.connect(self.db_path)
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM memory_links WHERE relation_type = 'semantic_sim'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(cnt, 1)

    def test_manual_supports_link_and_spreading_activation(self):
        self.server.memory_save(
            content="alpha anchor only",
            memory_type="semantic",
            category="test",
            source_agent="worker6",
        )
        self.server.memory_save(
            content="beta linked only",
            memory_type="semantic",
            category="test",
            source_agent="worker6",
        )
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT id, content
                FROM memories
                WHERE content IN ('alpha anchor only', 'beta linked only')
                ORDER BY id
                """
            ).fetchall()
            alpha_id = rows[0][0]
            beta_id = rows[1][0]
        finally:
            conn.close()

        link_result = self.server.memory_link_add(
            source_memory_id=alpha_id,
            target_memory_id=beta_id,
            relation_type="supports",
            strength=1.0,
        )
        self.assertIn("memory_link_add ok", link_result)

        raw_no_spread = self.server.memory_search(
            query="alpha anchor only",
            top_k=5,
            enable_spreading=False,
        )
        rows_no_spread = json.loads(raw_no_spread)
        self.assertIn(alpha_id, {r["id"] for r in rows_no_spread})

        raw_spread = self.server.memory_search(
            query="alpha anchor only",
            top_k=5,
            enable_spreading=True,
            spreading_min_strength=0.9,
            spreading_boost=1.0,
        )
        rows_spread = json.loads(raw_spread)
        self.assertIn(beta_id, {r["id"] for r in rows_spread})

    def test_memory_save_uses_bucket_default_confidence(self):
        self.server.memory_save(
            content="confidence default check for code bucket",
            memory_type="procedural",
            memory_bucket="code",
            category="test",
            source_agent="worker6",
        )
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT confidence
                FROM memories
                WHERE content = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                ("confidence default check for code bucket",),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(float(row[0]), 0.95, places=3)

    def test_memory_save_excludes_bucket_name_from_tags(self):
        self.server.memory_save(
            content="bucket tag dedupe check",
            memory_type="semantic",
            memory_bucket="code",
            category="test",
            source_agent="worker6",
            tags_csv="code,architecture,code",
        )

        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT id
                FROM memories
                WHERE content = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                ("bucket tag dedupe check",),
            ).fetchone()
            tags = conn.execute(
                "SELECT tag FROM tags WHERE memory_id = ? ORDER BY tag ASC",
                (row[0],),
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual([tag[0] for tag in tags], ["architecture"])

    def test_memory_search_excludes_future_valid_from_unless_include_future(self):
        future_ts = (datetime.now() + timedelta(days=7)).isoformat(timespec="seconds")
        self.server.memory_save(
            content="future_policy_memory_only",
            memory_type="semantic",
            memory_bucket="knowledge",
            category="test",
            source_agent="worker6",
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE memories
                SET valid_from = ?, valid_until = NULL
                WHERE content = ?
                """,
                (future_ts, "future_policy_memory_only"),
            )
            conn.commit()
        finally:
            conn.close()

        raw_default = self.server.memory_search(
            query="future_policy_memory_only",
            top_k=5,
            include_future=False,
        )
        if raw_default.startswith("No memories found"):
            default_rows = []
        else:
            default_rows = json.loads(raw_default)
        self.assertNotIn("future_policy_memory_only", {r["content"] for r in default_rows})

        raw_with_future = self.server.memory_search(
            query="future_policy_memory_only",
            top_k=5,
            include_future=True,
        )
        with_future_rows = json.loads(raw_with_future)
        self.assertIn("future_policy_memory_only", {r["content"] for r in with_future_rows})
        for row in with_future_rows:
            self.assertEqual(set(row.keys()), {"id", "content", "tags"})

    def test_memory_search_tri_prefers_high_rank_signals(self):
        self.server.memory_save(
            content="tri rank anchor target memory high",
            memory_type="semantic",
            memory_bucket="knowledge",
            category="test",
            source_agent="worker6",
            importance=9,
            confidence=0.95,
        )
        self.server.memory_save(
            content="tri rank anchor target memory low",
            memory_type="semantic",
            memory_bucket="knowledge",
            category="test",
            source_agent="worker6",
            importance=3,
            confidence=0.40,
        )

        now_ts = datetime.now().isoformat(timespec="seconds")
        old_ts = (datetime.now() - timedelta(days=120)).isoformat(timespec="seconds")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE memories
                SET access_count = 12, recall_count = 9, last_accessed_at = ?
                WHERE content = ?
                """,
                (now_ts, "tri rank anchor target memory high"),
            )
            conn.execute(
                """
                UPDATE memories
                SET access_count = 0, recall_count = 0, last_accessed_at = ?
                WHERE content = ?
                """,
                (old_ts, "tri rank anchor target memory low"),
            )
            conn.commit()
        finally:
            conn.close()

        raw = self.server.memory_search(
            query="tri rank anchor target memory",
            top_k=2,
            enable_spreading=False,
        )
        rows = json.loads(raw)
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0]["content"], "tri rank anchor target memory high")
        for row in rows:
            self.assertEqual(set(row.keys()), {"id", "content", "tags"})

    def test_memory_search_normalizes_newlines_in_response_only(self):
        multiline = "first line\nsecond line\nthird line"
        self.server.memory_save(
            content=multiline,
            memory_type="semantic",
            memory_bucket="knowledge",
            category="test",
            source_agent="worker6",
            tags_csv="newline:test",
        )

        raw = self.server.memory_search(query="first line", top_k=5)
        rows = json.loads(raw)
        matched = [r for r in rows if r["content"] == "first line second line third line"]
        self.assertGreaterEqual(len(matched), 1)

        conn = sqlite3.connect(self.db_path)
        try:
            stored = conn.execute(
                "SELECT content FROM memories WHERE content = ? ORDER BY id DESC LIMIT 1",
                (multiline,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(stored)
        self.assertEqual(stored[0], multiline)

    def test_recall_search_tri_rerank_changes_order(self):
        prompt_vec = self.server.np.array([1.0, 0.0], dtype=self.server.np.float32)

        def _vec(x: float, y: float) -> bytes:
            return self.server.np.array([x, y], dtype=self.server.np.float32).tobytes()

        embeddings = {
            1: _vec(0.92, 0.39191836),   # A: high sim + fresh + high rank
            2: _vec(0.95, 0.31224990),   # B: high sim + stale + low rank
            3: _vec(0.80, 0.60000000),   # C: mid sim + fresh + high rank
            4: _vec(0.82, 0.57236352),   # D: mid sim + stale + low rank
        }

        now = datetime.now()
        fresh = now.isoformat(timespec="seconds")
        stale = (now - timedelta(days=365)).isoformat(timespec="seconds")

        memory_rows = {
            1: {
                "content": "A",
                "importance": 9,
                "confidence": 0.95,
                "access_count": 10,
                "recall_count": 10,
                "emotional_impact": 9.0,
                "last_accessed_at": fresh,
                "created_at": fresh,
            },
            2: {
                "content": "B",
                "importance": 2,
                "confidence": 0.30,
                "access_count": 0,
                "recall_count": 0,
                "emotional_impact": 0.0,
                "last_accessed_at": stale,
                "created_at": stale,
            },
            3: {
                "content": "C",
                "importance": 9,
                "confidence": 0.95,
                "access_count": 10,
                "recall_count": 10,
                "emotional_impact": 9.0,
                "last_accessed_at": fresh,
                "created_at": fresh,
            },
            4: {
                "content": "D",
                "importance": 2,
                "confidence": 0.30,
                "access_count": 0,
                "recall_count": 0,
                "emotional_impact": 0.0,
                "last_accessed_at": stale,
                "created_at": stale,
            },
        }

        class _FakeCursor:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

            def fetchone(self):
                return self._rows[0] if self._rows else None

        class _FakeConn:
            def execute(self, sql, params=()):
                if "FROM memories_vec mv" in sql:
                    return _FakeCursor([{"memory_id": 1}, {"memory_id": 2}, {"memory_id": 3}, {"memory_id": 4}])
                if "SELECT embedding FROM memories_vec" in sql:
                    memory_id = int(params[0])
                    return _FakeCursor([{"embedding": embeddings[memory_id]}])
                if "SELECT content, importance, confidence, access_count, recall_count" in sql:
                    memory_id = int(params[0])
                    return _FakeCursor([memory_rows[memory_id]])
                if "SELECT content FROM memories" in sql:
                    memory_id = int(params[0])
                    return _FakeCursor([{"content": memory_rows[memory_id]["content"]}])
                raise AssertionError(f"unexpected query: {sql}")

            def close(self):
                return None

        with mock.patch.object(self.server, "get_db", return_value=_FakeConn()), \
             mock.patch.object(self.server, "_embed_model") as mock_model, \
             mock.patch.object(self.server, "_RECALL_TOP_K", 4), \
             mock.patch.object(self.server, "_RECALL_TOKEN_BUDGET", 10_000):
            mock_model.encode.return_value = prompt_vec
            results = self.server._recall_search("tri recall ranking")

        tri_order = [memory_id for memory_id, _, _ in results]
        self.assertEqual(tri_order, [1, 3, 2, 4])

        similarity_only_order = sorted(
            [1, 2, 3, 4],
            key=lambda mid: float(self.server.np.dot(prompt_vec, self.server.np.frombuffer(embeddings[mid], dtype=self.server.np.float32))),
            reverse=True,
        )
        self.assertEqual(similarity_only_order, [2, 1, 4, 3])
        self.assertNotEqual(tri_order, similarity_only_order)

    def test_memory_load_returns_min_fields_and_normalizes_newlines_in_response_only(self):
        multiline = "load line1\nload line2"
        self.server.memory_save(
            content=multiline,
            memory_type="semantic",
            memory_bucket="knowledge",
            category="test",
            source_agent="worker6",
            tags_csv="load:test",
        )

        raw = self.server.memory_load(tags="load:test")
        rows = json.loads(raw)
        target = [r for r in rows if r["content"] == "load line1 load line2"]
        self.assertGreaterEqual(len(target), 1)
        for row in rows:
            self.assertEqual(set(row.keys()), {"id", "content", "tags"})

        conn = sqlite3.connect(self.db_path)
        try:
            stored = conn.execute(
                "SELECT content FROM memories WHERE content = ? ORDER BY id DESC LIMIT 1",
                (multiline,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(stored)
        self.assertEqual(stored[0], multiline)

if __name__ == "__main__":
    unittest.main()
