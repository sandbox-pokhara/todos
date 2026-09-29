from collections.abc import AsyncIterator

import httpx
import pytest

from todo_bot.api import create_app
from todo_bot.board import render_board
from todo_bot.directory import Member
from todo_bot.store import BoardLocation, TodoStore

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
async def test_board_location(store: TodoStore) -> None:
    assert await store.get_board() is None
    await store.set_board(BoardLocation(channel_id=1, message_id=2))
    await store.set_board(BoardLocation(channel_id=3, message_id=4))
    assert await store.get_board() == BoardLocation(channel_id=3, message_id=4)


@pytest.mark.anyio
async def test_render_board(store: TodoStore) -> None:
    assert render_board([]).description == "Nothing open 🎉"

    todo = await store.add("Ship it", assignee_id=ALICE.id)
    embed = render_board(await store.list_todos("open"))
    assert embed.description == f"⬜ **#{todo.id}** Ship it — <@{ALICE.id}>"
    assert embed.footer.text == "1 open · updated"
