import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Literal

import aiosqlite

Status = Literal["open", "done", "all"]
BoardLayout = Literal["list", "project"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,  -- the Discord server the todo belongs to
    title TEXT NOT NULL,
    project TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    -- Databases from before assignees were removed keep an unused assignee_id.
    created_by INTEGER,
    created_at TEXT NOT NULL,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS boards (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    done_message_id INTEGER,  -- the old single Completed message, until replaced
    layout TEXT NOT NULL DEFAULT 'list'
);
CREATE TABLE IF NOT EXISTS guild_day_messages (
    guild_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (guild_id, day)
);
"""
INDEXES = "CREATE INDEX IF NOT EXISTS todos_guild ON todos (guild_id, done, id);"


class Unset(Enum):
    UNSET = auto()


UNSET = Unset.UNSET

# Before the project column, titles carried it: "tibia-engine: Fix login".
_TITLE_PROJECT = re.compile(r"([a-z][\w.-]*):\s*(.+)", re.IGNORECASE)


def normalize_project(project: str | None) -> str | None:
    """Projects are compared lowercased; blank means no project."""
    return (project or "").strip().lower() or None


@dataclass(frozen=True)
class Todo:
    id: int
    title: str
    project: str | None
    done: bool
    created_by: int | None
    created_at: datetime
    done_at: datetime | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_todo(row: aiosqlite.Row) -> Todo:
    return Todo(
        id=row["id"],
        title=row["title"],
        project=row["project"],
        done=bool(row["done"]),
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        done_at=datetime.fromisoformat(row["done_at"]) if row["done_at"] else None,
    )


@dataclass(frozen=True)
class BoardLocation:
    channel_id: int
    todo_message_id: int


@dataclass(frozen=True)
class DayMessage:
    """A message in the board channel listing one day's completed todos."""

    day: date
    message_id: int
    content: str  # as last posted, to skip edits that change nothing


@dataclass(frozen=True)
class Create:
    title: str
    done: bool = False
    created_by: int | None = None
    project: str | None = None


@dataclass(frozen=True)
class Update:
    id: int
    title: str | Unset = UNSET
    done: bool | Unset = UNSET
    project: str | None | Unset = UNSET


@dataclass(frozen=True)
class Delete:
    id: int


Op = Create | Update | Delete


class TodoNotFound(Exception):
    def __init__(self, todo_id: int, index: int) -> None:
        super().__init__(f"Todo {todo_id} not found")
        self.todo_id = todo_id
        self.index = index  # position of the failing op in the batch


class GuildNotSet(Exception):
    """The database has todos from before servers were tracked, and no server
    was named to give them to."""


class TodoStore:
    """The database connection, shared by every server.

    Todos and boards are only reachable through `guild()`, which scopes every
    query to one server, so one server can never read or change another's.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._listeners: list[Callable[[int], None]] = []
        # One shared connection means one transaction: writers take turns so a
        # commit can never sweep up half of someone else's batch.
        self._write_lock = asyncio.Lock()

    def add_listener(self, listener: Callable[[int], None]) -> None:
        """Call `listener(guild_id)` after every change to a server's todos,
        from any source."""
        self._listeners.append(listener)

    def _changed(self, guild_id: int) -> None:
        for listener in self._listeners:
            listener(guild_id)

    def guild(self, guild_id: int) -> "GuildStore":
        return GuildStore(self, guild_id)

    @classmethod
    async def open(cls, path: str, legacy_guild_id: int | None = None) -> "TodoStore":
        """Open the database, creating or upgrading it.

        `legacy_guild_id` is the server that todos and the board from before
        servers were tracked belong to. Only such a database needs it.
        """
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await _migrate(db, legacy_guild_id)
        except BaseException:
            await db.close()
            raise
        return cls(db)

    async def close(self) -> None:
        await self._db.close()


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        return {row["name"] for row in await cursor.fetchall()}


async def _migrate(db: aiosqlite.Connection, legacy_guild_id: int | None) -> None:
    todo_columns = await _columns(db, "todos")
    old_board = await _columns(db, "board")
    old_days = await _columns(db, "day_messages")

    # A database from before servers were tracked: it all belongs to one server.
    unscoped = bool(todo_columns) and "guild_id" not in todo_columns
    if (unscoped or old_board or old_days) and legacy_guild_id is None:
        raise GuildNotSet(
            "The database has todos from before servers were tracked;"
            " name the server they belong to"
        )
    if unscoped:
        await db.execute("ALTER TABLE todos ADD COLUMN guild_id INTEGER")
        await db.execute("UPDATE todos SET guild_id = ?", (legacy_guild_id,))

    await db.executescript(SCHEMA)

    if old_board:
        if "done_message_id" not in old_board:
            await db.execute("ALTER TABLE board ADD COLUMN done_message_id INTEGER")
        if "layout" not in old_board:
            await db.execute(
                "ALTER TABLE board ADD COLUMN layout TEXT NOT NULL DEFAULT 'list'"
            )
        await db.execute(
            "INSERT OR IGNORE INTO boards"
            " (guild_id, channel_id, message_id, done_message_id, layout)"
            " SELECT ?, channel_id, message_id, done_message_id, layout FROM board",
            (legacy_guild_id,),
        )
        await db.execute("DROP TABLE board")
    if old_days:
        await db.execute(
            "INSERT OR IGNORE INTO guild_day_messages"
            " (guild_id, day, message_id, content)"
            " SELECT ?, day, message_id, content FROM day_messages",
            (legacy_guild_id,),
        )
        await db.execute("DROP TABLE day_messages")

    if "project" not in await _columns(db, "todos"):
        await db.execute("ALTER TABLE todos ADD COLUMN project TEXT")
        async with db.execute("SELECT id, title FROM todos") as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            if match := _TITLE_PROJECT.fullmatch(row["title"]):
                await db.execute(
                    "UPDATE todos SET project = ?, title = ? WHERE id = ?",
                    (normalize_project(match[1]), match[2], row["id"]),
                )
    await db.executescript(INDEXES)
    await db.commit()


class GuildStore:
    """One server's todos and board.

    Every query is scoped to `guild_id`: another server's todo is reported as
    not found, exactly like one that doesn't exist.
    """

    def __init__(self, store: TodoStore, guild_id: int) -> None:
        self._store = store
        self._db = store._db  # pyright: ignore[reportPrivateUsage]
        self._write_lock = store._write_lock  # pyright: ignore[reportPrivateUsage]
        self.guild_id = guild_id

    async def batch(self, ops: Sequence[Op]) -> list[Todo | None]:
        """Apply `ops` in order, all or nothing.

        Returns each op's todo as it ends up (None for deletes). Raises
        TodoNotFound, having applied nothing, if an op names a todo this server
        doesn't have.
        """
        async with self._write_lock:
            try:
                results = [await self._apply(op, i) for i, op in enumerate(ops)]
            except BaseException:
                await self._db.rollback()
                raise
            await self._db.commit()
        if any(_writes(op) for op in ops):
            self._store._changed(self.guild_id)  # pyright: ignore[reportPrivateUsage]
        return results

    async def _apply(self, op: Op, index: int) -> Todo | None:
        if isinstance(op, Create):
            cursor = await self._db.execute(
                "INSERT INTO todos (guild_id, title, project, created_by, created_at,"
                " done, done_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self.guild_id,
                    op.title,
                    normalize_project(op.project),
                    op.created_by,
                    _now(),
                    int(op.done),
                    _now() if op.done else None,
                ),
            )
            return await self.get(cursor.lastrowid or 0)
        if isinstance(op, Delete):
            cursor = await self._db.execute(
                "DELETE FROM todos WHERE id = ? AND guild_id = ?",
                (op.id, self.guild_id),
            )
            if cursor.rowcount == 0:
                raise TodoNotFound(op.id, index)
            return None
        sets: list[str] = []
        params: list[object] = []
        if not isinstance(op.title, Unset):
            sets.append("title = ?")
            params.append(op.title)
        if not isinstance(op.done, Unset):
            sets += ["done = ?", "done_at = ?"]
            params += [int(op.done), _now() if op.done else None]
        if not isinstance(op.project, Unset):
            sets.append("project = ?")
            params.append(normalize_project(op.project))
        if sets:
            await self._db.execute(
                f"UPDATE todos SET {', '.join(sets)} WHERE id = ? AND guild_id = ?",
                [*params, op.id, self.guild_id],
            )
        todo = await self.get(op.id)
        if not todo:
            raise TodoNotFound(op.id, index)
        return todo

    async def add(
        self,
        title: str,
        created_by: int | None = None,
        project: str | None = None,
    ) -> Todo:
        [todo] = await self.batch(
            [Create(title, created_by=created_by, project=project)]
        )
        assert todo is not None
        return todo

    async def get(self, todo_id: int) -> Todo | None:
        async with self._db.execute(
            "SELECT * FROM todos WHERE id = ? AND guild_id = ?",
            (todo_id, self.guild_id),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_todo(row) if row else None

    async def list_todos(
        self,
        status: Status = "open",
        project: str | None = None,
    ) -> list[Todo]:
        clauses = ["guild_id = ?"]
        params: list[object] = [self.guild_id]
        if status != "all":
            clauses.append("done = ?")
            params.append(1 if status == "done" else 0)
        if project := normalize_project(project):
            clauses.append("project = ?")
            params.append(project)
        async with self._db.execute(
            f"SELECT * FROM todos WHERE {' AND '.join(clauses)} ORDER BY done, id",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_todo(row) for row in rows]

    async def search(
        self, query: str, status: Status = "all", limit: int = 25
    ) -> list[Todo]:
        """Match todos by id prefix, or title or project substring, for autocomplete."""
        clauses = [
            "guild_id = ?",
            "(CAST(id AS TEXT) LIKE ? OR title LIKE ? OR project LIKE ?)",
        ]
        params: list[object] = [
            self.guild_id,
            f"{query.lstrip('#')}%",
            f"%{query}%",
            f"%{query}%",
        ]
        if status != "all":
            clauses.append("done = ?")
            params.append(1 if status == "done" else 0)
        params.append(limit)
        async with self._db.execute(
            f"SELECT * FROM todos WHERE {' AND '.join(clauses)}"
            " ORDER BY done, id DESC LIMIT ?",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_todo(row) for row in rows]

    async def update(
        self,
        todo_id: int,
        *,
        title: str | Unset = UNSET,
        done: bool | Unset = UNSET,
        project: str | None | Unset = UNSET,
    ) -> Todo | None:
        try:
            [todo] = await self.batch([Update(todo_id, title, done, project)])
        except TodoNotFound:
            return None
        return todo

    async def delete(self, todo_id: int) -> bool:
        try:
            await self.batch([Delete(todo_id)])
        except TodoNotFound:
            return False
        return True

    async def projects(self) -> list[str]:
        """Every project in use, by name, for autocomplete."""
        async with self._db.execute(
            "SELECT DISTINCT project FROM todos"
            " WHERE guild_id = ? AND project IS NOT NULL ORDER BY project",
            (self.guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [row["project"] for row in rows]

    async def done_by_day(self) -> dict[date, list[Todo]]:
        """Completed todos grouped by UTC completion date, oldest first."""
        async with self._db.execute(
            "SELECT * FROM todos WHERE guild_id = ? AND done = 1 ORDER BY done_at, id",
            (self.guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        days: dict[date, list[Todo]] = {}
        for row in rows:
            todo = _row_to_todo(row)
            assert todo.done_at is not None
            days.setdefault(todo.done_at.date(), []).append(todo)
        return days

    async def get_board(self) -> BoardLocation | None:
        async with self._db.execute(
            "SELECT channel_id, message_id FROM boards WHERE guild_id = ?",
            (self.guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return BoardLocation(
            channel_id=row["channel_id"], todo_message_id=row["message_id"]
        )

    async def set_board(self, location: BoardLocation) -> None:
        async with self._write_lock:
            await self._db.execute(
                "INSERT INTO boards (guild_id, channel_id, message_id) VALUES (?, ?, ?)"
                " ON CONFLICT (guild_id) DO UPDATE SET"
                " channel_id = excluded.channel_id,"
                " message_id = excluded.message_id, done_message_id = NULL",
                (self.guild_id, location.channel_id, location.todo_message_id),
            )
            await self._db.commit()

    async def board_layout(self) -> BoardLayout:
        async with self._db.execute(
            "SELECT layout FROM boards WHERE guild_id = ?", (self.guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return "project" if row and row["layout"] == "project" else "list"

    async def set_board_layout(self, layout: BoardLayout) -> None:
        async with self._write_lock:
            await self._db.execute(
                "UPDATE boards SET layout = ? WHERE guild_id = ?",
                (layout, self.guild_id),
            )
            await self._db.commit()

    async def pop_legacy_done_message(self) -> int | None:
        """The old single Completed message's id, if the board still has one."""
        async with self._write_lock:
            async with self._db.execute(
                "SELECT done_message_id FROM boards WHERE guild_id = ?",
                (self.guild_id,),
            ) as cursor:
                row = await cursor.fetchone()
            await self._db.execute(
                "UPDATE boards SET done_message_id = NULL WHERE guild_id = ?",
                (self.guild_id,),
            )
            await self._db.commit()
        return row["done_message_id"] if row else None

    # Per-day Completed messages. These writes are the board's own bookkeeping,
    # so they don't notify listeners.

    async def day_messages(self) -> dict[date, DayMessage]:
        async with self._db.execute(
            "SELECT * FROM guild_day_messages WHERE guild_id = ?", (self.guild_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        messages = [
            DayMessage(date.fromisoformat(r["day"]), r["message_id"], r["content"])
            for r in rows
        ]
        return {m.day: m for m in messages}

    async def save_day_message(self, message: DayMessage) -> None:
        async with self._write_lock:
            await self._db.execute(
                "INSERT OR REPLACE INTO guild_day_messages"
                " (guild_id, day, message_id, content) VALUES (?, ?, ?, ?)",
                (
                    self.guild_id,
                    message.day.isoformat(),
                    message.message_id,
                    message.content,
                ),
            )
            await self._db.commit()

    async def delete_day_message(self, day: date) -> None:
        async with self._write_lock:
            await self._db.execute(
                "DELETE FROM guild_day_messages WHERE guild_id = ? AND day = ?",
                (self.guild_id, day.isoformat()),
            )
            await self._db.commit()

    async def forget_day_message(self, message_id: int) -> bool:
        """Forget a per-day message that was deleted; False if it isn't one."""
        async with self._write_lock:
            cursor = await self._db.execute(
                "DELETE FROM guild_day_messages WHERE guild_id = ? AND message_id = ?",
                (self.guild_id, message_id),
            )
            await self._db.commit()
        return cursor.rowcount > 0

    async def clear_day_messages(self) -> list[int]:
        """Forget every per-day message, returning their ids."""
        async with self._write_lock:
            async with self._db.execute(
                "SELECT message_id FROM guild_day_messages WHERE guild_id = ?",
                (self.guild_id,),
            ) as c:
                ids = [row["message_id"] for row in await c.fetchall()]
            await self._db.execute(
                "DELETE FROM guild_day_messages WHERE guild_id = ?", (self.guild_id,)
            )
            await self._db.commit()
        return ids


def _writes(op: Op) -> bool:
    """False only for an update that sets nothing."""
    if isinstance(op, Update):
        return not (
            isinstance(op.title, Unset)
            and isinstance(op.done, Unset)
            and isinstance(op.project, Unset)
        )
    return True
