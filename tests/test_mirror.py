import asyncio
import logging

from mautrix.types import EventID, RelationType, RoomID
from mautrix.util.async_db import Database

from discourse_watch.db import Store, upgrade_table
from discourse_watch.discourse import DiscourseClient, DiscourseError
from discourse_watch.mirror import Mirror, MirrorSettings

BASE = "https://forum.example.org"
ROOM = RoomID("!room:example.org")
log = logging.getLogger("test")


def post(pid, topic, number, cat=5, **extra):
    data = {
        "id": pid,
        "topic_id": topic,
        "post_number": number,
        "username": f"user{pid}",
        "name": "",
        "cooked": f"<p>post {pid}</p>",
        "created_at": "2026-10-10T00:00:00Z",
        "post_type": 1,
        "topic_title": f"Topic {topic}",
        "topic_slug": f"topic-{topic}",
        "category_id": cat,
    }
    data.update(extra)
    return data


class FakeForum(DiscourseClient):
    """Serves canned JSON instead of doing HTTP."""

    def __init__(self):
        super().__init__(None, BASE, "test", log, request_delay=0)
        self.posts = {}  # id -> json
        self.topics = {}  # id -> title
        self.requests = []

    def add(self, *posts):
        for p in posts:
            self.posts[p["id"]] = p
            self.topics.setdefault(p["topic_id"], p["topic_title"])

    async def get_json(self, path, params=None):
        self.requests.append((path, params))
        if path == "/posts.json":
            before = int(params["before"]) if params else None
            ids = sorted((i for i in self.posts if before is None or i < before), reverse=True)
            return {"latest_posts": [self.posts[i] for i in ids[:50]]}
        if path == "/latest.json":
            last = {}
            for p in self.posts.values():
                last[p["topic_id"]] = max(last.get(p["topic_id"], 0), p["id"])
            order = sorted(last, key=last.get, reverse=True)
            pinned = {"id": 1, "title": "Welcome", "slug": "welcome", "category_id": 5, "pinned": True}
            return {"topic_list": {"topics": [pinned] + [
                {"id": t, "title": self.topics[t], "slug": f"topic-{t}", "category_id": 5}
                for t in order
            ]}}
        if path == "/site.json":
            return {"categories": [
                {"id": 5, "slug": "support", "name": "Support"},
                {"id": 6, "slug": "faq", "name": "FAQ", "parent_category_id": 10},
                {"id": 10, "slug": "howto", "name": "Howto"},
            ]}
        if path.startswith("/t/") and path.endswith("/posts.json"):
            wanted = {int(v) for _, v in params}
            return {"post_stream": {"posts": [self.posts[i] for i in wanted]}}
        if path.startswith("/t/"):
            tid = int(path[3:].split(".")[0])
            stream = sorted(i for i, p in self.posts.items() if p["topic_id"] == tid)
            return {
                "id": tid, "title": self.topics[tid], "slug": f"topic-{tid}",
                "category_id": 5, "posts_count": len(stream),
                # Discourse only embeds the first 20 posts.
                "post_stream": {"stream": stream, "posts": [self.posts[i] for i in stream[:2]]},
            }
        raise AssertionError(path)


class FakeMatrix:
    def __init__(self):
        self.sent = []

    async def send(self, room_id, content):
        event_id = EventID(f"$e{len(self.sent)}")
        self.sent.append((event_id, content))
        return event_id


async def make(tmp_path, **settings):
    db = Database.create(f"sqlite:///{tmp_path}/test.db", upgrade_table=upgrade_table)
    await db.start()
    forum, matrix = FakeForum(), FakeMatrix()
    mirror = Mirror(forum, Store(db), matrix.send, MirrorSettings(room_id=ROOM, **settings), log)
    return db, forum, matrix, mirror


def thread_of(content):
    rel = content.relates_to
    return rel.event_id if rel and rel.rel_type == RelationType.THREAD else None


def test_new_topic_and_replies_go_into_thread(tmp_path):
    async def run():
        db, forum, matrix, mirror = await make(tmp_path, backfill_topics=0)
        forum.add(post(1, 100, 1))
        assert await mirror.poll() == 0  # initial sync just stores the cursor
        forum.add(post(2, 200, 1), post(3, 200, 2, reply_to_post_number=1), post(4, 100, 2))
        assert await mirror.poll() == 4
        (root200, c0), (r1, c1), (root100, c2), (r2, c3) = matrix.sent
        assert thread_of(c0) is None and "post 2" in c0.body and "Topic 200" in c0.body
        assert thread_of(c1) == root200 and "↩ #1" in c1.body
        # Topic 100 started before the bot: its opening post is loaded into the root.
        assert thread_of(c2) is None and "post 1" in c2.body and "started by" in c2.body
        assert thread_of(c3) == root100 and "post 4" in c3.body
        # Reply fallback points at the latest event in the thread.
        forum.add(post(5, 200, 3))
        assert await mirror.poll() == 1
        assert thread_of(matrix.sent[-1][1]) == root200
        assert matrix.sent[-1][1].relates_to.in_reply_to.event_id == r1
        assert await mirror.poll() == 0
        await db.stop()

    asyncio.run(run())


def test_backfill(tmp_path):
    async def run():
        db, forum, matrix, mirror = await make(
            tmp_path, backfill_topics=2, backfill_posts_per_topic=2
        )
        forum.add(post(1, 100, 1), post(2, 300, 1))
        for n in range(2, 8):
            forum.add(post(10 + n, 200, n))
        forum.add(post(9, 200, 1), post(30, 300, 2))
        await mirror.poll()
        bodies = [c.body for _, c in matrix.sent]
        # Topic 200 (older activity) first, topic 300 last; topic 100 not backfilled.
        assert bodies[0].startswith("📌 Topic 200") and "post 9" in bodies[0]
        assert "4 earlier posts not mirrored" in bodies[1]
        assert "post 16" in bodies[2] and "post 17" in bodies[3]
        assert bodies[4].startswith("📌 Topic 300") and "post 30" in bodies[5]
        assert len(bodies) == 6
        assert not any("Topic 100" in b for b in bodies)
        assert await mirror.get_cursor() == 30
        # A restart does not repeat anything.
        assert await mirror.poll() == 0
        await db.stop()

    asyncio.run(run())


def test_catch_up_pages_and_skips_hidden(tmp_path):
    async def run():
        db, forum, matrix, mirror = await make(tmp_path, backfill_topics=0)
        forum.add(post(1, 100, 1))
        await mirror.poll()
        forum.add(*(post(i, 100, i) for i in range(2, 130)))
        forum.add(post(130, 100, 130, hidden=True), post(131, 100, 131, post_type=3))
        assert await mirror.poll() == 129  # header root + 128 posts
        assert [p for p, _ in forum.requests].count("/posts.json") >= 4
        assert await mirror.get_cursor() == 131
        await db.stop()

    asyncio.run(run())


def test_category_filter(tmp_path):
    async def run():
        db, forum, matrix, mirror = await make(
            tmp_path, backfill_topics=0, include_categories=["howto"]
        )
        forum.add(post(1, 100, 1))
        await mirror.poll()
        forum.add(post(2, 200, 1, cat=5), post(3, 300, 1, cat=6))
        assert await mirror.poll() == 1
        assert "Topic 300" in matrix.sent[0][1].body
        assert "Howto › FAQ" in matrix.sent[0][1].formatted_body
        await db.stop()

    asyncio.run(run())


def test_huge_post_is_shrunk_below_event_limit(tmp_path):
    async def run():
        db, forum, matrix, mirror = await make(tmp_path, backfill_topics=0)
        forum.add(post(1, 100, 1))
        await mirror.poll()
        link = '<a href="https://example.org/' + "x" * 200 + '">l</a> '
        forum.add(post(2, 100, 2, cooked="<p>" + link * 2000 + "</p>"))
        await mirror.poll()
        content = matrix.sent[-1][1]
        assert len((content.body + content.formatted_body).encode()) < 60000
        assert "read the full post on the forum" in content.body
        await db.stop()

    asyncio.run(run())


def test_parse_topic_ref():
    from discourse_watch.mirror import parse_topic_ref

    assert parse_topic_ref("123") == 123
    assert parse_topic_ref("#123") == 123
    assert parse_topic_ref(f"{BASE}/t/some-topic/123") == 123
    assert parse_topic_ref(f"{BASE}/t/some-topic/123/4") == 123
    assert parse_topic_ref(f"{BASE}/t/123/4") == 123
    assert parse_topic_ref(f"{BASE}/u/someone") is None
    assert parse_topic_ref("hello") is None


def test_ignored_topic_is_skipped_until_unignored(tmp_path):
    async def run():
        db, forum, matrix, mirror = await make(tmp_path, backfill_topics=0)
        forum.add(post(1, 100, 1))
        await mirror.poll()
        forum.add(post(2, 100, 2), post(3, 200, 1))
        assert await mirror.poll() == 3  # header root + post 2, topic 200 root
        root100 = matrix.sent[0][0]
        assert await mirror.ignore(100) is True
        assert await mirror.ignore(100) is False
        forum.add(post(4, 100, 3), post(5, 200, 2))
        assert await mirror.poll() == 1
        assert "post 5" in matrix.sent[-1][1].body
        assert await mirror.get_cursor() == 5
        assert await mirror.store.ignored_topics(ROOM) == [(100, "Topic 100")]
        # Unignore resumes in the existing thread with new posts only.
        assert await mirror.unignore(100) is True
        assert await mirror.unignore(100) is False
        forum.add(post(6, 100, 4))
        assert await mirror.poll() == 1
        assert thread_of(matrix.sent[-1][1]) == root100 and "post 6" in matrix.sent[-1][1].body
        assert not any("post 4" in c.body for _, c in matrix.sent)
        await db.stop()

    asyncio.run(run())


def test_deleted_thread_root_ignores_topic(tmp_path):
    async def run():
        db, forum, matrix, mirror = await make(tmp_path, backfill_topics=0)
        forum.add(post(1, 100, 1))
        await mirror.poll()
        forum.add(post(2, 200, 1), post(3, 200, 2))
        await mirror.poll()
        root, reply = matrix.sent[0][0], matrix.sent[1][0]
        assert await mirror.root_redacted(reply) is None  # deleting a reply changes nothing
        thread = await mirror.root_redacted(root)
        assert thread.topic_id == 200
        assert await mirror.store.is_ignored(ROOM, 200)
        forum.add(post(4, 200, 3))
        assert await mirror.poll() == 0
        # After unignore the topic gets a new thread, not the deleted root.
        await mirror.unignore(200)
        forum.add(post(5, 200, 4))
        assert await mirror.poll() == 2
        new_root = matrix.sent[-2][0]
        assert new_root != root and thread_of(matrix.sent[-1][1]) == new_root
        await db.stop()

    asyncio.run(run())


def test_backfill_skips_ignored_topics(tmp_path):
    async def run():
        db, forum, matrix, mirror = await make(tmp_path, backfill_topics=5)
        forum.add(post(1, 100, 1), post(2, 200, 1))
        await mirror.store.ignore_topic(ROOM, 200, "")
        await mirror.poll()
        assert [c.body.splitlines()[0] for _, c in matrix.sent] == ["📌 Topic 100"]
        await db.stop()

    asyncio.run(run())


def test_interrupted_initial_sync_does_not_repeat(tmp_path):
    async def run():
        db, forum, matrix, mirror = await make(
            tmp_path, backfill_topics=2, backfill_posts_per_topic=2
        )
        forum.add(post(1, 100, 1), post(2, 300, 1))
        for n in range(2, 8):
            forum.add(post(10 + n, 200, n))
        forum.add(post(9, 200, 1), post(30, 300, 2))
        send = matrix.send

        async def fail_fourth(room_id, content):
            if len(matrix.sent) == 3:
                matrix.send = send
                mirror.send = send
                raise RuntimeError("Too Many Requests")
            return await send(room_id, content)

        mirror.send = fail_fourth
        try:
            await mirror.poll()
        except RuntimeError:
            pass
        assert len(matrix.sent) == 3 and await mirror.get_cursor() is None
        await mirror.poll()
        bodies = [c.body for _, c in matrix.sent]
        assert sum("earlier posts not mirrored" in b for b in bodies) == 1
        assert len(bodies) == 6 and "post 17" in bodies[3]
        assert await mirror.get_cursor() == 30
        await db.stop()

    asyncio.run(run())


def test_root_without_opening_post_when_forum_fails(tmp_path):
    async def run():
        db, forum, matrix, mirror = await make(tmp_path, backfill_topics=0)
        forum.add(post(1, 100, 1))
        await mirror.poll()
        forum.add(post(2, 100, 2))
        original = forum.get_json

        async def failing(path, params=None):
            if path.startswith("/t/"):
                raise DiscourseError("HTTP 500")
            return await original(path, params)

        forum.get_json = failing
        assert await mirror.poll() == 2
        (root, c0), (_, c1) = matrix.sent
        assert "Topic 100" in c0.body and "post 1" not in c0.body
        assert thread_of(c1) == root and "post 2" in c1.body
        await db.stop()

    asyncio.run(run())
