# maubot-discourse-watch

A [maubot](https://github.com/maubot/maubot) plugin that mirrors a Discourse
forum into one Matrix room, so you can follow it from your Matrix client.
It is set up for the [NethServer community forum](https://community.nethserver.org/),
but works with any public Discourse instance.

* Every forum **topic** becomes a **thread** in the room. The thread root shows
  the title, category, author and the opening post.
* Every new **post** in a topic is sent **into that topic's thread**, with
  author, a link to the post and the post's content (formatting, code blocks,
  quotes and links kept; images become links).
* On the first start the most recently active topics are backfilled (opening
  post plus the latest replies). After that, only new posts are sent.

The plugin uses the forum's public JSON API (`/posts.json`, `/latest.json`,
`/t/<id>.json`, `/site.json`) without an account. It polls once a minute by
default; after downtime it catches up on missed posts (up to
`max_pages_per_poll` × 50 posts).

## Installation

1. Download the `.mbp` from the latest CI run (artifact `maubot-plugin`) or build it
   yourself with `mbc build`.
2. Upload it in the maubot management UI (*Plugins → +*).
3. Create a Matrix room for the forum and invite the bot's client to it.
4. Create an instance of `io.github.platypuschan.discoursewatch` for that
   client and set `room_id` in its configuration (the room's internal ID,
   `!…:server`, found under *Room settings → Advanced* in Element).

Homeservers limit how fast a user may send messages (Synapse by default: a
burst of 10, then about one every 5 seconds). The backfill on the first start
can hit that limit; the bot then waits and retries, so the first start may take
a few minutes. On Synapse, a server admin can lift the limit for the bot with
the admin API `POST /_synapse/admin/v1/users/<bot user id>/override_ratelimit`
and `{"messages_per_second": 0, "burst_count": 0}`.

The plugin needs maubot ≥ 0.4 and stores its state in the instance database
(SQLite or PostgreSQL).

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `base_url` | `https://community.nethserver.org` | Discourse forum to watch |
| `room_id` | *(empty)* | Matrix room that receives the forum; nothing happens until set |
| `poll_interval` | `60` | Seconds between checks (minimum 15) |
| `backfill.topics` | `10` | Recently active topics to mirror on first start (`0` = only new activity) |
| `backfill.posts_per_topic` | `5` | Latest replies per backfilled topic |
| `categories.include` / `exclude` | `[]` | Category slugs or IDs; sub-categories match their parent; empty include = all |
| `max_post_chars` | `20000` | Longer posts are cut and link to the forum |
| `max_pages_per_poll` | `10` | Pages of 50 posts fetched when catching up |
| `user_agent` | `maubot-discourse-watch (…)` | User-Agent sent to the forum |
| `command_prefix` | `forum` | Prefix for the bot commands |
| `admins` | `[]` | Matrix IDs allowed to run `!forum poll` (empty = everyone) |
| `url_previews` | `false` | Show URL previews for the bot's messages. When `false`, messages carry the [MSC4095](https://github.com/matrix-org/matrix-spec-proposals/pull/4095) opt-out (`"com.beeper.linkpreviews": []`): Element 1.12.30 and later shows no previews for them, for everyone in the room. Other clients may ignore it. Only affects messages sent after the change. |

Changing `room_id` starts a fresh mirror (with backfill) in the new room.

Quote Matrix IDs in the config: YAML does not allow a bare `@` or `!` at the
start of a value, so write `admins: ["@alice:example.org"]` and
`room_id: "!abc:example.org"`. Otherwise maubot cannot load the config and the
instance does not start.

## Commands

* `!forum status` – forum, room, last processed post, number of mirrored topics,
  last poll and last error.
* `!forum poll` – check for new posts now.
* `!forum ignore [topic]` – stop mirroring a topic. Send it inside the topic's
  thread, or give the topic id or forum URL.
* `!forum unignore <topic>` – mirror an ignored topic again, starting with its
  next new post (posts written in the meantime are not sent).
* `!forum ignored` – list the ignored topics.
* `!forum cleanup` – remove what is left of deleted threads (see below).

`ignore`, `unignore` and `cleanup` follow the `admins` setting, like `poll`.

### Deleting a thread

Deleting (redacting) a thread's first message in Matrix removes the whole
thread and ignores its topic: the bot deletes the rest of the thread and sends
no new posts of that topic any more.

* Deleting the first message needs a power level that may delete other users'
  messages, usually moderator or admin of the room.
* The bot always deletes its own messages in the thread (forum posts, notices,
  answers to commands). Replies written by other users are only deleted if the
  bot itself has that power level in the room; otherwise they stay and the bot
  logs how many it could not delete.
* If you `unignore` such a topic later, its next post starts a new thread.

A thread stays in the room's thread list (as "Message deleted") as long as any
message of it is left. When the bot could not delete everything, e.g. because
it was not a moderator yet, it cleans up later: on every start, whenever the
room's power levels change (so promoting the bot to moderator is enough), and
on `!forum cleanup`. The cleanup also catches thread roots deleted while the
bot was offline. It only touches threads whose root the bot sent.

## Limitations

* Edits and deletions on the forum are not mirrored; each post is sent once.
* Only public content is visible to the anonymous API (no private messages or
  restricted categories).
* Posts that only become visible later (e.g. after moderator approval) have an
  older post ID than the stored cursor and are skipped.
* Discourse emoji are shown as their short codes (e.g. `:smile:`).

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install maubot pytest pytest-asyncio aiosqlite
python -m pytest tests
mbc build
```

## License

[MIT](LICENSE): do whatever you like with the code. Have fun with it, and
please don't use it for anything evil. (That last part is a wish, not a
license condition.)
