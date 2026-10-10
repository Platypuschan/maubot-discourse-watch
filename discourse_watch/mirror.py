"""Mirror Discourse topics into Matrix threads."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from html import escape
from typing import Awaitable, Callable
from urllib.parse import urlparse

from mautrix.types import EventID, Format, MessageType, RoomID, TextMessageEventContent

from .db import Store, TopicThread
from .discourse import Category, DiscourseClient, Post, Topic
from .render import render_post

Sender = Callable[[RoomID, TextMessageEventContent], Awaitable[EventID]]

CATEGORY_CACHE_SECONDS = 3600
MAX_BODY_BYTES = 40000


@dataclass
class MirrorSettings:
    room_id: RoomID
    include_categories: list[str] = field(default_factory=list)
    exclude_categories: list[str] = field(default_factory=list)
    backfill_topics: int = 10
    backfill_posts_per_topic: int = 5
    max_post_chars: int = 20000
    max_pages_per_poll: int = 10


def parse_topic_ref(value: str) -> int | None:
    """Topic id from ``123`` or a topic/post URL such as ``…/t/some-slug/123/4``."""
    value = value.strip()
    if re.fullmatch(r"#?\d+", value):
        return int(value.lstrip("#"))
    parts = [p for p in urlparse(value).path.split("/") if p]
    if "t" not in parts:
        return None
    rest = parts[parts.index("t") + 1 :]
    # /t/<id>[/<post>] or /t/<slug>/<id>[/<post>]
    for candidate in rest[:2]:
        if candidate.isdigit():
            return int(candidate)
    return None


class Mirror:
    def __init__(
        self,
        client: DiscourseClient,
        store: Store,
        send: Sender,
        settings: MirrorSettings,
        log: logging.Logger,
    ) -> None:
        self.client = client
        self.store = store
        self.send = send
        self.settings = settings
        self.log = log
        self._categories: dict[int, Category] = {}
        self._categories_loaded = 0.0

    @property
    def cursor_key(self) -> str:
        return f"cursor:{self.client.base_url}:{self.settings.room_id}"

    async def get_cursor(self) -> int | None:
        value = await self.store.get_state(self.cursor_key)
        return int(value) if value is not None else None

    async def poll(self) -> int:
        """Run one poll cycle and return the number of messages sent."""
        cursor = await self.get_cursor()
        if cursor is None:
            return await self.initial_sync()
        posts, complete = await self.client.posts_since(cursor, self.settings.max_pages_per_poll)
        if not complete:
            self.log.warning(
                f"More than {self.settings.max_pages_per_poll} pages of new posts since post "
                f"{cursor}; older ones in between were skipped"
            )
        sent = 0
        for post in posts:
            sent += await self.handle_post(post)
            await self.store.set_state(self.cursor_key, str(post.id))
        return sent

    async def initial_sync(self) -> int:
        """First run for this room: remember the newest post and backfill topics."""
        newest = await self.client.latest_posts()
        cursor = max((p.id for p in newest), default=0)
        sent = 0
        if self.settings.backfill_topics > 0:
            # Pinned topics always head the list; they are not "recent activity".
            topics = [
                t
                for t in await self.client.latest_topics()
                if not t.pinned and await self.wanted(t.category_id)
            ]
            # Oldest activity first, so the most active topic ends up at the bottom.
            for summary in reversed(topics[: self.settings.backfill_topics]):
                sent += await self.backfill_topic(summary.id, cursor)
        await self.store.set_state(self.cursor_key, str(cursor))
        self.log.info(f"Initial sync done at post {cursor}, sent {sent} messages")
        return sent

    async def backfill_topic(self, topic_id: int, cursor: int) -> int:
        if await self.store.is_ignored(self.settings.room_id, topic_id):
            return 0
        topic, posts, total = await self.client.topic_posts(
            topic_id, self.settings.backfill_posts_per_topic
        )
        # Newer posts are delivered by the regular poll.
        posts = [p for p in posts if p.id <= cursor and p.visible]
        if not posts:
            return 0
        sent = 0
        thread = await self.store.get_topic(self.settings.room_id, topic.id)
        first = posts[0] if posts[0].post_number == 1 else None
        new_thread = thread is None
        if new_thread:
            thread = await self.create_thread(topic, first)
            sent += 1
        replies = [p for p in posts if p is not first]
        skipped = total - len(replies) - (1 if first else 0)
        # Only for a new thread: a retried initial sync must not repeat the notice.
        if new_thread and replies and skipped > 0:
            await self.send_notice(
                thread,
                f"… {skipped} earlier post{'s' if skipped != 1 else ''} not mirrored",
                self.client.topic_url(topic.id, topic.slug),
            )
            sent += 1
        for post in replies:
            sent += await self.send_post(thread, post)
        return sent

    async def handle_post(self, post: Post) -> int:
        if not post.visible or not await self.wanted(post.category_id):
            return 0
        room_id = self.settings.room_id
        if await self.store.is_sent(room_id, post.id):
            return 0
        if await self.store.is_ignored(room_id, post.topic_id):
            return 0
        thread = await self.store.get_topic(room_id, post.topic_id)
        if thread is None:
            topic = Topic(post.topic_id, post.topic_title, post.topic_slug, post.category_id)
            first = post if post.post_number == 1 else None
            thread = await self.create_thread(topic, first)
            return 1 if first else 1 + await self.send_post(thread, post)
        return await self.send_post(thread, post)

    async def ignore(self, topic_id: int) -> bool:
        """Stop mirroring ``topic_id``; its thread stays and is used again after unignore."""
        thread = await self.store.get_topic(self.settings.room_id, topic_id)
        return await self.store.ignore_topic(
            self.settings.room_id, topic_id, thread.title if thread else ""
        )

    async def unignore(self, topic_id: int) -> bool:
        return await self.store.unignore_topic(self.settings.room_id, topic_id)

    async def thread_of_event(self, event_id: EventID) -> TopicThread | None:
        return await self.store.get_topic_by_root(self.settings.room_id, event_id)

    async def root_redacted(self, event_id: EventID) -> TopicThread | None:
        """A thread root was deleted in Matrix: ignore its topic and forget the thread.

        Forgetting it means a later unignore starts a new thread instead of
        replying to the deleted root. The caller deletes the rest of the thread.
        """
        thread = await self.thread_of_event(event_id)
        if thread is None:
            return None
        await self.store.ignore_topic(thread.room_id, thread.topic_id, thread.title)
        await self.store.delete_topic(thread.room_id, thread.topic_id)
        return thread

    async def create_thread(self, topic: Topic, first: Post | None) -> TopicThread:
        """Send the thread root for ``topic``, including its first post if given."""
        url = self.client.topic_url(topic.id, topic.slug)
        category = await self.category_label(topic.category_id)
        meta_html = [f"<em>{escape(category)}</em>"] if category else []
        meta_text = [category] if category else []
        if first:
            meta_html.append(f"started by {self.author_html(first)}")
            meta_text.append(f"started by {self.author_text(first)}")
        html = f'<h4>📌 <a href="{escape(url)}">{escape(topic.title)}</a></h4>'
        text = f"📌 {topic.title}\n{url}"
        if meta_html:
            html += f"<p>{' · '.join(meta_html)}</p>"
            text += "\n" + " · ".join(meta_text)
        if first:
            body_html, body_text = self.post_body(first)
            html += body_html
            text += "\n\n" + body_text
        content = TextMessageEventContent(
            msgtype=MessageType.TEXT, body=text, format=Format.HTML, formatted_body=html
        )
        event_id = await self.send(self.settings.room_id, content)
        thread = TopicThread(self.settings.room_id, topic.id, event_id, event_id, topic.title)
        await self.store.put_topic(thread)
        await self.store.add_thread_event(thread.room_id, topic.id, event_id)
        if first:
            await self.store.mark_sent(self.settings.room_id, first.id, event_id)
        return thread

    async def send_post(self, thread: TopicThread, post: Post) -> int:
        if await self.store.is_sent(thread.room_id, post.id):
            return 0
        url = self.client.post_url(post.topic_id, post.post_number, post.topic_slug)
        header_html = f'{self.author_html(post)} · <a href="{escape(url)}">#{post.post_number}</a>'
        header_text = f"{self.author_text(post)} · #{post.post_number} {url}"
        if post.reply_to_post_number:
            header_html += f" ↩ #{post.reply_to_post_number}"
            header_text += f" ↩ #{post.reply_to_post_number}"
        body_html, body_text = self.post_body(post)
        content = TextMessageEventContent(
            msgtype=MessageType.TEXT,
            body=f"{header_text}\n\n{body_text}",
            format=Format.HTML,
            formatted_body=f"<p>{header_html}</p>{body_html}",
        )
        await self.send_in_thread(thread, content)
        await self.store.mark_sent(thread.room_id, post.id, thread.last_event_id)
        return 1

    async def send_notice(self, thread: TopicThread, text: str, url: str) -> None:
        content = TextMessageEventContent(
            msgtype=MessageType.NOTICE,
            body=f"{text}: {url}",
            format=Format.HTML,
            formatted_body=f'<em>{escape(text)}</em> (<a href="{escape(url)}">read on the forum</a>)',
        )
        await self.send_in_thread(thread, content)

    async def send_in_thread(self, thread: TopicThread, content: TextMessageEventContent) -> None:
        content.set_thread_parent(thread.root_event_id, last_event_in_thread=thread.last_event_id)
        thread.last_event_id = await self.send(thread.room_id, content)
        await self.store.add_thread_event(thread.room_id, thread.topic_id, thread.last_event_id)
        await self.store.put_topic(thread)

    def post_body(self, post: Post) -> tuple[str, str]:
        budget = self.settings.max_post_chars
        rendered = render_post(post.cooked, self.client.base_url, budget)
        # Matrix rejects events above 64 KiB; markup and URLs can make the HTML
        # much longer than the visible text, so shrink until both bodies fit.
        while len((rendered.html + rendered.text).encode()) > MAX_BODY_BYTES and budget > 500:
            budget //= 2
            rendered = render_post(post.cooked, self.client.base_url, budget)
        html, text = rendered.html, rendered.text
        if rendered.truncated:
            url = self.client.post_url(post.topic_id, post.post_number, post.topic_slug)
            html += f'<p>… <a href="{escape(url)}">read the full post on the forum</a></p>'
            text += f"\n… read the full post on the forum: {url}"
        return html, text

    @staticmethod
    def author_text(post: Post) -> str:
        if post.name and post.name.lower() != post.username.lower():
            return f"{post.name} (@{post.username})"
        return f"@{post.username}"

    def author_html(self, post: Post) -> str:
        url = f"{self.client.base_url}/u/{post.username}"
        name = post.name if post.name and post.name.lower() != post.username.lower() else ""
        link = f'<a href="{escape(url)}">@{escape(post.username)}</a>'
        return f"<strong>{escape(name)}</strong> ({link})" if name else f"<strong>{link}</strong>"

    async def load_categories(self, force: bool = False) -> dict[int, Category]:
        stale = time.monotonic() - self._categories_loaded > CATEGORY_CACHE_SECONDS
        if force or stale or not self._categories:
            try:
                self._categories = await self.client.categories()
                self._categories_loaded = time.monotonic()
            except Exception as e:
                self.log.warning(f"Could not load categories: {e}")
        return self._categories

    async def category_chain(self, category_id: int | None) -> list[Category]:
        if category_id is None:
            return []
        cats = await self.load_categories()
        if category_id not in cats:
            cats = await self.load_categories(force=True)
        chain: list[Category] = []
        current = cats.get(category_id)
        while current and len(chain) < 5:
            chain.insert(0, current)
            current = cats.get(current.parent_id) if current.parent_id else None
        return chain

    async def category_label(self, category_id: int | None) -> str:
        return " › ".join(c.name for c in await self.category_chain(category_id))

    async def wanted(self, category_id: int | None) -> bool:
        include = {s.lower() for s in self.settings.include_categories}
        exclude = {s.lower() for s in self.settings.exclude_categories}
        if not include and not exclude:
            return True
        keys = {
            key
            for c in await self.category_chain(category_id)
            for key in (c.slug.lower(), str(c.id))
        }
        if keys & exclude:
            return False
        return not include or bool(keys & include)
