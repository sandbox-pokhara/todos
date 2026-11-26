# todos

Discord bot for managing a server's shared todo list, with a REST API (and a Claude Code skill that uses it).

The bot and the API run in one process and share one SQLite database.

One bot can serve several Discord servers, such as one for work and one for personal todos. Each server's todos, projects and board are kept apart: commands in a server, and its API key, only ever see that server's todos. Another server's todo is answered with "not found", the same as one that doesn't exist. Todo numbers are shared across servers, so a server's numbers can skip.

## Discord setup

1. Create an application at <https://discord.com/developers/applications>. Under **Bot**, copy the token.
2. Invite the bot. Under **OAuth2 → URL Generator**, pick the scopes `bot` and `applications.commands` and the permissions `Send Messages` and `Embed Links`, then open the generated URL.
3. Get your server ID: turn on Developer Mode, right-click the server, then **Copy Server ID**.
4. Add the server and a new API key for it to `GUILDS` (see [Configuration](#configuration)) and restart the bot. Slash commands are only registered in the servers listed there.
5. In the channel you want as the board, run `/todo board`.

To add another server, invite the same bot there and repeat steps 3 to 5.

## The board

The board is a set of embeds in one channel, which the bot edits in place:

- **Completed - Sep 29, 2026**: one message for each day that has completed todos, oldest at the top. Each lists that day's todos as `#12 Fix login ✅`, and the footer counts them (`3 todos done`). Days are in UTC. If a day outgrows its embed, the list ends with "…and N more", but the footer still counts every todo. When every todo from a day is reopened or deleted, that day's message is removed.
- **TODO**: every open todo, as `#12 Fix login`, at the bottom, with a footer that counts them (`5 todos open`). If the list outgrows the embed, it ends with "…and N more", but the footer still counts every todo.

Each todo can have a project, such as `tibia-engine`, or none. The board shows it in front of the title (`#12 tibia-engine: Fix login`). Projects are lowercased when saved, so `Tibia-Engine` and `tibia-engine` are the same project.

`/todo view` switches how open todos are shown, for everyone looking at the board:

- **List**: one line per todo, as above.
- **By project**: the same TODO embed, with a bold heading for each project, in name order, and the todos under it without the project in front. Todos without a project come last, under **No project**.

Anyone can run it. The choice is saved and survives restarts and `/todo board`.

The bot updates the board whenever a todo changes, whether the change came from a slash command or the API. Discord can only add messages at the bottom of a channel, so the first todo completed on a new day posts that day's message and posts TODO again below it. Run `/todo board` in another channel to move the board there; the old messages are deleted. Only members with **Manage Server** can run it. If someone deletes a day's message, the bot posts that day and everything below it again, so the order stays the same.

Command replies are ephemeral: only the person who ran the command sees them. To keep the board channel tidy, you can deny **Send Messages** there for everyone except the bot. Slash commands still work in that channel, because they only need **Use Application Commands**.

## Commands

| Command | |
|---|---|
| `/todo add <title> [project]` | Add a todo |
| `/todo list [status] [project]` | Show open, done or all todos, just to you |
| `/todo done <todo>` / `/todo reopen <todo>` | Mark a todo done, or open it again |
| `/todo project <todo> [project]` | Set a todo's project, or leave `project` empty to clear it |
| `/todo edit <todo> <title>` | Rename |
| `/todo delete <todo>` | Delete |
| `/todo board` | Post the board in this channel, or move it here |
| `/todo view <layout>` | Show open todos on the board as a list, or by project |

The `<todo>` argument suggests matching todos as you type, and `project` suggests the projects already in use.

## Configuration

| Env var | |
|---|---|
| `DISCORD_TOKEN` | Bot token (required) |
| `GUILDS` | The servers to serve, each with its own API key: `<server id>:<api key>,<server id>:<api key>`. Every key must be different and at least 16 characters. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `GUILD_ID`, `API_KEY` | One server and its key, the older way to configure the bot. They can be combined with `GUILDS`; that server comes first |
| `DATABASE_PATH` | SQLite file (default `todos.db`; `/data/todos.db` in Docker) |
| `PORT` | API port (default `8000`) |

One of `GUILDS` or `GUILD_ID` with `API_KEY` is required. Todos from before the bot served several servers belong to the first server listed (`GUILD_ID` if it's set), so keep that server first.

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
dokku config:set todos DISCORD_TOKEN=... GUILDS=<server id>:<api key>
dokku ports:set todos http:80:8000
dokku checks:disable todos   # see below
dokku checks:set todos wait-to-retire 0
dokku domains:set todos todos.example.com
dokku letsencrypt:enable todos   # if the letsencrypt plugin is installed; the API key must go over HTTPS

# locally
git remote add dokku dokku@your-server:todos
git push dokku master
```

`checks:disable` turns off zero-downtime deploys. Without it, Dokku briefly runs the old and new containers side by side, so two bots would answer commands at the same time and both would write to the SQLite file. Even with checks disabled, Dokku keeps the old container running for 60 seconds by default; `wait-to-retire 0` stops it right away. The cost is a few seconds of downtime per deploy.

The storage mount keeps the database across deploys. Without it, every deploy wipes the todos.

## API

Every endpoint except `/health` needs `Authorization: Bearer <api key>`, and works on the todos of the server that key belongs to. Discord IDs (`created_by`, the server id) are strings.

| Method | Path | |
|---|---|---|
| `GET` | `/todos?status=open\|done\|all&project=` | List todos |
| `POST` | `/todos` | Create: `{"title", "project"?}` |
| `GET` | `/todos/{id}` | Get one |
| `PATCH` | `/todos/{id}` | Update any of `{"title", "done", "project"}`; send `project: null` to clear it |
| `DELETE` | `/todos/{id}` | Delete |
| `POST` | `/todos/batch` | Apply up to 200 changes in one request, all or nothing (see below) |
| `GET` | `/projects` | Every project in use, by name |
| `GET` | `/server` | The server this key belongs to: `{"id", "name"}` (`name` is null until Discord connects) |
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
    "TODO_API_KEY_WORK": "...",
    "TODO_API_KEY_PERSONAL": "..."
  }
}
```

There's one `TODO_API_KEY_<NAME>` per server; the name is what you call the server when you talk to Claude. A plain `TODO_API_KEY` also works, for a single server.

## Development

```
uv run pytest
uvx pre-commit run --all-files
```

## License

This project is licensed under the terms of the MIT license.

### Batch

`POST /todos/batch` takes a list of operations and applies them in order, in one transaction:

```json
[
  {"op": "create", "title": "Ship it", "project": "todos", "done": true},
  {"op": "update", "id": 7, "done": true},
  {"op": "delete", "id": 3}
]
```

A `create` accepts the same fields as `POST /todos`, plus an optional `done`. An `update` accepts the same fields as `PATCH /todos/{id}`, plus the `id`. The response has one entry per operation, in the same order: the todo after the change, or `null` for a delete. If any operation fails, nothing is applied. An unknown id returns `404` and names the operation's position. Invalid input returns `422`. The board refreshes once for the whole batch.
