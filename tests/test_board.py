from collections.abc import AsyncIterator
from itertools import count
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from todo_bot.board import Board, pack_done
from todo_bot.store import TodoStore

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
        """Each message, top to bottom; the board shows as 'BOARD'."""
        return [
            m if isinstance(m, str) else "BOARD"
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


async def add_done(store: TodoStore, *titles: str) -> list[int]:
    ids: list[int] = []
    for title in titles:
        todo = await store.add(title)
        await store.update(todo.id, done=True)
        ids.append(todo.id)
    return ids


def lines(n: int, start: int = 0) -> str:
    return "\n".join(f"• t{i} ✅" for i in range(start, start + n))


@pytest.mark.anyio
async def test_pack_done(store: TodoStore) -> None:
    await add_done(store, *(f"t{i}" for i in range(25)))
    todos = await store.unposted_done()

    topped_up, new = pack_done(todos, None)
    assert topped_up is None and [len(c) for c in new] == [10, 10, 5]

    topped_up, new = pack_done(todos[:3], todos[3:10])
    assert topped_up == todos[3:10] + todos[:3] and new == []

    topped_up, new = pack_done(todos[:12], [])
    assert topped_up == todos[:10] and new == [todos[10:12]]


@pytest.mark.anyio
async def test_board_sync(store: TodoStore) -> None:
    client = FakeClient()
    channel = client.channels[CHANNEL_ID]
    board = Board(cast(discord.Client, client), store)

    await add_done(store, *(f"t{i}" for i in range(12)))
    await store.add("still open")
    await board.move_to(CHANNEL_ID)
    assert channel.layout() == [lines(10), lines(2, 10), "BOARD"]

    # Topping up the last message only edits; the board stays where it is.
    before = await store.get_board()
    await add_done(store, *(f"t{i}" for i in range(12, 20)))
    await board._refresh()  # pyright: ignore[reportPrivateUsage]
    assert channel.layout() == [lines(10), lines(10, 10), "BOARD"]
    assert await store.get_board() == before

    # A new message goes below the board, so the board is reposted under it.
    [t20] = await add_done(store, "t20")
    await board._refresh()  # pyright: ignore[reportPrivateUsage]
    assert channel.layout() == [lines(10), lines(10, 10), lines(1, 20), "BOARD"]
    assert await store.get_board() != before

    # Reopening removes it from its message; an emptied message is deleted.
    await store.update(t20, done=False)
    await board._refresh()  # pyright: ignore[reportPrivateUsage]
    assert channel.layout() == [lines(10), lines(10, 10), "BOARD"]

    # Moving reposts everything in the new channel and clears the old one.
    await board.move_to(2)
    assert channel.layout() == []
    assert client.channels[2].layout() == [lines(10), lines(10, 10), "BOARD"]


@pytest.mark.anyio
async def test_board_recovers_deleted_message(store: TodoStore) -> None:
    client = FakeClient()
    channel = client.channels[CHANNEL_ID]
    board = Board(cast(discord.Client, client), store)

    await add_done(store, *(f"t{i}" for i in range(3)))
    await board.move_to(CHANNEL_ID)
    first = min(channel.messages)
    del channel.messages[first]  # someone deletes the completed message

    await add_done(store, "t3")
    await board._refresh()  # pyright: ignore[reportPrivateUsage]
    assert channel.layout() == [lines(4), "BOARD"]
