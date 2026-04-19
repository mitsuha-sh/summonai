#!/usr/bin/env python3
import importlib
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path


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


class SchemaMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_test_stubs()

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "schema_migration_test.db"
        os.environ["SUMMONAI_MEMORY_DB"] = str(self.db_path)

        if "server" in sys.modules:
            self.server = importlib.reload(sys.modules["server"])
        else:
            self.server = importlib.import_module("server")

        self.custom_migrations = Path(self.tmpdir.name) / "migrations"
        self.custom_migrations.mkdir(parents=True, exist_ok=True)
        for src in sorted((Path(__file__).resolve().parent.parent / "db" / "migrations").glob("V*.sql")):
            shutil.copy2(src, self.custom_migrations / src.name)

        if self.db_path.exists():
            self.db_path.unlink()

        self._original_migrations_dir = self.server.MIGRATIONS_DIR
        self.server.MIGRATIONS_DIR = self.custom_migrations
        self.server.ensure_schema()

    def tearDown(self):
        self.server.MIGRATIONS_DIR = self._original_migrations_dir
        self.tmpdir.cleanup()

    def _fetch_versions(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT version, filename, applied_at, checksum FROM schema_versions ORDER BY version"
            ).fetchall()
        finally:
            conn.close()

    def test_applies_all_baseline_migrations(self):
        rows = self._fetch_versions()
        self.assertEqual([row[0] for row in rows], [1, 2, 3, 4, 5, 6, 7, 8, 9])

    def test_reapply_is_idempotent(self):
        before = self._fetch_versions()
        self.server.ensure_schema()
        after = self._fetch_versions()
        self.assertEqual(before, after)

    def test_detects_unapplied_and_applies_incrementally(self):
        v010 = self.custom_migrations / "V010_test_marker.sql"
        v010.write_text(
            """
            CREATE TABLE IF NOT EXISTS migration_test_marker (
                id INTEGER PRIMARY KEY,
                note TEXT
            );
            """.strip()
            + "\n",
            encoding="utf-8",
        )

        self.server.ensure_schema()

        rows = self._fetch_versions()
        self.assertEqual([row[0] for row in rows], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])

        conn = sqlite3.connect(self.db_path)
        try:
            marker_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_test_marker'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(marker_exists)

    def test_v006_cleanup_and_bucket_column_applied(self):
        conn = sqlite3.connect(self.db_path)
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(memories)").fetchall()
            }
        finally:
            conn.close()

        self.assertIn("memory_bucket", cols)
        self.assertNotIn("last_decayed_at", cols)

    def test_v007_memory_links_table_applied(self):
        conn = sqlite3.connect(self.db_path)
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(memory_links)").fetchall()
            }
        finally:
            conn.close()
        self.assertIn("relation_type", cols)
        self.assertIn("strength", cols)

    def test_v008_memories_recall_count_applied(self):
        conn = sqlite3.connect(self.db_path)
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(memories)").fetchall()
            }
        finally:
            conn.close()
        self.assertIn("recall_count", cols)

    def test_v009_goals_removed(self):
        conn = sqlite3.connect(self.db_path)
        try:
            memory_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(memories)").fetchall()
            }
            goals_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='goals'"
            ).fetchone()
        finally:
            conn.close()

        self.assertNotIn("goal_id", memory_cols)
        self.assertIsNone(goals_exists)


if __name__ == "__main__":
    unittest.main()
