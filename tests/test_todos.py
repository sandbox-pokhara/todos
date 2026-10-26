from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import httpx
import pytest

from todo_bot.api import create_app
from todo_bot.store import TodoStore

API_KEY = "test-key"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def store() -> AsyncIterator[TodoStore]:
    s = await TodoStore.open(":memory:")
    yield s
    await s.close()


@pytest.fixture
async def client(store: TodoStore) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(store, lambda: True, API_KEY)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {API_KEY}"},
    ) as c:
        yield c


@pytest.mark.anyio
async def test_store_lifecycle(store: TodoStore) -> None:
    todo = await store.add("Write docs", created_by=1)
    assert not todo.done and todo.created_by == 1

    done = await store.update(todo.id, done=True)
    assert done and done.done and done.done_at

    assert await store.list_todos("open") == []
    assert [t.id for t in await store.list_todos("done")] == [todo.id]

    reopened = await store.update(todo.id, done=False)
    assert reopened and not reopened.done and reopened.done_at is None

    assert [t.id for t in await store.search("docs")] == [todo.id]
    assert [t.id for t in await store.search(f"#{todo.id}")] == [todo.id]

    assert await store.delete(todo.id)
    assert not await store.delete(todo.id)


@pytest.mark.anyio
async def test_api_requires_key(client: httpx.AsyncClient) -> None:
    r = await client.get("/todos", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert (await client.get("/health")).status_code == 200


@pytest.mark.anyio
async def test_api_crud(client: httpx.AsyncClient) -> None:
    r = await client.post("/todos", json={"title": "Ship it"})
    assert r.status_code == 201
    todo = r.json()
    assert "assignee_id" not in todo

    r = await client.patch(f"/todos/{todo['id']}", json={"done": True})
    assert r.json()["done"] is True
    assert r.json()["title"] == "Ship it"  # untouched

    assert (await client.get("/todos")).json() == []
    assert len((await client.get("/todos", params={"status": "all"})).json()) == 1

    assert (await client.delete(f"/todos/{todo['id']}")).status_code == 204
    assert (await client.get(f"/todos/{todo['id']}")).status_code == 404


@pytest.mark.anyio
async def test_api_validation(client: httpx.AsyncClient) -> None:
    assert (await client.post("/todos", json={"title": ""})).status_code == 422
    r = await client.post("/todos", json={"title": "x" * 201})
    assert r.status_code == 422
    assert (await client.get("/members")).status_code == 404


@pytest.mark.anyio
async def test_store_notifies_changes(store: TodoStore) -> None:
    changes: list[None] = []
    store.add_listener(lambda: changes.append(None))

    todo = await store.add("Plan")
    await store.update(todo.id, done=True)
    await store.update(todo.id)  # nothing to change
    await store.delete(todo.id)
    await store.delete(todo.id)  # already gone
    assert len(changes) == 3


@pytest.mark.anyio
async def test_api_changes_reach_the_board(
    store: TodoStore, client: httpx.AsyncClient
) -> None:
    changes: list[None] = []
    store.add_listener(lambda: changes.append(None))
    await client.post("/todos", json={"title": "From Claude"})
    assert changes


@pytest.mark.anyio
async def test_api_batch(store: TodoStore, client: httpx.AsyncClient) -> None:
    changes: list[None] = []
    store.add_listener(lambda: changes.append(None))
    old = (await client.post("/todos", json={"title": "Old"})).json()
    changes.clear()

    r = await client.post(
        "/todos/batch",
        json=[
            {"op": "delete", "id": old["id"]},
            {"op": "create", "title": "Shipped", "done": True},
            {"op": "create", "title": "Next", "project": "api"},
            {"op": "update", "id": old["id"] + 2, "title": "Next up"},
        ],
    )
    assert r.status_code == 200
    deleted, shipped, created, renamed = r.json()
    assert deleted is None
    assert shipped["done"] and shipped["done_at"]
    assert created["project"] == "api"
    assert renamed["id"] == created["id"] and renamed["title"] == "Next up"
    assert len(changes) == 1  # one board refresh for the whole batch

    titles = [t["title"] for t in (await client.get("/todos?status=all")).json()]
    assert titles == ["Next up", "Shipped"]


@pytest.mark.anyio
async def test_api_batch_is_all_or_nothing(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/todos/batch",
        json=[{"op": "create", "title": "Kept?"}, {"op": "delete", "id": 999}],
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "Op 1: todo 999 not found; nothing was changed"

    r = await client.post(
        "/todos/batch",
        json=[{"op": "create", "title": "Kept?"}, {"op": "create", "title": ""}],
    )
    assert r.status_code == 422
    r = await client.post(
        "/todos/batch",
        json=[{"op": "update", "id": 1, "title": ""}],
    )
    assert r.status_code == 422
    assert (await client.post("/todos/batch", json=[])).status_code == 422

    assert (await client.get("/todos?status=all")).json() == []


@pytest.mark.anyio
async def test_api_projects(client: httpx.AsyncClient) -> None:
    r = await client.post("/todos", json={"title": "Fix login", "project": " Tibia "})
    todo = r.json()
    assert todo["project"] == "tibia"  # trimmed and lowercased
    assert (await client.post("/todos", json={"title": "Loose"})).json()[
        "project"
    ] is None
    r = await client.post("/todos", json={"title": "Blank", "project": ""})
    assert r.json()["project"] is None

    r = await client.patch(f"/todos/{todo['id']}", json={"title": "Fix logins"})
    assert r.json()["project"] == "tibia"  # untouched
    r = await client.patch(f"/todos/{todo['id']}", json={"project": "smurfskins"})
    assert r.json()["project"] == "smurfskins"

    r = await client.post(
        "/todos/batch", json=[{"op": "create", "title": "Batch", "project": "api"}]
    )
    assert r.json()[0]["project"] == "api"
    assert (await client.get("/projects")).json() == ["api", "smurfskins"]
    r = await client.get("/todos", params={"project": "SMURFSKINS"})
    assert [t["id"] for t in r.json()] == [todo["id"]]

    r = await client.patch(f"/todos/{todo['id']}", json={"project": None})
    assert r.json()["project"] is None


@pytest.mark.anyio
async def test_migrates_title_prefixes(tmp_path: Path) -> None:
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " title TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,"
            " assignee_id INTEGER, created_by INTEGER, created_at TEXT NOT NULL,"
            " done_at TEXT)"
        )
        await db.executemany(
            "INSERT INTO todos (title, created_at) VALUES (?, '2026-10-01T00:00:00')",
            [("Tibia-Engine: Fix login",), ("Meet at 10:30",), ("No project",)],
        )
        await db.commit()
    store = await TodoStore.open(path)
    try:
        todos = await store.list_todos("all")
        assert [(t.project, t.title) for t in todos] == [
            ("tibia-engine", "Fix login"),
            (None, "Meet at 10:30"),
            (None, "No project"),
        ]
        # The old, now unused assignee_id column doesn't get in the way.
        added = await store.add("New", project="todos")
        assert (await store.get(added.id)) == added
    finally:
        await store.close()
