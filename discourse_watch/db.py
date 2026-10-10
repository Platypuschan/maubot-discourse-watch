from __future__ import annotations

from dataclasses import dataclass

from mautrix.types import EventID, RoomID
from mautrix.util.async_db import Connection, Database, UpgradeTable

upgrade_table = UpgradeTable()


@upgrade_table.register(description="Initial revision")
async def upgrade_v1(conn: Connection) -> None:
    await conn.execute(
        """CREATE TABLE state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )"""
    )
    await conn.execute(
        """CREATE TABLE topic (
            room_id       TEXT   NOT NULL,
            topic_id      BIGINT NOT NULL,
            root_event_id TEXT   NOT NULL,
            last_event_id TEXT   NOT NULL,
            title         TEXT   NOT NULL,
            PRIMARY KEY (room_id, topic_id)
        )"""
    )
    await conn.execute(
        """CREATE TABLE sent_post (
            room_id  TEXT   NOT NULL,
            post_id  BIGINT NOT NULL,
            event_id TEXT   NOT NULL,
            PRIMARY KEY (room_id, post_id)
        )"""
    )


@dataclass
class TopicThread:
    room_id: RoomID
    topic_id: int
    root_event_id: EventID
    last_event_id: EventID
    title: str


class Store:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_state(self, key: str) -> str | None:
        return await self.db.fetchval("SELECT value FROM state WHERE key=$1", key)

    async def set_state(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO state (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            key,
            value,
        )

    async def get_topic(self, room_id: RoomID, topic_id: int) -> TopicThread | None:
        row = await self.db.fetchrow(
            "SELECT room_id, topic_id, root_event_id, last_event_id, title FROM topic "
            "WHERE room_id=$1 AND topic_id=$2",
            room_id,
            topic_id,
        )
        return TopicThread(**row) if row else None

    async def put_topic(self, thread: TopicThread) -> None:
        await self.db.execute(
            "INSERT INTO topic (room_id, topic_id, root_event_id, last_event_id, title) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (room_id, topic_id) DO UPDATE SET root_event_id=excluded.root_event_id, "
            "last_event_id=excluded.last_event_id, title=excluded.title",
            thread.room_id,
            thread.topic_id,
            thread.root_event_id,
            thread.last_event_id,
            thread.title,
        )

    async def count_topics(self, room_id: RoomID) -> int:
        return await self.db.fetchval("SELECT COUNT(*) FROM topic WHERE room_id=$1", room_id)

    async def is_sent(self, room_id: RoomID, post_id: int) -> bool:
        return bool(
            await self.db.fetchval(
                "SELECT 1 FROM sent_post WHERE room_id=$1 AND post_id=$2", room_id, post_id
            )
        )

    async def mark_sent(self, room_id: RoomID, post_id: int, event_id: EventID) -> None:
        await self.db.execute(
            "INSERT INTO sent_post (room_id, post_id, event_id) VALUES ($1, $2, $3) "
            "ON CONFLICT (room_id, post_id) DO NOTHING",
            room_id,
            post_id,
            event_id,
        )
