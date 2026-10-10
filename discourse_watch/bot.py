from __future__ import annotations

import asyncio
import time
from typing import Type

from maubot import MessageEvent, Plugin
from maubot.handlers import command
from mautrix.types import EventID, RoomID, TextMessageEventContent
from mautrix.util.async_db import UpgradeTable
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .db import Store, upgrade_table
from .discourse import DiscourseClient
from .mirror import Mirror, MirrorSettings


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

    async def send_message(self, room_id: RoomID, content: TextMessageEventContent) -> EventID:
        return await self.client.send_message(room_id, content)

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
