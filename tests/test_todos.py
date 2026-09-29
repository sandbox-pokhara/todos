from collections.abc import AsyncIterator

import httpx
import pytest

from todo_bot.api import create_app
from todo_bot.directory import Member
from todo_bot.store import TodoStore

API_KEY = "test-key"
ALICE = Member(id=111111111111111111, name="Alice", username="alice")


class FakeDirectory:
    def ready(self) -> bool:
        return True

    def members(self) -> list[Member]:
        return [ALICE]

    def member(self, user_id: int) -> Member | None:
        return ALICE if user_id == ALICE.id else None


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
    app = create_app(store, FakeDirectory(), API_KEY)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {API_KEY}"},
    ) as c:
        yield c


@pytest.mark.anyio
async def test_store_lifecycle(store: TodoStore) -> None:
    todo = await store.add("Write docs", assignee_id=ALICE.id, created_by=1)
    assert not todo.done and todo.assignee_id == ALICE.id

    done = await store.update(todo.id, done=True)
    assert done and done.done and done.done_at

    assert await store.list_todos("open") == []
    assert [t.id for t in await store.list_todos("done")] == [todo.id]

    unassigned = await store.update(todo.id, assignee_id=None, done=False)
    assert unassigned and unassigned.assignee_id is None and unassigned.done_at is None

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
    r = await client.post(
        "/todos", json={"title": "Ship it", "assignee_id": str(ALICE.id)}
    )
    assert r.status_code == 201
    todo = r.json()
    assert todo["assignee_name"] == "Alice"
    assert todo["assignee_id"] == str(ALICE.id)

    r = await client.patch(f"/todos/{todo['id']}", json={"done": True})
    assert r.json()["done"] is True
    assert r.json()["assignee_id"] == str(ALICE.id)  # untouched

    r = await client.patch(f"/todos/{todo['id']}", json={"assignee_id": None})
    assert r.json()["assignee_id"] is None

    assert (await client.get("/todos")).json() == []
    assert len((await client.get("/todos", params={"status": "all"})).json()) == 1

    assert (await client.delete(f"/todos/{todo['id']}")).status_code == 204
    assert (await client.get(f"/todos/{todo['id']}")).status_code == 404


@pytest.mark.anyio
async def test_api_validation(client: httpx.AsyncClient) -> None:
    assert (await client.post("/todos", json={"title": ""})).status_code == 422
    r = await client.post("/todos", json={"title": "x", "assignee_id": "999"})
    assert r.status_code == 422
    members = (await client.get("/members")).json()
    assert members == [{"id": str(ALICE.id), "name": "Alice", "username": "alice"}]


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
            {"op": "create", "title": "Next", "assignee_id": str(ALICE.id)},
            {"op": "update", "id": old["id"] + 2, "title": "Next up"},
        ],
    )
    assert r.status_code == 200
    deleted, shipped, created, renamed = r.json()
    assert deleted is None
    assert shipped["done"] and shipped["done_at"]
    assert created["assignee_name"] == "Alice"
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
        json=[{"op": "create", "title": "x", "assignee_id": "999"}],
    )
    assert r.status_code == 422
    assert (await client.post("/todos/batch", json=[])).status_code == 422

    assert (await client.get("/todos?status=all")).json() == []
