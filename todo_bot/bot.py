import asyncio
import logging
from typing import Literal

import discord
from discord import app_commands

from todo_bot.board import Board, format_todo, format_todos
from todo_bot.directory import Member
from todo_bot.store import Status, TodoStore

log = logging.getLogger(__name__)


async def _todo_choices(
    store: TodoStore, current: str, status: Status
) -> list[app_commands.Choice[int]]:
    todos = await store.search(current, status=status)
    return [
        app_commands.Choice(name=f"#{t.id} {t.title}"[:100], value=t.id) for t in todos
    ]


async def _not_found(interaction: discord.Interaction, todo_id: int) -> None:
    await interaction.response.send_message(
        f"Todo #{todo_id} not found.", ephemeral=True
    )


class TodoCommands(app_commands.Group):
    def __init__(self, store: TodoStore, board: Board) -> None:
        super().__init__(name="todo", description="Manage the server todo list")
        self.store = store
        self._board = board

    @app_commands.command(description="Post the todo board in this channel")
    async def board(self, interaction: discord.Interaction) -> None:
        if not interaction.permissions.manage_guild:
            await interaction.response.send_message(
                "Only members with Manage Server can move the board.", ephemeral=True
            )
            return
        if interaction.channel_id is None:
            await interaction.response.send_message(
                "The board can't be posted here.", ephemeral=True
            )
            return
        perms = interaction.app_permissions
        if not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                "I need **Send Messages** and **Embed Links** in this channel.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._board.move_to(interaction.channel_id)
        await interaction.followup.send(
            "The board lives here now. It updates itself whenever a todo changes."
        )

    @app_commands.command(description="Add a todo")
    @app_commands.describe(title="What needs doing", assignee="Who should do it")
    async def add(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 200],
        assignee: discord.Member | None = None,
    ) -> None:
        todo = await self.store.add(
            title,
            assignee_id=assignee.id if assignee else None,
            created_by=interaction.user.id,
        )
        await interaction.response.send_message(
            f"Added {format_todo(todo)}", ephemeral=True
        )

    @app_commands.command(name="list", description="Show the todo list")
    @app_commands.describe(status="Which todos to show", assignee="Only this person's")
    async def list_(
        self,
        interaction: discord.Interaction,
        status: Literal["open", "done", "all"] = "open",
        assignee: discord.Member | None = None,
    ) -> None:
        todos = await self.store.list_todos(
            status, assignee_id=assignee.id if assignee else None
        )
        title = f"Todos ({status})" + (
            f" for {assignee.display_name}" if assignee else ""
        )
        embed = discord.Embed(
            title=title,
            description=format_todos(todos) or "Nothing here 🎉",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Mark a todo as done")
    async def done(self, interaction: discord.Interaction, todo: int) -> None:
        updated = await self.store.update(todo, done=True)
        if not updated:
            return await _not_found(interaction, todo)
        await interaction.response.send_message(
            f"Done: {format_todo(updated)}", ephemeral=True
        )

    @done.autocomplete("todo")
    async def _done_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await _todo_choices(self.store, current, "open")

    @app_commands.command(description="Mark a done todo as open again")
    async def reopen(self, interaction: discord.Interaction, todo: int) -> None:
        updated = await self.store.update(todo, done=False)
        if not updated:
            return await _not_found(interaction, todo)
        await interaction.response.send_message(
            f"Reopened: {format_todo(updated)}", ephemeral=True
        )

    @reopen.autocomplete("todo")
    async def _reopen_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await _todo_choices(self.store, current, "done")

    @app_commands.command(description="Assign a todo (leave user empty to unassign)")
    async def assign(
        self,
        interaction: discord.Interaction,
        todo: int,
        user: discord.Member | None = None,
    ) -> None:
        updated = await self.store.update(todo, assignee_id=user.id if user else None)
        if not updated:
            return await _not_found(interaction, todo)
        verb = "Assigned" if user else "Unassigned"
        await interaction.response.send_message(
            f"{verb}: {format_todo(updated)}", ephemeral=True
        )

    @assign.autocomplete("todo")
    async def _assign_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await _todo_choices(self.store, current, "all")

    @app_commands.command(description="Rename a todo")
    async def edit(
        self,
        interaction: discord.Interaction,
        todo: int,
        title: app_commands.Range[str, 1, 200],
    ) -> None:
        updated = await self.store.update(todo, title=title)
        if not updated:
            return await _not_found(interaction, todo)
        await interaction.response.send_message(
            f"Edited: {format_todo(updated)}", ephemeral=True
        )

    @edit.autocomplete("todo")
    async def _edit_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await _todo_choices(self.store, current, "all")

    @app_commands.command(description="Delete a todo")
    async def delete(self, interaction: discord.Interaction, todo: int) -> None:
        existing = await self.store.get(todo)
        if not existing or not await self.store.delete(todo):
            return await _not_found(interaction, todo)
        await interaction.response.send_message(
            f"Deleted: {format_todo(existing)}", ephemeral=True
        )

    @delete.autocomplete("todo")
    async def _delete_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await _todo_choices(self.store, current, "all")


class TodoBot(discord.Client):
    def __init__(self, store: TodoStore, guild_id: int) -> None:
        intents = discord.Intents.default()
        intents.members = True  # needed to list members for the API
        super().__init__(
            intents=intents, allowed_mentions=discord.AllowedMentions.none()
        )
        self.store = store
        self.guild_id = guild_id
        self.tree = app_commands.CommandTree(self)
        self.board = Board(self, store)
        # Changes from the API go through the same store, so they update the board too.
        store.add_listener(self.board.request_refresh)
        self._board_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.guild_id)
        self.tree.add_command(TodoCommands(self.store, self.board), guild=guild)
        await self.tree.sync(guild=guild)
        self._board_task = asyncio.create_task(self.board.run())

    async def close(self) -> None:
        if self._board_task:
            self._board_task.cancel()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    # MemberDirectory

    def _guild(self) -> discord.Guild | None:
        return self.get_guild(self.guild_id)

    def ready(self) -> bool:
        return self.is_ready() and self._guild() is not None

    def members(self) -> list[Member]:
        guild = self._guild()
        if not guild:
            return []
        return [_to_member(m) for m in guild.members if not m.bot]

    def member(self, user_id: int) -> Member | None:
        guild = self._guild()
        found = guild.get_member(user_id) if guild else None
        return _to_member(found) if found else None


def _to_member(member: discord.Member) -> Member:
    return Member(id=member.id, name=member.display_name, username=member.name)
