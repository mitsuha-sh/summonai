#!/usr/bin/env python3
import sqlite3

import sqlite_vec
from sentence_transformers import SentenceTransformer

DB_PATH = "db/summonai_memory.db"
MODEL_NAME = "cl-nagoya/ruri-v3-130m"
BATCH_SIZE = 50


def main() -> None:
    db = sqlite3.connect(DB_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)

    db.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
            memory_id INTEGER PRIMARY KEY,
            embedding float[512]
        )
        """
    )

    existing_ids = set(r[0] for r in db.execute("SELECT memory_id FROM memories_vec").fetchall())
    all_mems = db.execute("SELECT id, content FROM memories").fetchall()
    to_process = [(mid, content) for mid, content in all_mems if mid not in existing_ids]

    print(f"Total: {len(all_mems)}, Already: {len(existing_ids)}, To process: {len(to_process)}")
    if not to_process:
        print("Nothing to backfill.")
        return

    model = SentenceTransformer(MODEL_NAME)

    for i in range(0, len(to_process), BATCH_SIZE):
        batch = to_process[i : i + BATCH_SIZE]
        ids = [mid for mid, _ in batch]
        texts = [content for _, content in batch]
        embeddings = model.encode(texts, show_progress_bar=False)

        for mid, emb in zip(ids, embeddings):
            db.execute(
                "INSERT INTO memories_vec (memory_id, embedding) VALUES (?, ?)",
                (mid, emb.tobytes()),
            )
        db.commit()
        print(f"  Batch {i // BATCH_SIZE + 1}: {len(batch)} embedded")

    print(f"Done: {len(to_process)} embeddings generated")


if __name__ == "__main__":
    main()
