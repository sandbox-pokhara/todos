import secrets
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from todo_bot.directory import Member, MemberDirectory
from todo_bot.store import (
    UNSET,
    Create,
    Delete,
    Op,
    Status,
    Todo,
    TodoNotFound,
    TodoStore,
    Unset,
    Update,
)

# Discord IDs are exchanged as strings: they overflow JavaScript's safe integers.
UserId = Annotated[str, Field(pattern=r"^\d{1,20}$")]
Title = Annotated[str, Field(min_length=1, max_length=200)]


class TodoOut(BaseModel):
    id: int
    title: str
    done: bool
    assignee_id: str | None
    assignee_name: str | None
    created_by: str | None
    created_at: datetime
    done_at: datetime | None


class TodoCreate(BaseModel):
    title: Title
    assignee_id: UserId | None = None


class TodoPatch(BaseModel):
    """Only fields that are sent are changed; send assignee_id: null to unassign."""

    title: Title | None = None
    done: bool | None = None
    assignee_id: UserId | None = None


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


class MemberOut(BaseModel):
    id: str
    name: str
    username: str


def create_app(store: TodoStore, directory: MemberDirectory, api_key: str) -> FastAPI:
    bearer = HTTPBearer(auto_error=False)

    def require_key(
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if not creds or not secrets.compare_digest(creds.credentials, api_key):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")

    app = FastAPI(title="Todo Bot API")
    auth = [Depends(require_key)]

    def to_out(todo: Todo) -> TodoOut:
        assignee = directory.member(todo.assignee_id) if todo.assignee_id else None
        return TodoOut(
            id=todo.id,
            title=todo.title,
            done=todo.done,
            assignee_id=str(todo.assignee_id) if todo.assignee_id else None,
            assignee_name=assignee.name if assignee else None,
            created_by=str(todo.created_by) if todo.created_by else None,
            created_at=todo.created_at,
            done_at=todo.done_at,
        )

    def check_member(user_id: str) -> int:
        uid = int(user_id)
        if directory.ready() and directory.member(uid) is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"User {user_id} is not a member of the server",
            )
        return uid

    def to_update(todo_id: int, body: TodoPatch) -> Update:
        assignee: int | None | Unset = UNSET
        if "assignee_id" in body.model_fields_set:
            assignee = check_member(body.assignee_id) if body.assignee_id else None
        return Update(
            todo_id,
            title=body.title if body.title is not None else UNSET,
            done=body.done if body.done is not None else UNSET,
            assignee_id=assignee,
        )

    def to_op(op: CreateOp | UpdateOp | DeleteOp) -> Op:
        match op:
            case CreateOp():
                assignee = check_member(op.assignee_id) if op.assignee_id else None
                return Create(op.title, assignee_id=assignee, done=op.done)
            case UpdateOp():
                return to_update(op.id, op)
            case DeleteOp():
                return Delete(op.id)

    async def get_or_404(todo_id: int) -> Todo:
        todo = await store.get(todo_id)
        if not todo:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Todo {todo_id} not found")
        return todo

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True, "discord_ready": directory.ready()}

    @app.get("/todos", dependencies=auth)
    async def list_todos(
        status: Status = "open",
        assignee_id: Annotated[UserId | None, Query()] = None,
    ) -> list[TodoOut]:
        todos = await store.list_todos(
            status, assignee_id=int(assignee_id) if assignee_id else None
        )
        return [to_out(t) for t in todos]

    @app.post("/todos", dependencies=auth, status_code=status.HTTP_201_CREATED)
    async def create_todo(body: TodoCreate) -> TodoOut:
        assignee = check_member(body.assignee_id) if body.assignee_id else None
        return to_out(await store.add(body.title, assignee_id=assignee))

    @app.get("/todos/{todo_id}", dependencies=auth)
    async def get_todo(todo_id: int) -> TodoOut:
        return to_out(await get_or_404(todo_id))

    @app.patch("/todos/{todo_id}", dependencies=auth)
    async def update_todo(todo_id: int, body: TodoPatch) -> TodoOut:
        try:
            [updated] = await store.batch([to_update(todo_id, body)])
        except TodoNotFound:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"Todo {todo_id} not found"
            ) from None
        assert updated is not None
        return to_out(updated)

    @app.post("/todos/batch", dependencies=auth)
    async def batch(
        ops: Annotated[list[BatchOp], Body(min_length=1, max_length=MAX_BATCH)],
    ) -> list[TodoOut | None]:
        """Apply several changes at once, in order, all or nothing.

        Returns one result per op: the todo as it ends up, or null for a delete.
        """
        try:
            results = await store.batch([to_op(op) for op in ops])
        except TodoNotFound as e:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"Op {e.index}: todo {e.todo_id} not found; nothing was changed",
            ) from None
        return [to_out(t) if t else None for t in results]

    @app.delete(
        "/todos/{todo_id}", dependencies=auth, status_code=status.HTTP_204_NO_CONTENT
    )
    async def delete_todo(todo_id: int) -> None:
        await get_or_404(todo_id)
        await store.delete(todo_id)

    @app.get("/members", dependencies=auth)
    async def list_members() -> list[MemberOut]:
        if not directory.ready():
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Discord not connected yet"
            )
        members: list[Member] = sorted(
            directory.members(), key=lambda m: m.name.lower()
        )
        return [
            MemberOut(id=str(m.id), name=m.name, username=m.username) for m in members
        ]

    return app
