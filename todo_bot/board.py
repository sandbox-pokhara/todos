import asyncio
import logging

import discord

from todo_bot.store import BoardLocation, Todo, TodoStore

log = logging.getLogger(__name__)

EMBED_LIMIT = 4000
# Batches bursts of changes into one edit and keeps well under Discord's rate limits.
REFRESH_DELAY = 1.0


def format_todo(todo: Todo, *, assignee: bool = False) -> str:
    line = f"#{todo.id} {todo.title}"
    if todo.done:
        line += " ✅"
    if assignee and todo.assignee_id:
        # Mentions render as names; AllowedMentions.none() on the client stops pings.
        line += f" — <@{todo.assignee_id}>"
    return line


def format_done(todo: Todo) -> str:
    line = format_todo(todo)
    if todo.done_at:
        line += f" - {todo.done_at:%b %d}"  # UTC date; close enough
    return line


def format_todos(todos: list[Todo], *, assignee: bool = False) -> str:
    """One line per todo, cut off to fit in an embed description."""
    lines: list[str] = []
    length = 0
    for i, todo in enumerate(todos):
        line = format_todo(todo, assignee=assignee)
        if length + len(line) > EMBED_LIMIT:
            lines.append(f"…and {len(todos) - i} more")
            break
        lines.append(line)
        length += len(line) + 1
    return "\n".join(lines)


def render_todo(todos: list[Todo]) -> discord.Embed:
    return discord.Embed(
        title="TODO",
        description=format_todos(todos) or "Nothing to do",
        color=discord.Color.blurple(),
    )


def render_done(newest_first: list[Todo]) -> discord.Embed:
    """As many of the latest completed todos as fit, oldest at the top."""
    lines: list[str] = []
    length = 0
    for todo in newest_first:
        line = format_done(todo)
        if length + len(line) > EMBED_LIMIT:
            break
        lines.append(line)
        length += len(line) + 1
    return discord.Embed(
        title="Completed",
        description="\n".join(reversed(lines)) or "Nothing completed yet",
        color=discord.Color.green(),
    )


class Board:
    """Two messages in the board channel: recently completed todos, then the open
    todos below them. Both are edited in place.

    Changes only mark the board dirty; one background task does the editing, so
    edits never overlap and a burst of changes becomes a single edit.
    """

    def __init__(self, client: discord.Client, store: TodoStore) -> None:
        self.client = client
        self.store = store
        self._dirty = asyncio.Event()
        self._lock = asyncio.Lock()

    def request_refresh(self) -> None:
        self._dirty.set()

    async def run(self) -> None:
        await self.client.wait_until_ready()
        try:
            await self._migrate_legacy()
        except Exception:
            log.exception("Failed to replace the old board layout")
        self.request_refresh()  # catch up on anything that changed while offline
        while True:
            await self._dirty.wait()
            await asyncio.sleep(REFRESH_DELAY)
            self._dirty.clear()
            try:
                await self._refresh()
            except Exception:
                log.exception("Failed to refresh the board")

    async def move_to(self, channel_id: int) -> None:
        """Post the board in `channel_id` and delete the old one, if any."""
        async with self._lock:
            old = await self.store.get_board()
            await self._post(self._channel(channel_id))
        if old:
            await self._delete(
                old.channel_id, [old.done_message_id, old.todo_message_id]
            )

    async def _refresh(self) -> None:
        async with self._lock:
            board = await self.store.get_board()
            if not board:
                return
            channel = self._channel(board.channel_id)
            done, todo = await self._render()
            try:
                await channel.get_partial_message(board.done_message_id).edit(
                    embed=done
                )
                await channel.get_partial_message(board.todo_message_id).edit(
                    embed=todo
                )
                return
            except discord.NotFound:
                # Someone deleted one: repost both so Completed stays on top.
                await self._post(channel)
        await self._delete(
            board.channel_id, [board.done_message_id, board.todo_message_id]
        )

    async def _post(self, channel: discord.PartialMessageable) -> None:
        done, todo = await self._render()
        done_message = await channel.send(embed=done)
        todo_message = await channel.send(embed=todo)
        await self.store.set_board(
            BoardLocation(channel.id, done_message.id, todo_message.id)
        )

    async def _render(self) -> tuple[discord.Embed, discord.Embed]:
        return (
            render_done(await self.store.recent_done()),
            render_todo(await self.store.list_todos("open")),
        )

    async def _migrate_legacy(self) -> None:
        """Swap the old completed-todo messages for the two-message board."""
        legacy = await self.store.legacy_board_messages()
        if legacy:
            channel_id, message_ids = legacy
            async with self._lock:
                await self._post(self._channel(channel_id))
            await self._delete(channel_id, message_ids)
        await self.store.drop_legacy_board()

    async def _delete(self, channel_id: int, message_ids: list[int]) -> None:
        channel = self._channel(channel_id)
        for message_id in message_ids:
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.HTTPException:
                pass  # already gone, or no access to the old channel

    def _channel(self, channel_id: int) -> discord.PartialMessageable:
        return self.client.get_partial_messageable(channel_id)
