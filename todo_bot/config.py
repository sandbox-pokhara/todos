import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Guild:
    """A Discord server the bot serves, and the API key that reaches its todos."""

    id: int
    api_key: str


@dataclass(frozen=True)
class Settings:
    discord_token: str
    # The first server also gets any todos from before servers were tracked.
    guilds: tuple[Guild, ...]
    database_path: str
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        if not os.environ.get("DISCORD_TOKEN"):
            raise SystemExit("Missing required env var: DISCORD_TOKEN")
        try:
            guilds = parse_guilds(
                os.environ.get("GUILDS", ""),
                os.environ.get("GUILD_ID", ""),
                os.environ.get("API_KEY", ""),
            )
        except ValueError as e:
            raise SystemExit(str(e)) from None
        return cls(
            discord_token=os.environ["DISCORD_TOKEN"],
            guilds=guilds,
            database_path=os.environ.get("DATABASE_PATH", "todos.db"),
            port=int(os.environ.get("PORT", "8000")),
        )


def parse_guilds(
    guilds: str, guild_id: str = "", api_key: str = ""
) -> tuple[Guild, ...]:
    """Servers from `GUILDS` ("<server id>:<api key>,..."), after the single server
    that `GUILD_ID` and `API_KEY` name, if they're set."""
    entries: list[tuple[str, str]] = []
    if guild_id or api_key:
        if not (guild_id and api_key):
            raise ValueError("GUILD_ID and API_KEY must be set together")
        entries.append((guild_id, api_key))
    # Errors name entries by position, never by content: it may hold a key.
    for i, entry in enumerate(filter(None, (e.strip() for e in guilds.split(",")))):
        server, sep, key = entry.partition(":")
        if not sep:
            raise ValueError(f"GUILDS entry {i + 1} isn't <server id>:<api key>")
        entries.append((server.strip(), key.strip()))
    if not entries:
        raise ValueError("Set GUILDS, or GUILD_ID and API_KEY")

    result: list[Guild] = []
    for server, key in entries:
        if not server.isdigit():
            raise ValueError("A server id in GUILDS or GUILD_ID isn't a number")
        if len(key) < 16:
            raise ValueError(f"The API key for server {server} is under 16 characters")
        if any(g.id == int(server) for g in result):
            raise ValueError(f"Server {server} is listed twice")
        # A key must lead to exactly one server's todos.
        if any(g.api_key == key for g in result):
            raise ValueError(f"Server {server} reuses another server's API key")
        result.append(Guild(int(server), key))
    return tuple(result)
