"""Minimal client for the public Discourse JSON API."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

# Discourse post types: 1 regular, 2 moderator action, 3 small action, 4 whisper.
REGULAR_POST = 1


class DiscourseError(Exception):
    pass


@dataclass
class Post:
    id: int
    topic_id: int
    post_number: int
    username: str
    name: str
    cooked: str
    created_at: str
    post_type: int = REGULAR_POST
    reply_to_post_number: int | None = None
    topic_title: str = ""
    topic_slug: str = ""
    category_id: int | None = None
    hidden: bool = False
    deleted: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any], topic: Topic | None = None) -> Post:
        return cls(
            id=int(data["id"]),
            topic_id=int(data["topic_id"]),
            post_number=int(data["post_number"]),
            username=data.get("username") or "",
            name=data.get("name") or data.get("display_username") or "",
            cooked=data.get("cooked") or "",
            created_at=data.get("created_at") or "",
            post_type=int(data.get("post_type") or REGULAR_POST),
            reply_to_post_number=data.get("reply_to_post_number"),
            topic_title=data.get("topic_title") or (topic.title if topic else ""),
            topic_slug=data.get("topic_slug") or (topic.slug if topic else ""),
            category_id=data.get("category_id", topic.category_id if topic else None),
            hidden=bool(data.get("hidden")),
            deleted=bool(data.get("deleted_at") or data.get("user_deleted")),
        )

    @property
    def visible(self) -> bool:
        return self.post_type == REGULAR_POST and not self.hidden and not self.deleted


@dataclass
class Topic:
    id: int
    title: str
    slug: str
    category_id: int | None
    posts_count: int = 0
    pinned: bool = False


@dataclass
class Category:
    id: int
    slug: str
    name: str
    parent_id: int | None


class DiscourseClient:
    def __init__(
        self,
        http: ClientSession,
        base_url: str,
        user_agent: str,
        log: logging.Logger,
        request_delay: float = 0.5,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.log = log
        self.request_delay = request_delay
        self._lock = asyncio.Lock()

    async def get_json(self, path: str, params: Any = None) -> dict[str, Any]:
        """GET ``path`` and decode JSON, honouring Discourse rate limits."""
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        async with self._lock:  # serialise requests so we stay polite
            for attempt in range(4):
                try:
                    async with self.http.get(
                        url, params=params, headers=headers, timeout=ClientTimeout(total=30)
                    ) as resp:
                        if resp.status == 429:
                            wait = _retry_after(resp.headers.get("Retry-After"), attempt)
                            self.log.warning(f"Rate limited by {url}, waiting {wait}s")
                            await asyncio.sleep(wait)
                            continue
                        if resp.status >= 400:
                            raise DiscourseError(f"GET {url} returned HTTP {resp.status}")
                        data = await resp.json(content_type=None)
                except (ClientError, asyncio.TimeoutError) as e:
                    raise DiscourseError(f"GET {url} failed: {e!r}") from e
                await asyncio.sleep(self.request_delay)
                if not isinstance(data, dict):
                    raise DiscourseError(f"GET {url} returned unexpected JSON")
                return data
        raise DiscourseError(f"GET {url} still rate limited after retries")

    async def latest_posts(self, before: int | None = None) -> list[Post]:
        """Newest public posts across the forum, newest first (max 50 per page)."""
        params = {"before": str(before)} if before else None
        data = await self.get_json("/posts.json", params)
        return [Post.from_json(p) for p in data.get("latest_posts", [])]

    async def posts_since(self, cursor: int, max_pages: int) -> tuple[list[Post], bool]:
        """All posts with an id above ``cursor``, oldest first.

        Returns ``(posts, complete)``; ``complete`` is False when ``max_pages``
        ran out before the cursor was reached, i.e. some posts were skipped.
        """
        collected: dict[int, Post] = {}
        before: int | None = None
        for _ in range(max_pages):
            page = await self.latest_posts(before)
            if not page:
                return sorted(collected.values(), key=lambda p: p.id), True
            for post in page:
                if post.id > cursor:
                    collected[post.id] = post
            oldest = min(p.id for p in page)
            if oldest <= cursor + 1:
                return sorted(collected.values(), key=lambda p: p.id), True
            before = oldest
        return sorted(collected.values(), key=lambda p: p.id), False

    async def latest_topics(self) -> list[Topic]:
        """Topics ordered by latest activity (first page)."""
        data = await self.get_json("/latest.json")
        return [
            Topic(
                id=int(t["id"]),
                title=t.get("title") or "",
                slug=t.get("slug") or "",
                category_id=t.get("category_id"),
                posts_count=int(t.get("posts_count") or 0),
                pinned=bool(t.get("pinned") or t.get("pinned_globally")),
            )
            for t in data.get("topic_list", {}).get("topics", [])
        ]

    async def topic_posts(self, topic_id: int, last: int) -> tuple[Topic, list[Post], int]:
        """The first post and the last ``last`` posts of a topic.

        Returns ``(topic, posts, total)`` where ``total`` is the number of
        post ids in the topic's stream.
        """
        data = await self.get_json(f"/t/{topic_id}.json")
        topic = Topic(
            id=int(data["id"]),
            title=data.get("title") or "",
            slug=data.get("slug") or "",
            category_id=data.get("category_id"),
            posts_count=int(data.get("posts_count") or 0),
        )
        stream = data.get("post_stream", {})
        ids: list[int] = list(stream.get("stream") or [])
        wanted = list(dict.fromkeys(ids[:1] + (ids[-last:] if last > 0 else [])))
        loaded = {int(p["id"]): p for p in stream.get("posts", [])}
        missing = [i for i in wanted if i not in loaded]
        if missing:
            extra = await self.get_json(
                f"/t/{topic_id}/posts.json", [("post_ids[]", str(i)) for i in missing]
            )
            for p in extra.get("post_stream", {}).get("posts", []):
                loaded[int(p["id"])] = p
        posts = [Post.from_json(loaded[i], topic) for i in wanted if i in loaded]
        return topic, sorted(posts, key=lambda p: p.post_number), len(ids)

    async def categories(self) -> dict[int, Category]:
        data = await self.get_json("/site.json")
        return {
            int(c["id"]): Category(
                id=int(c["id"]),
                slug=c.get("slug") or "",
                name=c.get("name") or "",
                parent_id=c.get("parent_category_id"),
            )
            for c in data.get("categories", [])
        }

    def topic_url(self, topic_id: int, slug: str = "") -> str:
        return f"{self.base_url}/t/{slug or '-'}/{topic_id}"

    def post_url(self, topic_id: int, post_number: int, slug: str = "") -> str:
        return f"{self.topic_url(topic_id, slug)}/{post_number}"


def _retry_after(value: str | None, attempt: int) -> float:
    try:
        return min(max(float(value), 1.0), 300.0) if value else 5.0 * (attempt + 1)
    except ValueError:
        return 5.0 * (attempt + 1)
