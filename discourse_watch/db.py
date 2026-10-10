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


@upgrade_table.register(description="Ignored topics")
async def upgrade_v2(conn: Connection) -> None:
    await conn.execute(
        """CREATE TABLE ignored_topic (
            room_id  TEXT   NOT NULL,
            topic_id BIGINT NOT NULL,
            title    TEXT   NOT NULL,
            PRIMARY KEY (room_id, topic_id)
        )"""
    )


@upgrade_table.register(description="Events sent into threads")
async def upgrade_v3(conn: Connection) -> None:
    await conn.execute(
        """CREATE TABLE thread_event (
            room_id  TEXT   NOT NULL,
            topic_id BIGINT NOT NULL,
            event_id TEXT   NOT NULL,
            PRIMARY KEY (room_id, event_id)
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

    async def get_topic_by_root(self, room_id: RoomID, event_id: EventID) -> TopicThread | None:
        row = await self.db.fetchrow(
            "SELECT room_id, topic_id, root_event_id, last_event_id, title FROM topic "
            "WHERE room_id=$1 AND root_event_id=$2",
            room_id,
            event_id,
        )
        return TopicThread(**row) if row else None

    async def delete_topic(self, room_id: RoomID, topic_id: int) -> None:
        await self.db.execute(
            "DELETE FROM topic WHERE room_id=$1 AND topic_id=$2", room_id, topic_id
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

    async def is_ignored(self, room_id: RoomID, topic_id: int) -> bool:
        return bool(
            await self.db.fetchval(
                "SELECT 1 FROM ignored_topic WHERE room_id=$1 AND topic_id=$2", room_id, topic_id
            )
        )

    async def ignore_topic(self, room_id: RoomID, topic_id: int, title: str) -> bool:
        """Add ``topic_id`` to the ignore list; return False if it was already there."""
        if await self.is_ignored(room_id, topic_id):
            return False
        await self.db.execute(
            "INSERT INTO ignored_topic (room_id, topic_id, title) VALUES ($1, $2, $3)",
            room_id,
            topic_id,
            title,
        )
        return True

    async def unignore_topic(self, room_id: RoomID, topic_id: int) -> bool:
        """Remove ``topic_id`` from the ignore list; return False if it was not there."""
        if not await self.is_ignored(room_id, topic_id):
            return False
        await self.db.execute(
            "DELETE FROM ignored_topic WHERE room_id=$1 AND topic_id=$2", room_id, topic_id
        )
        return True

    async def ignored_topics(self, room_id: RoomID) -> list[tuple[int, str]]:
        rows = await self.db.fetch(
            "SELECT topic_id, title FROM ignored_topic WHERE room_id=$1 ORDER BY topic_id",
            room_id,
        )
        return [(row["topic_id"], row["title"]) for row in rows]

    async def add_thread_event(self, room_id: RoomID, topic_id: int, event_id: EventID) -> None:
        await self.db.execute(
            "INSERT INTO thread_event (room_id, topic_id, event_id) VALUES ($1, $2, $3) "
            "ON CONFLICT (room_id, event_id) DO NOTHING",
            room_id,
            topic_id,
            event_id,
        )

    async def thread_events(self, room_id: RoomID, topic_id: int) -> list[EventID]:
        rows = await self.db.fetch(
            "SELECT event_id FROM thread_event WHERE room_id=$1 AND topic_id=$2",
            room_id,
            topic_id,
        )
        return [EventID(row["event_id"]) for row in rows]

    async def forget_thread_events(self, room_id: RoomID, topic_id: int) -> None:
        await self.db.execute(
            "DELETE FROM thread_event WHERE room_id=$1 AND topic_id=$2", room_id, topic_id
        )
