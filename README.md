# todos

Discord bot for managing a server's shared todo list, with a REST API (and a Claude Code skill that uses it).

The bot and the API run in one process and share one SQLite database.

## Discord setup

1. Create an application at <https://discord.com/developers/applications>. Under **Bot**, copy the token and enable **Server Members Intent**. The API needs that intent to look up member names.
2. Invite the bot. Under **OAuth2 → URL Generator**, pick the scopes `bot` and `applications.commands` and the permissions `Send Messages` and `Embed Links`, then open the generated URL.
3. Get your server ID: turn on Developer Mode, right-click the server, then **Copy Server ID**.
4. In the channel you want as the board, run `/todo board`.

## The board

The board is two embeds in one channel, and the bot edits both in place:

- **Completed**: the most recently completed todos, as `#12 Fix login ✅ - <time>`, with the newest at the bottom. The time is shown in each viewer's own timezone. An embed holds about 4,000 characters, so once it's full the oldest ones drop off. They're still in the database, and `/todo list status:done` shows them.
- **TODO**: every open todo, as `#12 Fix login`. If the list outgrows the embed, it ends with "…and N more".

Assignees aren't shown on the board. `/todo list` and the command replies show them.

The bot updates the board whenever a todo changes, whether the change came from a slash command or the API. Run `/todo board` in another channel to move the board there; the old messages are deleted. Only members with **Manage Server** can run it. If someone deletes one of the two messages, the bot posts both again on the next change, so Completed stays on top.

Command replies are ephemeral: only the person who ran the command sees them. To keep the board channel tidy, you can deny **Send Messages** there for everyone except the bot. Slash commands still work in that channel, because they only need **Use Application Commands**.

## Commands

| Command | |
|---|---|
| `/todo add <title> [assignee]` | Add a todo |
| `/todo list [status] [assignee]` | Show open, done or all todos, just to you |
| `/todo done <todo>` / `/todo reopen <todo>` | Mark a todo done, or open it again |
| `/todo assign <todo> [user]` | Assign a todo, or leave `user` empty to unassign |
| `/todo edit <todo> <title>` | Rename |
| `/todo delete <todo>` | Delete |
| `/todo board` | Post the board in this channel, or move it here |

The `<todo>` argument suggests matching todos as you type. Assignees are shown by name and are never pinged.

## Configuration

| Env var | |
|---|---|
| `DISCORD_TOKEN` | Bot token (required) |
| `GUILD_ID` | Server ID; slash commands are registered there only (required) |
| `API_KEY` | Bearer token for the API (required). Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `DATABASE_PATH` | SQLite file (default `todos.db`; `/data/todos.db` in Docker) |
| `PORT` | API port (default `8000`) |

## Run locally

```
cp .env.example .env   # fill it in
uv run --env-file .env python -m todo_bot
```

Interactive API docs are served at <http://localhost:8000/docs>.

## Deploy to Dokku

```
# on the server
dokku apps:create todos
dokku storage:ensure-directory todos
dokku storage:mount todos /var/lib/dokku/data/storage/todos:/data
dokku config:set todos DISCORD_TOKEN=... GUILD_ID=... API_KEY=...
dokku ports:set todos http:80:8000
dokku checks:disable todos   # see below
dokku domains:set todos todos.example.com
dokku letsencrypt:enable todos   # if the letsencrypt plugin is installed; the API key must go over HTTPS

# locally
git remote add dokku dokku@your-server:todos
git push dokku master
```

`checks:disable` turns off zero-downtime deploys. Without it, Dokku briefly runs the old and new containers side by side, so two bots would answer commands at the same time and both would write to the SQLite file. The cost is a few seconds of downtime per deploy.

The storage mount keeps the database across deploys. Without it, every deploy wipes the todos.

## API

Every endpoint except `/health` needs `Authorization: Bearer $API_KEY`. Discord user IDs are strings.

| Method | Path | |
|---|---|---|
| `GET` | `/todos?status=open\|done\|all&assignee_id=` | List todos |
| `POST` | `/todos` | Create: `{"title", "assignee_id"?}` |
| `GET` | `/todos/{id}` | Get one |
| `PATCH` | `/todos/{id}` | Update any of `{"title", "done", "assignee_id"}`; send `assignee_id: null` to unassign |
| `DELETE` | `/todos/{id}` | Delete |
| `GET` | `/members` | Server members (`id`, `name`, `username`), for resolving names |
| `GET` | `/health` | Liveness check, plus whether Discord is connected |

## Claude Code skill

`skill/todos/SKILL.md` teaches Claude Code to manage the list through the API. To install it for your user:

```
mkdir -p ~/.claude/skills && cp -r skill/todos ~/.claude/skills/
```

Then add the connection details to `~/.claude/settings.json`:

```json
{
  "env": {
    "TODO_API_URL": "https://todos.example.com",
    "TODO_API_KEY": "..."
  }
}
```

## Development

```
uv run pytest
uvx pre-commit run --all-files
```

## License

This project is licensed under the terms of the MIT license.
