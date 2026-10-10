"""Start the real plugin class with maubot's test harness and a fake forum."""
import asyncio

import pytest
from mautrix.types import EventID, RelationType

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
