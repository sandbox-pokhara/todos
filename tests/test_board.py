from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from itertools import count
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from todo_bot.board import Board, render_day, render_todo
from todo_bot.store import BoardLocation, TodoStore

CHANNEL_ID = 1


def not_found() -> discord.NotFound:
    return discord.NotFound(cast(Any, SimpleNamespace(status=404, reason="")), "")


class FakeChannel:
    """Just enough of a PartialMessageable: messages in order, by id."""

    def __init__(self, channel_id: int, ids: "count[int]") -> None:
        self.id = channel_id
        self._ids = ids
        self.messages: dict[int, Any] = {}

    async def send(
        self, content: str | None = None, *, embed: discord.Embed | None = None
    ) -> SimpleNamespace:
        message_id = next(self._ids)
        self.messages[message_id] = content if embed is None else embed
        return SimpleNamespace(id=message_id)

    def get_partial_message(self, message_id: int) -> SimpleNamespace:
        async def edit(
            content: str | None = None, embed: discord.Embed | None = None
        ) -> None:
            if message_id not in self.messages:
                raise not_found()
            self.messages[message_id] = content if embed is None else embed

        async def delete() -> None:
            if self.messages.pop(message_id, None) is None:
                raise not_found()

        return SimpleNamespace(edit=edit, delete=delete)

    def layout(self) -> list[str]:
        """Each message, top to bottom: its text, or an embed's title."""
        return [
            m if isinstance(m, str) else m.title
            for _, m in sorted(self.messages.items())
        ]


class FakeClient:
    def __init__(self) -> None:
        ids = count(100)
        self.channels = {i: FakeChannel(i, ids) for i in (CHANNEL_ID, 2)}

    def get_partial_messageable(self, channel_id: int) -> FakeChannel:
        return self.channels[channel_id]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def store() -> AsyncIterator[TodoStore]:
    s = await TodoStore.open(":memory:")
    yield s
    await s.close()


def make_board(store: TodoStore) -> tuple[Board, FakeClient]:
    client = FakeClient()
    return Board(cast(discord.Client, client), store), client


async def refresh(board: Board) -> None:
    await board._refresh()  # pyright: ignore[reportPrivateUsage]


async def complete_on(store: TodoStore, title: str, day: date) -> int:
    todo = await store.add(title)
    await store.update(todo.id, done=True)
    await store._db.execute(  # pyright: ignore[reportPrivateUsage]
        "UPDATE todos SET done_at = ? WHERE id = ?",
        (
            datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).isoformat(),
            todo.id,
        ),
    )
    return todo.id


SEP_28, SEP_29, SEP_30 = date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)


@pytest.mark.anyio
async def test_board_location(store: TodoStore) -> None:
    assert await store.get_board() is None
    await store.set_board(BoardLocation(1, 3))
    await store.set_board(BoardLocation(4, 6))
    assert await store.get_board() == BoardLocation(4, 6)


@pytest.mark.anyio
async def test_render(store: TodoStore) -> None:
    assert render_todo([]).description == "Nothing to do"

    first = await store.add("Ship it")
    second = await store.add("Write docs")
    assert render_todo(await store.list_todos("open")).description == (
        f"#{first.id} Ship it\n#{second.id} Write docs"
    )


@pytest.mark.anyio
async def test_done_by_day(store: TodoStore) -> None:
    a = await complete_on(store, "Ship it", SEP_29)
    b = await complete_on(store, "Old one", SEP_28)
    c = await complete_on(store, "Write docs", SEP_29)
    await store.add("Still open")
    days = await store.done_by_day()
    assert list(days) == [SEP_28, SEP_29]
    assert [t.id for t in days[SEP_29]] == [a, c]

    embed = render_day(SEP_29, days[SEP_29])
    assert embed.title == "Completed - Sep 29, 2026"
    assert embed.description == f"#{a} Ship it ✅\n#{c} Write docs ✅"
    assert embed.footer.text == "2 todos done"
    assert render_day(SEP_28, days[SEP_28]).footer.text == "1 todo done"
    assert b


@pytest.mark.anyio
async def test_long_day_is_cut_off(store: TodoStore) -> None:
    for i in range(40):
        await complete_on(store, f"{i:02} " + "x" * 190, SEP_29)
    embed = render_day(SEP_29, (await store.done_by_day())[SEP_29])
    assert len(embed.description or "") <= 4096
    assert (embed.description or "").endswith("more")
    assert embed.footer.text == "40 todos done"  # counts all of them


@pytest.mark.anyio
async def test_board_sync(store: TodoStore) -> None:
    board, client = make_board(store)
    channel = client.channels[CHANNEL_ID]

    todo = await store.add("Ship it")
    await complete_on(store, "Old one", SEP_28)
    await board.move_to(CHANNEL_ID)
    assert channel.layout() == ["Completed - Sep 28, 2026", "TODO"]
    location = await store.get_board()

    # Completing on a new day adds its message above TODO.
    await store.update(todo.id, done=True)
    today = datetime.now(timezone.utc).date()
    await refresh(board)
    assert channel.layout() == [
        "Completed - Sep 28, 2026",
        f"Completed - {today:%b %d, %Y}",
        "TODO",
    ]
    assert await store.get_board() != location
    location = await store.get_board()

    # Same day: edits in place.
    other = await store.add("Write docs")
    await store.update(other.id, done=True)
    await refresh(board)
    assert await store.get_board() == location
    [_, day, open_] = channel.messages.values()
    assert "Write docs ✅" in (day.description or "")
    assert day.footer.text == "2 todos done"
    assert open_.description == "Nothing to do"

    # Reopening every todo of a day removes its message.
    await store.update(todo.id, done=False)
    await store.update(other.id, done=False)
    await refresh(board)
    assert channel.layout() == ["Completed - Sep 28, 2026", "TODO"]

    # If someone deletes a day, it and everything below are reposted in order.
    await store.update(todo.id, done=True)
    await refresh(board)
    first_day = (await store.day_messages())[SEP_28]
    del channel.messages[first_day.message_id]
    await board.message_deleted(first_day.message_id)
    await refresh(board)
    assert channel.layout() == [
        "Completed - Sep 28, 2026",
        f"Completed - {today:%b %d, %Y}",
        "TODO",
    ]

    # Moving clears the old channel.
    await board.move_to(2)
    assert channel.layout() == []
    assert client.channels[2].layout() == [
        "Completed - Sep 28, 2026",
        f"Completed - {today:%b %d, %Y}",
        "TODO",
    ]


@pytest.mark.anyio
async def test_replaces_legacy_completed_message(store: TodoStore) -> None:
    board, client = make_board(store)
    channel = client.channels[CHANNEL_ID]
    legacy = await channel.send(embed=discord.Embed(title="Completed"))
    todo = await channel.send(embed=discord.Embed(title="TODO"))
    await store._db.execute(  # pyright: ignore[reportPrivateUsage]
        "INSERT INTO board (id, channel_id, message_id, done_message_id)"
        " VALUES (1, ?, ?, ?)",
        (CHANNEL_ID, todo.id, legacy.id),
    )
    await complete_on(store, "Old one", SEP_28)
    await refresh(board)
    assert channel.layout() == ["Completed - Sep 28, 2026", "TODO"]
