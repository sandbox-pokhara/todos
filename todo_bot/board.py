import asyncio
import logging
from datetime import datetime, timezone

import discord

from todo_bot.store import BoardLocation, DoneMessage, Todo, TodoStore

log = logging.getLogger(__name__)

EMBED_LIMIT = 4000
MESSAGE_LIMIT = 2000
DONE_PER_MESSAGE = 10
# Batches bursts of changes into one edit and keeps well under Discord's rate limits.
REFRESH_DELAY = 1.0


def format_todo(todo: Todo) -> str:
    box = "✅" if todo.done else "⬜"
    title = f"~~{todo.title}~~" if todo.done else todo.title
    line = f"{box} **#{todo.id}** {title}"
    if todo.assignee_id:
        # Mentions render as names; AllowedMentions.none() on the client stops pings.
        line += f" — <@{todo.assignee_id}>"
    return line


def format_todos(todos: list[Todo]) -> str:
    """One line per todo, cut off to fit in an embed description."""
    lines: list[str] = []
    length = 0
    for i, todo in enumerate(todos):
        line = format_todo(todo)
        if length + len(line) > EMBED_LIMIT:
            lines.append(f"…and {len(todos) - i} more")
            break
        lines.append(line)
        length += len(line) + 1
    return "\n".join(lines)


def render_board(todos: list[Todo]) -> discord.Embed:
    embed = discord.Embed(
        title="📋 Todo board",
        description=format_todos(todos) or "Nothing open 🎉",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"{len(todos)} open · updated")
    return embed


def format_done(todo: Todo) -> str:
    line = f"• {todo.title} ✅"
    if todo.assignee_id:
        line += f" — <@{todo.assignee_id}>"
    return line


def render_done(todos: list[Todo]) -> str:
    # Renaming a todo can push a full message past the limit; clip rather than fail.
    return "\n".join(format_done(t) for t in todos)[:MESSAGE_LIMIT]


def _fits(todos: list[Todo]) -> bool:
    return (
        len(todos) <= DONE_PER_MESSAGE
        and len("\n".join(format_done(t) for t in todos)) <= MESSAGE_LIMIT
    )


def pack_done(
    unposted: list[Todo], last: list[Todo] | None
) -> tuple[list[Todo] | None, list[list[Todo]]]:
    """Top up the last completed-todo message, then split the rest into new ones.

    Returns the last message's new todos (None if there is no last message) and
    the todos for each new message.
    """
    topped_up = list(last) if last is not None else None
    new: list[list[Todo]] = []
    current = topped_up
    for todo in unposted:
        if current is None or not _fits([*current, todo]):
            current = list[Todo]()
            new.append(current)
        current.append(todo)
    return topped_up, new


class Board:
    """The board channel: completed todos, 10 per message, with the open todos in
    one embed below them.

    Changes only mark the board dirty; one background task does the syncing, so
    syncs never overlap and a burst of changes becomes a single sync.
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
        """Repost everything in `channel_id` and delete the old messages."""
        async with self._lock:
            old = await self.store.get_board()
            old_done = await self.store.reset_done_messages()
            await self._sync(self._channel(channel_id), None)
            if old:
                old_channel = self._channel(old.channel_id)
                for message_id in [*old_done, old.message_id]:
                    await self._delete(old_channel, message_id)

    async def _refresh(self) -> None:
        async with self._lock:
            location = await self.store.get_board()
            if location:
                await self._sync(self._channel(location.channel_id), location)

    async def _sync(
        self, channel: discord.PartialMessageable, board: BoardLocation | None
    ) -> None:
        await self.store.detach_reopened()
        messages = await self.store.done_messages()
        topped_up, new = pack_done(
            await self.store.unposted_done(), messages[-1].todos if messages else None
        )
        if messages and topped_up is not None:
            last = messages[-1]
            messages[-1] = DoneMessage(last.message_id, last.content, topped_up)

        for message in messages:
            if not await self._update_done(channel, message):
                # Someone deleted it; its todos are unposted again, so start over.
                return await self._sync(channel, board)

        for todos in new:
            content = render_done(todos)
            sent = await channel.send(content)
            await self.store.save_done_message(sent.id, content, [t.id for t in todos])

        embed = render_board(await self.store.list_todos("open"))
        if board and not new:
            try:
                await channel.get_partial_message(board.message_id).edit(embed=embed)
                return
            except discord.NotFound:
                board = None  # deleted by someone; post a fresh one
        # New completed messages landed below the board: repost it at the bottom.
        sent = await channel.send(embed=embed)
        await self.store.set_board(BoardLocation(channel.id, sent.id))
        if board:
            await self._delete(channel, board.message_id)

    async def _update_done(
        self, channel: discord.PartialMessageable, message: DoneMessage
    ) -> bool:
        """Edit or delete a completed-todo message; False if it no longer exists."""
        if not message.todos:
            await self._delete(channel, message.message_id)
            await self.store.delete_done_message(message.message_id)
            return True
        content = render_done(message.todos)
        if content != message.content:
            try:
                await channel.get_partial_message(message.message_id).edit(
                    content=content
                )
            except discord.NotFound:
                await self.store.delete_done_message(message.message_id)
                return False
        await self.store.save_done_message(
            message.message_id, content, [t.id for t in message.todos]
        )
        return True

    async def _delete(
        self, channel: discord.PartialMessageable, message_id: int
    ) -> None:
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.HTTPException:
            pass  # already gone, or no access to the old channel

    def _channel(self, channel_id: int) -> discord.PartialMessageable:
        return self.client.get_partial_messageable(channel_id)
