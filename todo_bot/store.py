from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
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
    message_id INTEGER NOT NULL
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
    message_id: int


class TodoStore:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._listeners: list[Callable[[], None]] = []

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
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    async def add(
        self, title: str, assignee_id: int | None = None, created_by: int | None = None
    ) -> Todo:
        cursor = await self._db.execute(
            "INSERT INTO todos (title, assignee_id, created_by, created_at)"
            " VALUES (?, ?, ?, ?)",
            (title, assignee_id, created_by, _now()),
        )
        await self._db.commit()
        self._changed()
        todo = await self.get(cursor.lastrowid or 0)
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
        sets: list[str] = []
        params: list[object] = []
        if not isinstance(title, Unset):
            sets.append("title = ?")
            params.append(title)
        if not isinstance(done, Unset):
            sets += ["done = ?", "done_at = ?"]
            params += [int(done), _now() if done else None]
        if not isinstance(assignee_id, Unset):
            sets.append("assignee_id = ?")
            params.append(assignee_id)
        if sets:
            await self._db.execute(
                f"UPDATE todos SET {', '.join(sets)} WHERE id = ?", [*params, todo_id]
            )
            await self._db.commit()
            self._changed()
        return await self.get(todo_id)

    async def delete(self, todo_id: int) -> bool:
        cursor = await self._db.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
        await self._db.commit()
        if cursor.rowcount > 0:
            self._changed()
            return True
        return False

    async def get_board(self) -> BoardLocation | None:
        async with self._db.execute(
            "SELECT channel_id, message_id FROM board WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        return BoardLocation(row["channel_id"], row["message_id"]) if row else None

    async def set_board(self, location: BoardLocation) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO board (id, channel_id, message_id) VALUES (1, ?, ?)",
            (location.channel_id, location.message_id),
        )
        await self._db.commit()
