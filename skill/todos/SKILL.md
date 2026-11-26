---
name: todos
description: Manage the Discord servers' todo lists (e.g. work and personal) through the Todo Bot API — list, add, complete, reopen, rename, move between projects or delete todos. Use whenever the user mentions their todos, the todo list, work or personal tasks, tasks for the team/server, or asks what's open or to add/finish a todo.
---

# Discord server todos

The todo lists live in the Todo Bot API. Each Discord server (for example work and personal) has its own list and its own API key. A key only ever reaches its own server's todos.

Call the API with `curl`, using these env vars:
- `TODO_API_URL`: base URL, e.g. `https://todos.example.com`
- `TODO_API_KEY_<NAME>`: one bearer token per server, e.g. `TODO_API_KEY_WORK` and `TODO_API_KEY_PERSONAL`. A plain `TODO_API_KEY` is the key of a single server.

See which servers are set up with `env | grep -o '^TODO_API_KEY[A-Z_]*'`. If there's no URL or no key, tell the user to add them to the `env` block of `~/.claude/settings.json`, then stop.

Send the same headers on every request, with the key of the server you mean:

```bash
curl -sS -H "Authorization: Bearer $TODO_API_KEY_WORK" -H "Content-Type: application/json" "$TODO_API_URL/..."
```

## Picking the server

- Use the server the user names ("my personal todos", "add to work"). Personal life, family, money and home go in personal; the job, coworkers and work projects go in work.
- If there's more than one server and it isn't clear which one the user means, ask. Never guess: a personal todo put in work is seen by coworkers.
- Moving a todo to another server means creating it there and deleting it from the old one. Todo numbers are shared across servers, so a todo gets a new number when it moves.
- When you show todos from more than one server, label each group with its server.
- `GET /server` returns `{"id", "name"}`, the Discord server a key belongs to. Use it when you're unsure which server a key is for.

## Endpoints

| Action | Request |
|---|---|
| List todos | `GET /todos?status=open` (`open` is the default; also `done` or `all`). Add `&project=<name>` to filter by project |
| Get one | `GET /todos/{id}` |
| Add | `POST /todos` with `{"title": "...", "project": "..."}` (`project` is optional) |
| Complete / reopen | `PATCH /todos/{id}` with `{"done": true}` / `{"done": false}` |
| Rename | `PATCH /todos/{id}` with `{"title": "..."}` |
| Set / clear project | `PATCH /todos/{id}` with `{"project": "..."}` / `{"project": null}` |
| Delete | `DELETE /todos/{id}` (returns 204) |
| Several changes at once | `POST /todos/batch` with a list of ops, see below |
| Projects in use | `GET /projects`, returns a list of names |
| Which server | `GET /server`, returns `{"id", "name"}` |

A todo looks like: `{"id", "title", "project", "done", "created_by", "created_at", "done_at"}`.

## Batch

For more than one change, send a single `POST /todos/batch` instead of one request per todo. This applies whether the changes are adding a list, completing several todos, or clearing old ones. The body is a list of up to 200 ops, applied in order:

```json
[
  {"op": "create", "title": "...", "project": "...", "done": false},
  {"op": "update", "id": 7, "done": true},
  {"op": "delete", "id": 3}
]
```

`create` takes the same fields as `POST /todos`, plus an optional `done` (for something that's already finished). `update` takes the same fields as `PATCH /todos/{id}`. The response is one entry per op, in order: the todo, or `null` for a delete. It's all or nothing: on `404` (the message names the op's position) or `422`, nothing was changed, so fix the input and resend the whole batch.

To delete everything, list the todos with `status=all` and send one delete op per id.

## Rules

- The project goes in the `project` field, never in the title. Projects are lowercased when saved; `null` or `""` means no project. Check `GET /projects` before using a new name, so a typo doesn't start a new project.
- When the user names a todo by its text rather than its number, list the todos and match on the title. If more than one todo matches, ask which one.
- Ask for confirmation before deleting, and once for a whole batch rather than per todo. Completing, reopening and moving between projects need no confirmation.
- Show lists compactly, one line per todo: `#12 ⬜ tibia-engine: Fix login` (use ✅ for done ones, and leave the project out when there is none).
- Errors: `401` means a bad key, `404` means the todo doesn't exist in that key's server (it may be in another one), and `422` means invalid input (the message says which).
