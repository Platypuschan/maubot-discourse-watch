from __future__ import annotations

import asyncio
import time
from html import escape
from typing import Awaitable, Callable, Type, TypeVar

from maubot import MessageEvent, Plugin
from maubot.handlers import command, event
from mautrix.api import Method, Path
from mautrix.errors import MForbidden, MLimitExceeded
from mautrix.types import (
    EventID,
    EventType,
    Format,
    MessageType,
    RedactionEvent,
    RelationType,
    RoomID,
    TextMessageEventContent,
)
from mautrix.util.async_db import UpgradeTable
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .db import Store, TopicThread, upgrade_table
from .discourse import DiscourseClient
from .mirror import Mirror, MirrorSettings, parse_topic_ref


T = TypeVar("T")


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("base_url")
        helper.copy("room_id")
        helper.copy("poll_interval")
        helper.copy("backfill.topics")
        helper.copy("backfill.posts_per_topic")
        helper.copy("categories.include")
        helper.copy("categories.exclude")
        helper.copy("max_post_chars")
        helper.copy("max_pages_per_poll")
        helper.copy("user_agent")
        helper.copy("command_prefix")
        helper.copy("admins")


class DiscourseWatchBot(Plugin):
    store: Store
    mirror: Mirror | None
    poll_task: asyncio.Task | None
    poll_lock: asyncio.Lock
    wakeup: asyncio.Event
    last_poll: float | None
    last_error: str | None
    # Seconds per backoff step; tests make it tiny.
    backoff_unit: float = 1.0

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @classmethod
    def get_db_upgrade_table(cls) -> UpgradeTable | None:
        return upgrade_table

    async def start(self) -> None:
        self.config.load_and_update()
        self.store = Store(self.database)
        self.poll_lock = asyncio.Lock()
        self.wakeup = asyncio.Event()
        self.last_poll = None
        self.last_error = None
        self.mirror = self.build_mirror()
        self.poll_task = asyncio.create_task(self.poll_loop())

    async def stop(self) -> None:
        if self.poll_task:
            self.poll_task.cancel()
            try:
                await self.poll_task
            except asyncio.CancelledError:
                pass
            self.poll_task = None

    def on_external_config_update(self) -> None:
        self.config.load_and_update()
        self.mirror = self.build_mirror()
        self.wakeup.set()

    def build_mirror(self) -> Mirror | None:
        room_id = (self.config["room_id"] or "").strip()
        if not room_id.startswith("!"):
            self.log.warning("room_id is not configured, nothing will be mirrored")
            return None
        client = DiscourseClient(
            self.http, self.config["base_url"], self.config["user_agent"], self.log
        )
        settings = MirrorSettings(
            room_id=RoomID(room_id),
            include_categories=list(self.config["categories.include"] or []),
            exclude_categories=list(self.config["categories.exclude"] or []),
            backfill_topics=int(self.config["backfill.topics"]),
            backfill_posts_per_topic=int(self.config["backfill.posts_per_topic"]),
            max_post_chars=int(self.config["max_post_chars"]),
            max_pages_per_poll=int(self.config["max_pages_per_poll"]),
        )
        return Mirror(client, self.store, self.send_message, settings, self.log)

    async def rate_limited(self, call: Callable[[], Awaitable[T]], attempts: int = 8) -> T:
        """Run a Matrix request, backing off on the homeserver's rate limit.

        mautrix does not retry M_LIMIT_EXCEEDED and drops the response body, so
        retry_after_ms is not available; wait 1, 2, 4, 8, 16, 30, 30 s instead.
        """
        for attempt in range(attempts):
            try:
                return await call()
            except MLimitExceeded:
                if attempt == attempts - 1:
                    raise
                await asyncio.sleep(min(2**attempt, 30) * self.backoff_unit)
        raise AssertionError("unreachable")

    async def send_message(self, room_id: RoomID, content: TextMessageEventContent) -> EventID:
        return await self.rate_limited(lambda: self.client.send_message(room_id, content))

    async def poll_loop(self) -> None:
        while True:
            await self.poll_now()
            interval = max(int(self.config["poll_interval"]), 15)
            self.wakeup.clear()
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def poll_now(self) -> int:
        mirror = self.mirror
        if mirror is None:
            return 0
        async with self.poll_lock:
            try:
                sent = await mirror.poll()
                self.last_error = None
                return sent
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self.log.exception("Poll failed")
                return 0
            finally:
                self.last_poll = time.time()

    def is_admin(self, evt: MessageEvent) -> bool:
        admins = self.config["admins"] or []
        return not admins or evt.sender in admins

    @command.new(name=lambda self: self.config["command_prefix"], require_subcommand=True)
    async def forum(self, evt: MessageEvent) -> None:
        pass

    @forum.subcommand("status", help="Show the mirror status")
    async def status(self, evt: MessageEvent) -> None:
        if self.mirror is None:
            await evt.reply("Not configured: set `room_id` in the plugin config.")
            return
        cursor = await self.mirror.get_cursor()
        topics = await self.store.count_topics(self.mirror.settings.room_id)
        ignored = await self.store.ignored_topics(self.mirror.settings.room_id)
        last = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.last_poll))
            if self.last_poll
            else "never"
        )
        lines = [
            f"Forum: {self.mirror.client.base_url}",
            f"Room: {self.mirror.settings.room_id}",
            f"Last post id: {cursor if cursor is not None else 'not synced yet'}",
            f"Mirrored topics: {topics}",
            f"Ignored topics: {len(ignored)}",
            f"Last poll: {last}",
        ]
        if self.last_error:
            lines.append(f"Last error: {self.last_error}")
        await evt.reply("\n".join(f"* {line}" for line in lines))

    @forum.subcommand("poll", help="Check the forum for new posts now")
    async def poll(self, evt: MessageEvent) -> None:
        if not self.is_admin(evt):
            await evt.reply("You are not allowed to do that.")
            return
        sent = await self.poll_now()
        if self.last_error:
            await evt.reply(f"Poll failed: {self.last_error}")
        else:
            await evt.reply(f"Poll done, {sent} message(s) sent.")

    async def topic_from_command(self, evt: MessageEvent, value: str) -> int | None:
        """Topic named in the command, or the topic of the thread it was sent in."""
        if value.strip():
            return parse_topic_ref(value)
        for event_id in (evt.content.get_thread_parent(), evt.content.get_reply_to()):
            if event_id:
                thread = await self.mirror.thread_of_event(event_id)
                if thread:
                    return thread.topic_id
        return None

    @forum.subcommand("ignore", help="Stop mirroring a topic (send in its thread, or give id/URL)")
    @command.argument("topic", required=False, pass_raw=True)
    async def ignore(self, evt: MessageEvent, topic: str) -> None:
        if self.mirror is None:
            await evt.reply("Not configured: set `room_id` in the plugin config.")
            return
        if not self.is_admin(evt):
            await evt.reply("You are not allowed to do that.")
            return
        topic_id = await self.topic_from_command(evt, topic or "")
        if topic_id is None:
            await evt.reply(
                "Which topic? Send the command in the topic's thread, or give its id or URL."
            )
            return
        async with self.poll_lock:
            added = await self.mirror.ignore(topic_id)
        if added:
            await evt.reply(
                f"Ignoring topic {topic_id}. Undo with `!{self.config['command_prefix']} "
                f"unignore {topic_id}`."
            )
        else:
            await evt.reply(f"Topic {topic_id} is already ignored.")

    @forum.subcommand("unignore", help="Mirror an ignored topic again")
    @command.argument("topic", required=False, pass_raw=True)
    async def unignore(self, evt: MessageEvent, topic: str) -> None:
        if self.mirror is None:
            await evt.reply("Not configured: set `room_id` in the plugin config.")
            return
        if not self.is_admin(evt):
            await evt.reply("You are not allowed to do that.")
            return
        topic_id = await self.topic_from_command(evt, topic or "")
        if topic_id is None:
            await evt.reply("Which topic? Give its id or URL (see `ignored`).")
            return
        async with self.poll_lock:
            removed = await self.mirror.unignore(topic_id)
        if removed:
            await evt.reply(
                f"Topic {topic_id} is mirrored again from its next new post "
                "(posts written while it was ignored are not sent)."
            )
        else:
            await evt.reply(f"Topic {topic_id} is not ignored.")

    @forum.subcommand("ignored", help="List the ignored topics")
    async def ignored(self, evt: MessageEvent) -> None:
        if self.mirror is None:
            await evt.reply("Not configured: set `room_id` in the plugin config.")
            return
        topics = await self.store.ignored_topics(self.mirror.settings.room_id)
        if not topics:
            await evt.reply("No topics are ignored.")
            return
        # Forum titles go into HTML, not Markdown, so brackets or asterisks stay text.
        items_html, items_text = [], []
        for topic_id, title in topics:
            title = title or f"Topic {topic_id}"
            url = self.mirror.client.topic_url(topic_id)
            link = f'<a href="{escape(url)}">{escape(title)}</a>'
            items_html.append(f"<li>{link} – id {topic_id}</li>")
            items_text.append(f"* {title} – id {topic_id} {url}")
        await evt.reply(
            TextMessageEventContent(
                msgtype=MessageType.NOTICE,
                body="Ignored topics:\n" + "\n".join(items_text),
                format=Format.HTML,
                formatted_body=f"<p>Ignored topics:</p><ul>{''.join(items_html)}</ul>",
            )
        )

    @event.on(EventType.ROOM_REDACTION)
    async def on_redaction(self, evt: RedactionEvent) -> None:
        """Deleting a thread root in Matrix ignores that topic."""
        mirror = self.mirror
        if mirror is None or evt.room_id != mirror.settings.room_id or not evt.redacts:
            return
        async with self.poll_lock:
            thread = await mirror.root_redacted(evt.redacts)
        if thread:
            self.log.info(
                f"Thread root of topic {thread.topic_id} deleted by {evt.sender}, topic ignored"
            )
            await self.delete_thread(thread)

    async def thread_relations(self, room_id: RoomID, root: EventID) -> set[EventID]:
        """All events the homeserver knows in the thread of ``root``."""
        path = Path.v1.rooms[room_id].relations[root][RelationType.THREAD.value]
        events: set[EventID] = set()
        params = {"limit": "100"}
        for _ in range(100):
            data = await self.client.api.request(Method.GET, path, query_params=params)
            events.update(EventID(e["event_id"]) for e in data.get("chunk", []) if "event_id" in e)
            if not data.get("next_batch"):
                break
            params["from"] = data["next_batch"]
        return events

    async def redact(self, room_id: RoomID, event_id: EventID) -> None:
        await self.rate_limited(
            lambda: self.client.redact(room_id, event_id, reason="Forum thread deleted")
        )

    async def delete_thread(self, thread: TopicThread) -> None:
        """Delete the remaining messages of a thread whose root was deleted.

        The bot can always delete its own messages; other users' replies need a
        power level that allows deleting them.
        """
        events = set(await self.store.thread_events(thread.room_id, thread.topic_id))
        try:
            events |= await self.thread_relations(thread.room_id, thread.root_event_id)
        except Exception as e:
            self.log.warning(f"Could not list thread of topic {thread.topic_id}: {e}")
        events.discard(thread.root_event_id)
        deleted = forbidden = failed = 0
        for event_id in sorted(events):
            try:
                await self.redact(thread.room_id, event_id)
                deleted += 1
            except MForbidden:
                forbidden += 1
            except Exception as e:
                failed += 1
                self.log.warning(f"Could not delete {event_id}: {e}")
        await self.store.forget_thread_events(thread.room_id, thread.topic_id)
        self.log.info(
            f"Deleted {deleted} message(s) of topic {thread.topic_id}"
            + (f", {forbidden} not allowed (bot lacks power level)" if forbidden else "")
            + (f", {failed} failed" if failed else "")
        )
