import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Literal

import aiosqlite

Status = Literal["open", "done", "all"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    assignee_id INTEGER,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS board (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    done_message_id INTEGER  -- the old single Completed message, until replaced
);
CREATE TABLE IF NOT EXISTS day_messages (
    day TEXT PRIMARY KEY,
    message_id INTEGER NOT NULL,
    content TEXT NOT NULL
);
"""


class Unset(Enum):
    UNSET = auto()


UNSET = Unset.UNSET


@dataclass(frozen=True)
class Todo:
    id: int
    title: str
    done: bool
    assignee_id: int | None
    created_by: int | None
    created_at: datetime
    done_at: datetime | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_todo(row: aiosqlite.Row) -> Todo:
    return Todo(
        id=row["id"],
        title=row["title"],
        done=bool(row["done"]),
        assignee_id=row["assignee_id"],
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
    assignee_id: int | None = None
    done: bool = False
    created_by: int | None = None


@dataclass(frozen=True)
class Update:
    id: int
    title: str | Unset = UNSET
    done: bool | Unset = UNSET
    assignee_id: int | None | Unset = UNSET


@dataclass(frozen=True)
class Delete:
    id: int


Op = Create | Update | Delete


class TodoNotFound(Exception):
    def __init__(self, todo_id: int, index: int) -> None:
        super().__init__(f"Todo {todo_id} not found")
        self.todo_id = todo_id
        self.index = index  # position of the failing op in the batch


class TodoStore:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._listeners: list[Callable[[], None]] = []
        # One shared connection means one transaction: writers take turns so a
        # commit can never sweep up half of someone else's batch.
        self._write_lock = asyncio.Lock()

    def add_listener(self, listener: Callable[[], None]) -> None:
        """Call `listener` after every change to the todos, from any source."""
        self._listeners.append(listener)

    def _changed(self) -> None:
        for listener in self._listeners:
            listener()

    @classmethod
    async def open(cls, path: str) -> "TodoStore":
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(SCHEMA)
        async with db.execute("PRAGMA table_info(board)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        if "done_message_id" not in columns:
            await db.execute("ALTER TABLE board ADD COLUMN done_message_id INTEGER")
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    async def batch(self, ops: Sequence[Op]) -> list[Todo | None]:
        """Apply `ops` in order, all or nothing.

        Returns each op's todo as it ends up (None for deletes). Raises
        TodoNotFound, having applied nothing, if an op names a missing todo.
        """
        async with self._write_lock:
            try:
                results = [await self._apply(op, i) for i, op in enumerate(ops)]
            except BaseException:
                await self._db.rollback()
                raise
            await self._db.commit()
        if any(_writes(op) for op in ops):
            self._changed()
        return results

    async def _apply(self, op: Op, index: int) -> Todo | None:
        if isinstance(op, Create):
            cursor = await self._db.execute(
                "INSERT INTO todos (title, assignee_id, created_by, created_at, done,"
                " done_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    op.title,
                    op.assignee_id,
                    op.created_by,
                    _now(),
                    int(op.done),
                    _now() if op.done else None,
                ),
            )
            return await self.get(cursor.lastrowid or 0)
        if isinstance(op, Delete):
            cursor = await self._db.execute("DELETE FROM todos WHERE id = ?", (op.id,))
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
        if not isinstance(op.assignee_id, Unset):
            sets.append("assignee_id = ?")
            params.append(op.assignee_id)
        if sets:
            await self._db.execute(
                f"UPDATE todos SET {', '.join(sets)} WHERE id = ?", [*params, op.id]
            )
        todo = await self.get(op.id)
        if not todo:
            raise TodoNotFound(op.id, index)
        return todo

    async def add(
        self,
        title: str,
        assignee_id: int | None = None,
        created_by: int | None = None,
    ) -> Todo:
        [todo] = await self.batch([Create(title, assignee_id, created_by=created_by)])
        assert todo is not None
        return todo

    async def get(self, todo_id: int) -> Todo | None:
        async with self._db.execute(
            "SELECT * FROM todos WHERE id = ?", (todo_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_todo(row) if row else None

    async def list_todos(
        self, status: Status = "open", assignee_id: int | None = None
    ) -> list[Todo]:
        clauses: list[str] = []
        params: list[object] = []
        if status != "all":
            clauses.append("done = ?")
            params.append(1 if status == "done" else 0)
        if assignee_id is not None:
            clauses.append("assignee_id = ?")
            params.append(assignee_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._db.execute(
            f"SELECT * FROM todos {where} ORDER BY done, id", params
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_todo(row) for row in rows]

    async def search(
        self, query: str, status: Status = "all", limit: int = 25
    ) -> list[Todo]:
        """Match todos by id prefix or title substring, for autocomplete."""
        clauses = ["(CAST(id AS TEXT) LIKE ? OR title LIKE ?)"]
        params: list[object] = [f"{query.lstrip('#')}%", f"%{query}%"]
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
        assignee_id: int | None | Unset = UNSET,
    ) -> Todo | None:
        try:
            [todo] = await self.batch([Update(todo_id, title, done, assignee_id)])
        except TodoNotFound:
            return None
        return todo

    async def delete(self, todo_id: int) -> bool:
        try:
            await self.batch([Delete(todo_id)])
        except TodoNotFound:
            return False
        return True

    async def done_by_day(self) -> dict[date, list[Todo]]:
        """Completed todos grouped by UTC completion date, oldest first."""
        async with self._db.execute(
            "SELECT * FROM todos WHERE done = 1 ORDER BY done_at, id"
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
            "SELECT channel_id, message_id FROM board WHERE id = 1"
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
                "INSERT OR REPLACE INTO board"
                " (id, channel_id, message_id, done_message_id) VALUES (1, ?, ?, NULL)",
                (location.channel_id, location.todo_message_id),
            )
            await self._db.commit()

    async def pop_legacy_done_message(self) -> int | None:
        """The old single Completed message's id, if the board still has one."""
        async with self._write_lock:
            async with self._db.execute(
                "SELECT done_message_id FROM board WHERE id = 1"
            ) as cursor:
                row = await cursor.fetchone()
            await self._db.execute("UPDATE board SET done_message_id = NULL")
            await self._db.commit()
        return row["done_message_id"] if row else None

    # Per-day Completed messages. These writes are the board's own bookkeeping,
    # so they don't notify listeners.

    async def day_messages(self) -> dict[date, DayMessage]:
        async with self._db.execute("SELECT * FROM day_messages") as cursor:
            rows = await cursor.fetchall()
        messages = [
            DayMessage(date.fromisoformat(r["day"]), r["message_id"], r["content"])
            for r in rows
        ]
        return {m.day: m for m in messages}

    async def save_day_message(self, message: DayMessage) -> None:
        async with self._write_lock:
            await self._db.execute(
                "INSERT OR REPLACE INTO day_messages (day, message_id, content)"
                " VALUES (?, ?, ?)",
                (message.day.isoformat(), message.message_id, message.content),
            )
            await self._db.commit()

    async def delete_day_message(self, day: date) -> None:
        async with self._write_lock:
            await self._db.execute(
                "DELETE FROM day_messages WHERE day = ?", (day.isoformat(),)
            )
            await self._db.commit()

    async def forget_day_message(self, message_id: int) -> bool:
        """Forget a per-day message that was deleted; False if it isn't one."""
        async with self._write_lock:
            cursor = await self._db.execute(
                "DELETE FROM day_messages WHERE message_id = ?", (message_id,)
            )
            await self._db.commit()
        return cursor.rowcount > 0

    async def clear_day_messages(self) -> list[int]:
        """Forget every per-day message, returning their ids."""
        async with self._write_lock:
            async with self._db.execute("SELECT message_id FROM day_messages") as c:
                ids = [row["message_id"] for row in await c.fetchall()]
            await self._db.execute("DELETE FROM day_messages")
            await self._db.commit()
        return ids


def _writes(op: Op) -> bool:
    """False only for an update that sets nothing."""
    if isinstance(op, Update):
        return not (
            isinstance(op.title, Unset)
            and isinstance(op.done, Unset)
            and isinstance(op.assignee_id, Unset)
        )
    return True
