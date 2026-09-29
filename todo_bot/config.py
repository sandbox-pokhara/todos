import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    discord_token: str
    guild_id: int
    api_key: str
    database_path: str
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        missing = [
            name
            for name in ("DISCORD_TOKEN", "GUILD_ID", "API_KEY")
            if not os.environ.get(name)
        ]
        if missing:
            raise SystemExit(f"Missing required env vars: {', '.join(missing)}")
        return cls(
            discord_token=os.environ["DISCORD_TOKEN"],
            guild_id=int(os.environ["GUILD_ID"]),
            api_key=os.environ["API_KEY"],
            database_path=os.environ.get("DATABASE_PATH", "todos.db"),
            port=int(os.environ.get("PORT", "8000")),
        )
