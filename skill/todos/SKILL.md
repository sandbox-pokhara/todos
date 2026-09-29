---
name: todos
description: Manage the Discord server's shared todo list through the Todo Bot API — list, add, complete, reopen, rename, assign, unassign or delete todos. Use whenever the user mentions the server todos, the todo list, tasks for the team/server, or asks what's open, what someone is working on, or to add/finish/assign a todo.
---

# Discord server todos

The todo list lives in the Todo Bot API. Call it with `curl`, using two env vars:
- `TODO_API_URL`: base URL, e.g. `https://todos.example.com`
- `TODO_API_KEY`: bearer token

If either is unset, tell the user to add both to the `env` block of `~/.claude/settings.json`, then stop.

Send the same headers on every request:

```bash
curl -sS -H "Authorization: Bearer $TODO_API_KEY" -H "Content-Type: application/json" "$TODO_API_URL/..."
```

## Endpoints

| Action | Request |
|---|---|
| List todos | `GET /todos?status=open` (`open` is the default; also `done` or `all`). Add `&assignee_id=<id>` to filter by person |
| Get one | `GET /todos/{id}` |
| Add | `POST /todos` with `{"title": "...", "assignee_id": "<id>"}` (`assignee_id` is optional) |
| Complete / reopen | `PATCH /todos/{id}` with `{"done": true}` / `{"done": false}` |
| Rename | `PATCH /todos/{id}` with `{"title": "..."}` |
| Assign / unassign | `PATCH /todos/{id}` with `{"assignee_id": "<id>"}` / `{"assignee_id": null}` |
| Delete | `DELETE /todos/{id}` (returns 204) |
| Server members | `GET /members`, returns `[{"id", "name", "username"}]` |

A todo looks like: `{"id", "title", "done", "assignee_id", "assignee_name", "created_by", "created_at", "done_at"}`.

## Rules

- Discord user IDs are **strings** in both requests and responses. Never send them as numbers, because they lose precision.
- To assign by name ("give it to sam"), call `GET /members` first and match on `name` or `username`, ignoring case. If several people match, or none do, ask the user instead of guessing.
- When the user names a todo by its text rather than its number, list the todos and match on the title. If more than one todo matches, ask which one.
- Ask for confirmation before deleting. Completing, reopening and assigning need no confirmation.
- Show lists compactly, one line per todo: `#12 ⬜ Fix login — Sam` (use ✅ for done ones).
- Errors: `401` means a bad key, `404` means the todo doesn't exist, `422` means invalid input or an assignee who isn't in the server (the message says which), and `503` from `/members` means the bot is still connecting, so try again shortly.
