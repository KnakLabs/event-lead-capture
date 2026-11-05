"""A local stand-in for the SQS FIFO queue the deployed agents use.

The internal-tooling repo declares, per agent:

    "queue": { "fifo": true, "visibilityTimeoutSeconds": 360, "dlqMaxReceiveCount": 3 }

This reproduces those four semantics on SQLite so the local build behaves like
the deployed one, and the port is a driver swap rather than a redesign:

  * FIFO per message group — one in-flight message per group at a time, so a
    rep's badge photo is always processed before the note that follows it.
  * Deduplication by ID — Slack retries an event it thinks we failed; without
    this a slow badge read would create the lead twice.
  * Visibility timeout — a message a crashed worker was holding becomes
    available again instead of being lost.
  * Dead-letter after N receives — a message that keeps failing stops looping.

Only `send`, `receive`, `delete` and `release` are used, and each maps to one
SQS call, so swapping this for boto3 touches nothing above it.
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id                TEXT PRIMARY KEY,
    message_group_id  TEXT NOT NULL,
    dedupe_id         TEXT NOT NULL,
    body              TEXT NOT NULL,
    receive_count     INTEGER NOT NULL DEFAULT 0,
    visible_at        REAL NOT NULL DEFAULT 0,
    in_flight         INTEGER NOT NULL DEFAULT 0,
    enqueued_at       REAL NOT NULL
);
-- Deduplication has to outlive the message itself. SQS FIFO remembers a
-- deduplication ID for a time window whether or not the message was consumed;
-- tracking it on `messages` alone would forget the ID the moment a message was
-- acked, so a Slack retry arriving after a successful push would push again.
CREATE TABLE IF NOT EXISTS dedupe (
    dedupe_id TEXT PRIMARY KEY,
    seen_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dead_letters (
    id            TEXT PRIMARY KEY,
    body          TEXT NOT NULL,
    receive_count INTEGER NOT NULL,
    failed_at     REAL NOT NULL,
    last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_visible ON messages (visible_at, enqueued_at);
CREATE INDEX IF NOT EXISTS idx_dedupe_seen ON dedupe (seen_at);
"""

# How long a deduplication ID is remembered. SQS FIFO uses 5 minutes; we keep it
# longer because Slack's retry sequence can stretch past that, and a genuinely
# new capture always carries a new Slack event_id anyway.
DEDUPE_WINDOW_SECONDS = 15 * 60


@dataclass
class Message:
    id: str
    message_group_id: str
    body: dict[str, Any]
    receive_count: int


class TaskQueue:
    def __init__(
        self,
        db_path: str,
        *,
        visibility_timeout_seconds: int = 360,
        dlq_max_receive_count: int = 3,
        dedupe_window_seconds: int = DEDUPE_WINDOW_SECONDS,
    ) -> None:
        self.db_path = db_path
        self.visibility_timeout = visibility_timeout_seconds
        self.max_receive_count = dlq_max_receive_count
        self.dedupe_window = dedupe_window_seconds
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def send(self, *, message_group_id: str, dedupe_id: str, body: dict[str, Any]) -> bool:
        """Enqueue one message.

        Returns False if `dedupe_id` was seen within the dedupe window — whether
        or not that earlier message has since been processed.
        """
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute('DELETE FROM dedupe WHERE seen_at < ?', (now - self.dedupe_window,))
            try:
                conn.execute(
                    'INSERT INTO dedupe (dedupe_id, seen_at) VALUES (?, ?)', (dedupe_id, now)
                )
            except sqlite3.IntegrityError:
                logger.info('Duplicate message ignored: %s', dedupe_id)
                return False

            conn.execute(
                """INSERT INTO messages
                   (id, message_group_id, dedupe_id, body, enqueued_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (uuid.uuid4().hex, message_group_id, dedupe_id, json.dumps(body), now),
            )
            return True

    def receive(self) -> Message | None:
        """Claim the oldest visible message whose group has nothing in flight."""
        now = time.time()
        with self._lock, self._connect() as conn:
            # Expire the visibility lease of anything a dead worker was holding.
            conn.execute(
                'UPDATE messages SET in_flight = 0 WHERE in_flight = 1 AND visible_at <= ?',
                (now,),
            )
            row = conn.execute(
                """SELECT * FROM messages
                   WHERE in_flight = 0
                     AND visible_at <= ?
                     AND message_group_id NOT IN (
                         SELECT message_group_id FROM messages WHERE in_flight = 1
                     )
                   ORDER BY enqueued_at
                   LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None

            conn.execute(
                """UPDATE messages
                   SET in_flight = 1, receive_count = receive_count + 1, visible_at = ?
                   WHERE id = ?""",
                (now + self.visibility_timeout, row['id']),
            )
            return Message(
                id=row['id'],
                message_group_id=row['message_group_id'],
                body=json.loads(row['body']),
                receive_count=row['receive_count'] + 1,
            )

    def delete(self, message_id: str) -> None:
        """Acknowledge success."""
        with self._lock, self._connect() as conn:
            conn.execute('DELETE FROM messages WHERE id = ?', (message_id,))

    def release(self, message: Message, error: str = '') -> None:
        """Hand a failed message back, or dead-letter it once it's out of tries."""
        with self._lock, self._connect() as conn:
            if message.receive_count >= self.max_receive_count:
                conn.execute(
                    """INSERT OR REPLACE INTO dead_letters
                       (id, body, receive_count, failed_at, last_error)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        message.id,
                        json.dumps(message.body),
                        message.receive_count,
                        time.time(),
                        error[:2000],
                    ),
                )
                conn.execute('DELETE FROM messages WHERE id = ?', (message.id,))
                logger.error(
                    'Dead-lettered message after %d attempts: %s',
                    message.receive_count,
                    error[:300],
                )
            else:
                conn.execute(
                    'UPDATE messages SET in_flight = 0, visible_at = ? WHERE id = ?',
                    (time.time(), message.id),
                )

    def stats(self) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            return {
                'queued': conn.execute(
                    'SELECT COUNT(*) FROM messages WHERE in_flight = 0'
                ).fetchone()[0],
                'in_flight': conn.execute(
                    'SELECT COUNT(*) FROM messages WHERE in_flight = 1'
                ).fetchone()[0],
                'dead_letters': conn.execute('SELECT COUNT(*) FROM dead_letters').fetchone()[0],
            }
