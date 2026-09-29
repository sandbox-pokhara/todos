import asyncio
import logging
from datetime import datetime, timezone

import discord

from todo_bot.store import BoardLocation, Todo, TodoStore

log = logging.getLogger(__name__)

EMBED_LIMIT = 4000
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


class Board:
    """The single message in the board channel that lists the open todos.

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
        self.request_refresh()  # catch up on anything that changed while offline
        while True:
            await self._dirty.wait()
            await asyncio.sleep(REFRESH_DELAY)
            self._dirty.clear()
            try:
                await self._refresh()
            except Exception:
                log.exception("Failed to refresh the board")

    async def move_to(self, channel: discord.abc.Messageable) -> discord.Message:
        """Post the board in `channel` and delete the old one, if any."""
        async with self._lock:
            old = await self.store.get_board()
            message = await channel.send(embed=await self._render())
            await self.store.set_board(BoardLocation(message.channel.id, message.id))
            if old and old.message_id != message.id:
                try:
                    await (
                        self._channel(old.channel_id)
                        .get_partial_message(old.message_id)
                        .delete()
                    )
                except discord.HTTPException:
                    pass  # already gone, or no access to the old channel
            return message

    async def _refresh(self) -> None:
        async with self._lock:
            location = await self.store.get_board()
            if not location:
                return
            embed = await self._render()
            channel = self._channel(location.channel_id)
            try:
                await channel.get_partial_message(location.message_id).edit(embed=embed)
            except discord.NotFound:
                # Someone deleted the board message: post a fresh one in its place.
                message = await channel.send(embed=embed)
                await self.store.set_board(BoardLocation(channel.id, message.id))

    async def _render(self) -> discord.Embed:
        return render_board(await self.store.list_todos("open"))

    def _channel(self, channel_id: int) -> discord.PartialMessageable:
        return self.client.get_partial_messageable(channel_id)
