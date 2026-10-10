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

Changing `room_id` starts a fresh mirror (with backfill) in the new room.

## Commands

* `!forum status` – forum, room, last processed post, number of mirrored topics,
  last poll and last error.
* `!forum poll` – check for new posts now.
* `!forum ignore [topic]` – stop mirroring a topic. Send it inside the topic's
  thread, or give the topic id or forum URL.
* `!forum unignore <topic>` – mirror an ignored topic again, starting with its
  next new post (posts written in the meantime are not sent).
* `!forum ignored` – list the ignored topics.

`ignore` and `unignore` follow the `admins` setting, like `poll`.

### Ignoring a topic by deleting its thread

Deleting (redacting) a thread's first message in Matrix ignores that topic: no
new posts of it are sent any more. This needs a power level that may delete
other users' messages, usually moderator or admin of the room. The replies
already in the thread stay; delete them as well if you want the thread gone
completely. If you `unignore` such a topic later, its next post starts a new
thread.

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
