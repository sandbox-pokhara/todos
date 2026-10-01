import asyncio
import logging
from datetime import date

import discord

from todo_bot.store import BoardLocation, DayMessage, Todo, TodoStore

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
    embed = discord.Embed(
        title="TODO",
        description=format_todos(todos) or "Nothing to do",
        color=discord.Color.blurple(),
    )
    if todos:
        embed.set_footer(
            text=f"{len(todos)} {'todo' if len(todos) == 1 else 'todos'} open"
        )
    return embed


def render_day(day: date, todos: list[Todo]) -> discord.Embed:
    embed = discord.Embed(
        title=f"Completed - {day:%b %d, %Y}",
        description=format_todos(todos),
        color=discord.Color.green(),
    )
    embed.set_footer(text=f"{len(todos)} {'todo' if len(todos) == 1 else 'todos'} done")
    return embed


def _content(embed: discord.Embed) -> str:
    return f"{embed.title}\n{embed.description}\n{embed.footer.text}"


class Board:
    """The board channel: one Completed message per day (UTC) that has completed
    todos, oldest first, then the open todos below them. Messages are edited in
    place; a new day's message is posted at the bottom and TODO is reposted under it.

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

    async def message_deleted(self, message_id: int) -> None:
        """Repost a day whose message someone deleted. Unchanged days are never
        edited, so the next sync wouldn't notice on its own."""
        if await self.store.forget_day_message(message_id):
            self.request_refresh()

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
        """Repost the board in `channel_id` and delete the old messages, if any."""
        async with self._lock:
            old = await self.store.get_board()
            legacy = await self.store.pop_legacy_done_message()
            old_days = await self.store.clear_day_messages()
            await self._sync(self._channel(channel_id), None)
        if old:
            old_ids = [*old_days, old.todo_message_id]
            await self._delete(old.channel_id, [*old_ids, *filter(None, [legacy])])

    async def _refresh(self) -> None:
        async with self._lock:
            board = await self.store.get_board()
            if not board:
                return
            legacy = await self.store.pop_legacy_done_message()
            await self._sync(self._channel(board.channel_id), board)
        if legacy:
            await self._delete(board.channel_id, [legacy])

    async def _sync(
        self, channel: discord.PartialMessageable, board: BoardLocation | None
    ) -> None:
        days = await self.store.done_by_day()
        posted = await self.store.day_messages()

        # Days with nothing completed any more (reopened or deleted) go away.
        for day in posted.keys() - days.keys():
            await self._delete(channel.id, [posted.pop(day).message_id])
            await self.store.delete_day_message(day)

        # Messages can only go at the bottom, so every day from the first one
        # without a message onward is reposted, then TODO below them.
        missing = [day for day in days if day not in posted]
        repost_from = min(missing) if missing else None

        for day, message in sorted(posted.items()):
            if repost_from is not None and day >= repost_from:
                break
            embed = render_day(day, days[day])
            if _content(embed) == message.content:
                continue
            try:
                await channel.get_partial_message(message.message_id).edit(embed=embed)
            except discord.NotFound:
                # Someone deleted it: repost from here on.
                await self.store.delete_day_message(day)
                return await self._sync(channel, board)
            await self.store.save_day_message(
                DayMessage(day, message.message_id, _content(embed))
            )

        todo = render_todo(await self.store.list_todos("open"))
        if repost_from is None and board:
            try:
                await channel.get_partial_message(board.todo_message_id).edit(
                    embed=todo
                )
                return
            except discord.NotFound:
                pass  # deleted by someone; it's at the bottom, so just post it again

        stale: list[int] = []
        for day in sorted(
            d for d in days if repost_from is not None and d >= repost_from
        ):
            embed = render_day(day, days[day])
            sent = await channel.send(embed=embed)
            await self.store.save_day_message(DayMessage(day, sent.id, _content(embed)))
            if day in posted:
                stale.append(posted[day].message_id)
        sent = await channel.send(embed=todo)
        await self.store.set_board(BoardLocation(channel.id, sent.id))
        if board:
            stale.append(board.todo_message_id)
        await self._delete(channel.id, stale)

    async def _delete(self, channel_id: int, message_ids: list[int]) -> None:
        channel = self._channel(channel_id)
        for message_id in message_ids:
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.HTTPException:
                pass  # already gone, or no access to the old channel

    def _channel(self, channel_id: int) -> discord.PartialMessageable:
        return self.client.get_partial_messageable(channel_id)
