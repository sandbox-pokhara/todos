from collections.abc import AsyncIterator
from datetime import datetime, timezone
from itertools import count
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from todo_bot.board import Board, render_done, render_todo
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


@pytest.mark.anyio
async def test_board_location(store: TodoStore) -> None:
    assert await store.get_board() is None
    await store.set_board(BoardLocation(1, 2, 3))
    await store.set_board(BoardLocation(4, 5, 6))
    assert await store.get_board() == BoardLocation(4, 5, 6)


@pytest.mark.anyio
async def test_render(store: TodoStore) -> None:
    assert render_todo([]).description == "Nothing to do"
    assert render_done([]).description == "Nothing completed yet"

    first = await store.add("Ship it")
    second = await store.add("Write docs")
    assert render_todo(await store.list_todos("open")).description == (
        f"#{first.id} Ship it\n#{second.id} Write docs"
    )

    for todo in (first, second):
        await store.update(todo.id, done=True)
    lines = (render_done(await store.recent_done()).description or "").splitlines()
    today = f"{datetime.now(timezone.utc):%b %d}"
    assert lines == [  # oldest on top
        f"#{first.id} Ship it ✅ - {today}",
        f"#{second.id} Write docs ✅ - {today}",
    ]


@pytest.mark.anyio
async def test_completed_keeps_the_newest_that_fit(store: TodoStore) -> None:
    for i in range(40):
        todo = await store.add(f"{i:02} " + "x" * 190)
        await store.update(todo.id, done=True)
    description = render_done(await store.recent_done()).description or ""
    assert len(description) <= 4096
    assert description.splitlines()[-1].startswith("#40 39 ")  # newest at the bottom
    assert "#1 00 " not in description  # oldest dropped


@pytest.mark.anyio
async def test_board_sync(store: TodoStore) -> None:
    board, client = make_board(store)
    channel = client.channels[CHANNEL_ID]

    todo = await store.add("Ship it")
    await board.move_to(CHANNEL_ID)
    assert channel.layout() == ["Completed", "TODO"]
    location = await store.get_board()

    # Changes are edits: same two messages, new contents.
    await store.update(todo.id, done=True)
    await refresh(board)
    assert await store.get_board() == location
    [done, open_] = channel.messages.values()
    assert "Ship it ✅" in (done.description or "")
    assert open_.description == "Nothing to do"

    # If someone deletes one, both are reposted so Completed stays on top.
    assert location
    del channel.messages[location.done_message_id]
    await refresh(board)
    assert channel.layout() == ["Completed", "TODO"]

    # Moving clears the old channel.
    await board.move_to(2)
    assert channel.layout() == []
    assert client.channels[2].layout() == ["Completed", "TODO"]
