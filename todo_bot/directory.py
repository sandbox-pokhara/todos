from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Member:
    id: int
    name: str
    username: str


class MemberDirectory(Protocol):
    """Server member lookup, implemented by the bot and faked in tests."""

    def ready(self) -> bool: ...

    def members(self) -> list[Member]: ...

    def member(self, user_id: int) -> Member | None: ...
