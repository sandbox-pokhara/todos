import secrets
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from todo_bot.config import Guild
from todo_bot.store import (
    UNSET,
    Create,
    Delete,
    GuildStore,
    Op,
    Status,
    Todo,
    TodoNotFound,
    TodoStore,
    Update,
)

Title = Annotated[str, Field(min_length=1, max_length=200)]
# Lowercased on save; empty or null means no project.
Project = Annotated[str, Field(max_length=50)]


class TodoOut(BaseModel):
    id: int
    title: str
    project: str | None
    done: bool
    # Discord IDs are sent as strings: they overflow JavaScript's safe integers.
    created_by: str | None
    created_at: datetime
    done_at: datetime | None


class TodoCreate(BaseModel):
    title: Title
    project: Project | None = None


class TodoPatch(BaseModel):
    """Only fields that are sent are changed; send project: null to clear it."""

    title: Title | None = None
    project: Project | None = None
    done: bool | None = None


class CreateOp(TodoCreate):
    op: Literal["create"]
    done: bool = False


class UpdateOp(TodoPatch):
    op: Literal["update"]
    id: int


class DeleteOp(BaseModel):
    op: Literal["delete"]
    id: int


BatchOp = Annotated[CreateOp | UpdateOp | DeleteOp, Field(discriminator="op")]
MAX_BATCH = 200


class ServerOut(BaseModel):
    id: str
    name: str | None  # None until the bot has connected to the server


def create_app(
    store: TodoStore,
    guilds: Sequence[Guild],
    discord_ready: Callable[[], bool],
    guild_name: Callable[[int], str | None] = lambda _: None,
) -> FastAPI:
    """The API. Each key reaches exactly one server's todos: every endpoint
    works on the server whose key was sent, and sees nothing of the others."""
    bearer = HTTPBearer(auto_error=False)

    def server_todos(
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> GuildStore:
        sent = creds.credentials.encode() if creds else b""
        # Compare against every key, so the time taken doesn't hint at which matched.
        matches = [
            g for g in guilds if secrets.compare_digest(sent, g.api_key.encode())
        ]
        if not creds or not matches:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
        return store.guild(matches[0].id)

    app = FastAPI(title="Todo Bot API")

    def to_out(todo: Todo) -> TodoOut:
        return TodoOut(
            id=todo.id,
            title=todo.title,
            project=todo.project,
            done=todo.done,
            created_by=str(todo.created_by) if todo.created_by else None,
            created_at=todo.created_at,
            done_at=todo.done_at,
        )

    def to_update(todo_id: int, body: TodoPatch) -> Update:
        return Update(
            todo_id,
            title=body.title if body.title is not None else UNSET,
            done=body.done if body.done is not None else UNSET,
            project=body.project if "project" in body.model_fields_set else UNSET,
        )

    def to_op(op: CreateOp | UpdateOp | DeleteOp) -> Op:
        match op:
            case CreateOp():
                return Create(op.title, done=op.done, project=op.project)
            case UpdateOp():
                return to_update(op.id, op)
            case DeleteOp():
                return Delete(op.id)

    async def get_or_404(todos: GuildStore, todo_id: int) -> Todo:
        todo = await todos.get(todo_id)
        if not todo:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Todo {todo_id} not found")
        return todo

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True, "discord_ready": discord_ready()}

    @app.get("/server")
    async def server(todos: Annotated[GuildStore, Depends(server_todos)]) -> ServerOut:
        """The Discord server this key's todos belong to."""
        return ServerOut(id=str(todos.guild_id), name=guild_name(todos.guild_id))

    @app.get("/todos")
    async def list_todos(
        todos: Annotated[GuildStore, Depends(server_todos)],
        status: Status = "open",
        project: str | None = None,
    ) -> list[TodoOut]:
        return [to_out(t) for t in await todos.list_todos(status, project=project)]

    @app.post("/todos", status_code=status.HTTP_201_CREATED)
    async def create_todo(
        todos: Annotated[GuildStore, Depends(server_todos)], body: TodoCreate
    ) -> TodoOut:
        return to_out(await todos.add(body.title, project=body.project))

    @app.get("/todos/{todo_id}")
    async def get_todo(
        todos: Annotated[GuildStore, Depends(server_todos)], todo_id: int
    ) -> TodoOut:
        return to_out(await get_or_404(todos, todo_id))

    @app.patch("/todos/{todo_id}")
    async def update_todo(
        todos: Annotated[GuildStore, Depends(server_todos)],
        todo_id: int,
        body: TodoPatch,
    ) -> TodoOut:
        try:
            [updated] = await todos.batch([to_update(todo_id, body)])
        except TodoNotFound:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"Todo {todo_id} not found"
            ) from None
        assert updated is not None
        return to_out(updated)

    @app.post("/todos/batch")
    async def batch(
        todos: Annotated[GuildStore, Depends(server_todos)],
        ops: Annotated[list[BatchOp], Body(min_length=1, max_length=MAX_BATCH)],
    ) -> list[TodoOut | None]:
        """Apply several changes at once, in order, all or nothing.

        Returns one result per op: the todo as it ends up, or null for a delete.
        """
        try:
            results = await todos.batch([to_op(op) for op in ops])
        except TodoNotFound as e:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"Op {e.index}: todo {e.todo_id} not found; nothing was changed",
            ) from None
        return [to_out(t) if t else None for t in results]

    @app.delete("/todos/{todo_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_todo(
        todos: Annotated[GuildStore, Depends(server_todos)], todo_id: int
    ) -> None:
        if not await todos.delete(todo_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Todo {todo_id} not found")

    @app.get("/projects")
    async def list_projects(
        todos: Annotated[GuildStore, Depends(server_todos)],
    ) -> list[str]:
        """Every project some todo has, by name."""
        return await todos.projects()

    return app
