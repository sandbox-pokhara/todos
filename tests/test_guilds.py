"""Every server's todos, projects and board are its own: no key, command or
board of one server can read or change another's."""

from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import aiosqlite
import discord
import httpx
import pytest

from todo_bot.api import create_app
from todo_bot.bot import TodoBot, TodoCommands
from todo_bot.config import Guild, parse_guilds
from todo_bot.store import BoardLocation, DayMessage, TodoStore

WORK = Guild(1001, "test-key-for-work")
PERSONAL = Guild(2002, "test-key-for-personal")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> AsyncIterator[TodoStore]:
    s = await TodoStore.open(":memory:")
    yield s
    await s.close()


def api_client(db: TodoStore, key: str) -> httpx.AsyncClient:
    names = {WORK.id: "Work", PERSONAL.id: "Personal"}
    app = create_app(db, [WORK, PERSONAL], lambda: True, names.get)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {key}"},
    )


@pytest.fixture
async def work(db: TodoStore) -> AsyncIterator[httpx.AsyncClient]:
    async with api_client(db, WORK.api_key) as c:
        yield c


@pytest.fixture
async def personal(db: TodoStore) -> AsyncIterator[httpx.AsyncClient]:
    async with api_client(db, PERSONAL.api_key) as c:
        yield c


@pytest.mark.anyio
async def test_each_key_names_its_server(
    db: TodoStore, work: httpx.AsyncClient, personal: httpx.AsyncClient
) -> None:
    assert (await work.get("/server")).json() == {"id": "1001", "name": "Work"}
    assert (await personal.get("/server")).json() == {"id": "2002", "name": "Personal"}
    async with api_client(db, "test-key-for-nobody") as nobody:
        assert (await nobody.get("/server")).status_code == 401
        assert (await nobody.get("/todos")).status_code == 401
    async with api_client(db, "") as blank:
        assert (await blank.get("/todos")).status_code == 401


@pytest.mark.anyio
async def test_servers_only_see_their_own_todos(
    work: httpx.AsyncClient, personal: httpx.AsyncClient
) -> None:
    job = (await work.post("/todos", json={"title": "Ship", "project": "api"})).json()
    home = (await personal.post("/todos", json={"title": "Groceries"})).json()
    await personal.post("/todos", json={"title": "Taxes", "project": "money"})

    assert [t["title"] for t in (await work.get("/todos?status=all")).json()] == [
        "Ship"
    ]
    assert [t["title"] for t in (await personal.get("/todos?status=all")).json()] == [
        "Groceries",
        "Taxes",
    ]
    assert (await work.get("/projects")).json() == ["api"]
    assert (await personal.get("/projects")).json() == ["money"]
    assert (await work.get("/todos", params={"project": "money"})).json() == []

    # Another server's todo looks exactly like one that doesn't exist.
    for client, other in ((work, home), (personal, job)):
        theirs = f"/todos/{other['id']}"
        assert (await client.get(theirs)).status_code == 404
        r = await client.patch(theirs, json={"title": "Mine now", "done": True})
        assert r.status_code == 404
        assert r.json()["detail"] == f"Todo {other['id']} not found"
        assert (await client.delete(theirs)).status_code == 404

    # Nothing was changed by the attempts.
    assert (await work.get(f"/todos/{job['id']}")).json() == job
    assert (await personal.get(f"/todos/{home['id']}")).json() == home


@pytest.mark.anyio
async def test_batches_cannot_reach_another_server(
    work: httpx.AsyncClient, personal: httpx.AsyncClient
) -> None:
    home = (await personal.post("/todos", json={"title": "Groceries"})).json()
    for op in (
        {"op": "update", "id": home["id"], "done": True},
        {"op": "delete", "id": home["id"]},
    ):
        r = await work.post("/todos/batch", json=[{"op": "create", "title": "X"}, op])
        assert r.status_code == 404
    assert (await work.get("/todos?status=all")).json() == []  # all or nothing
    assert (await personal.get(f"/todos/{home['id']}")).json() == home


@pytest.mark.anyio
async def test_changes_refresh_only_their_server(
    db: TodoStore, work: httpx.AsyncClient, personal: httpx.AsyncClient
) -> None:
    changes: list[int] = []
    db.add_listener(changes.append)
    await personal.post("/todos", json={"title": "Groceries"})
    await work.post("/todos/batch", json=[{"op": "create", "title": "Ship"}])
    assert changes == [PERSONAL.id, WORK.id]


@pytest.mark.anyio
async def test_search_and_board_state_are_per_server(db: TodoStore) -> None:
    work, personal = db.guild(WORK.id), db.guild(PERSONAL.id)
    job = await work.add("Fix login", project="api")
    home = await personal.add("Fix the sink")
    assert [t.id for t in await work.search("Fix")] == [job.id]
    assert [t.id for t in await personal.search(f"#{job.id}")] == []
    assert await personal.update(job.id, done=True) is None
    assert not await personal.delete(job.id)
    assert (await work.get(job.id)) == job

    await work.set_board(BoardLocation(10, 11))
    assert await personal.get_board() is None
    await personal.set_board(BoardLocation(20, 21))
    await personal.set_board_layout("project")
    assert await work.get_board() == BoardLocation(10, 11)
    assert await work.board_layout() == "list"

    await work.update(job.id, done=True)
    await personal.update(home.id, done=True)
    assert [t.id for d in (await work.done_by_day()).values() for t in d] == [job.id]

    day = date(2026, 11, 25)
    await work.save_day_message(DayMessage(day, 12, "work"))
    await personal.save_day_message(DayMessage(day, 22, "home"))
    assert (await work.day_messages())[day].content == "work"
    assert not await work.forget_day_message(22)  # personal's message
    assert await personal.clear_day_messages() == [22]
    assert (await work.day_messages())[day].message_id == 12


@pytest.mark.anyio
async def test_migrates_a_single_server_database(tmp_path: Path) -> None:
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as old:
        await old.executescript(
            """
            CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, project TEXT, done INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER, created_at TEXT NOT NULL, done_at TEXT);
            CREATE TABLE board (id INTEGER PRIMARY KEY CHECK (id = 1),
                channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                done_message_id INTEGER, layout TEXT NOT NULL DEFAULT 'list');
            CREATE TABLE day_messages (day TEXT PRIMARY KEY,
                message_id INTEGER NOT NULL, content TEXT NOT NULL);
            INSERT INTO todos (title, project, created_at)
                VALUES ('Ship', 'api', '2026-10-01T00:00:00');
            INSERT INTO board VALUES (1, 50, 51, NULL, 'project');
            INSERT INTO day_messages VALUES ('2026-10-01', 52, 'Completed');
            """
        )
        await old.commit()

    # Opening it again is a no-op: the old todos stay with the first server, and
    # todos added since stay with theirs.
    for personal_titles in ([], ["Groceries"]):
        db = await TodoStore.open(path, legacy_guild_id=WORK.id)
        try:
            work, personal = db.guild(WORK.id), db.guild(PERSONAL.id)
            assert [t.title for t in await work.list_todos("all")] == ["Ship"]
            assert await work.get_board() == BoardLocation(50, 51)
            assert await work.board_layout() == "project"
            assert [m.message_id for m in (await work.day_messages()).values()] == [52]
            titles = [t.title for t in await personal.list_todos("all")]
            assert titles == personal_titles
            assert await personal.get_board() is None
            assert await personal.day_messages() == {}
            if not titles:
                await personal.add("Groceries")
        finally:
            await db.close()


@pytest.mark.anyio
async def test_bot_keeps_servers_apart(db: TodoStore) -> None:
    bot = TodoBot(db, [WORK.id, PERSONAL.id])
    try:
        work_board, personal_board = bot.boards[WORK.id], bot.boards[PERSONAL.id]
        assert work_board.store.guild_id == WORK.id
        assert personal_board.store.guild_id == PERSONAL.id

        await db.guild(PERSONAL.id).add("Groceries")
        assert personal_board._dirty.is_set()  # pyright: ignore[reportPrivateUsage]
        assert not work_board._dirty.is_set()  # pyright: ignore[reportPrivateUsage]
        await db.guild(3003).add("Unknown server")  # ignored, no crash

        commands = TodoCommands(work_board.store, work_board)
        for guild_id, allowed in ((WORK.id, True), (PERSONAL.id, False), (None, False)):
            interaction = cast(Any, SimpleNamespace(guild_id=guild_id))
            assert await commands.interaction_check(interaction) is allowed
    finally:
        await bot.close()
    assert isinstance(bot, discord.Client)


def test_parse_guilds() -> None:
    key_a, key_b = "a" * 20, "b" * 20
    assert parse_guilds(f"1:{key_a}, 2:{key_b}") == (Guild(1, key_a), Guild(2, key_b))
    # The old single-server settings still work, and come first.
    assert parse_guilds(f"2:{key_b}", "1", key_a) == (Guild(1, key_a), Guild(2, key_b))
    assert parse_guilds("", "1", key_a) == (Guild(1, key_a),)
    for bad in (
        "",
        "1",
        f"x:{key_a}",
        "1:short",
        f"1:{key_a},1:{key_b}",
        f"1:{key_a},2:{key_a}",  # one key for two servers
    ):
        with pytest.raises(ValueError):
            parse_guilds(bad)
    with pytest.raises(ValueError):
        parse_guilds("", "1", "")
