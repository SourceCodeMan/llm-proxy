import json
from datetime import datetime
from pathlib import Path

import aiosqlite

DB_PATH = Path(__file__).parent / "proxy.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    request_json TEXT NOT NULL,
    callback_url TEXT,
    status       TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS job_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_queue_id    INTEGER,
    queued_at       TIMESTAMP,
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    backend         TEXT,
    model_requested TEXT,
    model_used      TEXT,
    duration_ms     INTEGER,
    request_json    TEXT,
    response_json   TEXT,
    error           TEXT
);

INSERT OR IGNORE INTO config (key, value) VALUES ('flag', 'send');
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def get_flag() -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM config WHERE key='flag'") as cur:
            row = await cur.fetchone()
            return row[0] if row else "send"


async def set_flag(value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES ('flag', ?)", (value,)
        )
        await db.commit()


async def enqueue_job(request_json: str, callback_url: str = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO job_queue (request_json, callback_url) VALUES (?, ?)",
            (request_json, callback_url),
        )
        await db.commit()
        return cur.lastrowid


async def get_next_pending_job():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM job_queue WHERE status='pending' ORDER BY created_at LIMIT 1"
        ) as cur:
            return await cur.fetchone()


async def mark_job_processing(job_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE job_queue SET status='processing' WHERE id=?", (job_id,)
        )
        await db.commit()


async def complete_job(
    *,
    job_id: int,
    response_json: str,
    backend: str,
    model_requested: str,
    model_used: str,
    duration_ms: int,
    queued_at: str,
    started_at: str,
    request_json: str,
    error: str = None,
):
    status = "failed" if error else "done"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE job_queue SET status=? WHERE id=?", (status, job_id)
        )
        await db.execute(
            """INSERT INTO job_history
               (job_queue_id, queued_at, started_at, completed_at, backend,
                model_requested, model_used, duration_ms, request_json, response_json, error)
               VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id, queued_at, started_at, backend,
                model_requested, model_used, duration_ms,
                request_json, response_json, error,
            ),
        )
        await db.commit()


async def record_direct(
    *,
    request_json: str,
    response_json: str,
    backend: str,
    model_requested: str,
    model_used: str,
    duration_ms: int,
):
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO job_history
               (queued_at, started_at, completed_at, backend,
                model_requested, model_used, duration_ms, request_json, response_json)
               VALUES (?, ?, datetime('now'), ?, ?, ?, ?, ?, ?)""",
            (now, now, backend, model_requested, model_used, duration_ms,
             request_json, response_json),
        )
        await db.commit()


async def get_job_by_id(job_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM job_queue WHERE id=?", (job_id,)
        ) as cur:
            return await cur.fetchone()


async def get_queue_depth() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM job_queue WHERE status='pending'"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_history(limit: int = 50):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM job_history ORDER BY completed_at DESC LIMIT ?", (limit,)
        ) as cur:
            return await cur.fetchall()
