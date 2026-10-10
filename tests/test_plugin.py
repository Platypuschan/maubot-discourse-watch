"""Start the real plugin class with maubot's test harness and a fake forum."""
import asyncio

import pytest
from maubot.matrix import MaubotMessageEvent
from maubot.testing.bot import make_message
from mautrix.errors import MForbidden, MLimitExceeded
from mautrix.types import (
    EventID,
    EventType,
    RedactionEvent,
    RedactionEventContent,
    RelationType,
    UserID,
)

from discourse_watch import bot as bot_module
from discourse_watch.bot import Config, DiscourseWatchBot
from discourse_watch.db import upgrade_table

from .test_mirror import FakeForum, post

ROOM = "!room:example.com"


@pytest.fixture
def maubot_plugin_class():
    return DiscourseWatchBot


@pytest.fixture
def maubot_plugin_config_class():
    return Config


@pytest.fixture
def maubot_upgrade_table():
    return upgrade_table


@pytest.fixture
def maubot_plugin_config_overrides():
    return {"room_id": ROOM, "backfill": {"topics": 0, "posts_per_topic": 0}}


@pytest.fixture
def forum(monkeypatch):
    forum = FakeForum()
    forum.add(post(1, 100, 1))
    monkeypatch.setattr(bot_module, "DiscourseClient", lambda *args: forum)
    return forum


@pytest.fixture
def maubot_test_bot(maubot_test_bot):
    # The stock mock returns no event ID, but threads need one.
    original = maubot_test_bot._mock_send_message_event

    async def send(room_id, event_type, content, txn_id=None, **kwargs):
        await original(room_id, event_type, content, txn_id, **kwargs)
        return EventID(f"$e{len(maubot_test_bot.responded)}")

    maubot_test_bot.client.send_message_event = send
    return maubot_test_bot


@pytest.mark.asyncio
async def test_plugin_polls_and_answers_status(forum, maubot_test_bot, maubot_plugin):
    for _ in range(50):
        if await maubot_plugin.mirror.get_cursor() is not None:
            break
        await asyncio.sleep(0.01)
    assert await maubot_plugin.mirror.get_cursor() == 1

    forum.add(post(2, 100, 2))
    await maubot_test_bot.send("!forum poll", room_id=ROOM)
    root, reply, answer = maubot_test_bot.responded
    assert root.room_id == ROOM and "Topic 100" in root.content.body
    assert reply.content.relates_to.rel_type == RelationType.THREAD
    assert "2 message(s) sent" in answer.content.body

    await maubot_test_bot.send("!forum status", room_id=ROOM)
    status = maubot_test_bot.responded[-1].content.body
    assert "Last post id: 2" in status and "Mirrored topics: 1" in status
    await maubot_plugin.stop()


async def wait_synced(plugin):
    for _ in range(50):
        if await plugin.mirror.get_cursor() is not None:
            return
        await asyncio.sleep(0.01)


def in_thread(bot, body, root):
    evt = make_message(body, room_id=ROOM)
    evt.content.set_thread_parent(root)
    return MaubotMessageEvent(evt, bot.client)


@pytest.mark.asyncio
async def test_ignore_commands(forum, maubot_test_bot, maubot_plugin):
    await wait_synced(maubot_plugin)
    forum.add(post(2, 100, 2), post(3, 200, 1))
    await maubot_test_bot.send("!forum poll", room_id=ROOM)
    root100 = (await maubot_plugin.store.get_topic(ROOM, 100)).root_event_id

    command = in_thread(maubot_test_bot, "!forum ignore", root100)
    await maubot_test_bot.dispatch(EventType.ROOM_MESSAGE, command)
    assert "Ignoring topic 100" in maubot_test_bot.responded[-1].content.body
    await maubot_test_bot.send("!forum ignore https://forum.example.org/t/x/200/3", room_id=ROOM)
    assert "Ignoring topic 200" in maubot_test_bot.responded[-1].content.body
    await maubot_test_bot.send("!forum ignore", room_id=ROOM)
    assert "Which topic?" in maubot_test_bot.responded[-1].content.body

    await maubot_test_bot.send("!forum ignored", room_id=ROOM)
    listing = maubot_test_bot.responded[-1].content.body
    assert "Topic 100" in listing and "id 100" in listing and "id 200" in listing
    assert "<li>" in maubot_test_bot.responded[-1].content.formatted_body

    forum.add(post(4, 100, 3), post(5, 200, 2))
    await maubot_test_bot.send("!forum poll", room_id=ROOM)
    assert "0 message(s) sent" in maubot_test_bot.responded[-1].content.body

    await maubot_test_bot.send("!forum unignore 100", room_id=ROOM)
    assert "mirrored again" in maubot_test_bot.responded[-1].content.body
    await maubot_test_bot.send("!forum status", room_id=ROOM)
    assert "Ignored topics: 1" in maubot_test_bot.responded[-1].content.body
    await maubot_plugin.stop()


@pytest.mark.asyncio
async def test_redacting_thread_root_deletes_thread(forum, maubot_test_bot, maubot_plugin):
    await wait_synced(maubot_plugin)
    forum.add(post(2, 100, 2), post(3, 100, 3), post(4, 200, 1))
    await maubot_test_bot.send("!forum poll", room_id=ROOM)
    thread = await maubot_plugin.store.get_topic(ROOM, 100)
    root = thread.root_event_id
    own = set(await maubot_plugin.store.thread_events(ROOM, 100)) - {root}
    assert len(own) == 2

    # The homeserver also knows a user's reply and a command answer in the thread.
    pages = [
        {"chunk": [{"event_id": "$user-reply"}, {"event_id": sorted(own)[0]}], "next_batch": "b"},
        {"chunk": [{"event_id": "$bot-answer"}]},
    ]
    requests = []

    async def request(method, path, query_params=None, **kwargs):
        requests.append((str(path), dict(query_params or {})))
        return pages[len(requests) - 1]

    redacted = []
    limited = []

    async def redact(room_id, event_id, reason=None, **kwargs):
        if event_id == "$user-reply":
            raise MForbidden(403, "not allowed")
        if event_id == "$bot-answer" and not limited:
            limited.append(event_id)
            raise MLimitExceeded(429, "slow down")
        redacted.append(event_id)
        return EventID(f"$redaction-{event_id}")

    maubot_test_bot.client.api.request = request
    maubot_test_bot.client.redact = redact

    redaction = RedactionEvent(
        type=EventType.ROOM_REDACTION, room_id=ROOM, event_id=EventID("$redact"),
        sender=UserID("@admin:example.com"), timestamp=0,
        content=RedactionEventContent(), redacts=root,
    )
    await maubot_test_bot.dispatch(EventType.ROOM_REDACTION, redaction)
    assert requests[0][0].endswith("/relations/" + root.replace("$", "%24") + "/m.thread")
    assert requests[1][1]["from"] == "b"
    assert set(redacted) == own | {"$bot-answer"}
    assert limited == ["$bot-answer"]  # retried after the rate limit
    assert root not in redacted
    assert await maubot_plugin.store.is_ignored(ROOM, 100)
    assert await maubot_plugin.store.get_topic(ROOM, 100) is None
    assert await maubot_plugin.store.thread_events(ROOM, 100) == []
    # The other topic is untouched.
    assert await maubot_plugin.store.get_topic(ROOM, 200) is not None
    assert len(await maubot_plugin.store.thread_events(ROOM, 200)) == 1
    await maubot_plugin.stop()
