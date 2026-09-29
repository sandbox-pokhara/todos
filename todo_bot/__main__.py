import asyncio
import logging

import uvicorn

from todo_bot.api import create_app
from todo_bot.bot import TodoBot
from todo_bot.config import Settings
from todo_bot.store import TodoStore


async def main() -> None:
    settings = Settings.from_env()
    store = await TodoStore.open(settings.database_path)
    bot = TodoBot(store, settings.guild_id)
    app = create_app(store, bot, settings.api_key)
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=settings.port, log_config=None)
    )

    try:
        async with bot:
            # uvicorn handles SIGINT/SIGTERM; stop everything when either side exits.
            tasks = {
                asyncio.create_task(bot.start(settings.discord_token)),
                asyncio.create_task(server.serve()),
            }
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            server.should_exit = True
            await bot.close()
            await asyncio.gather(*tasks, return_exceptions=True)
            for task in done:
                task.result()  # re-raise a crash, e.g. a bad Discord token
    finally:
        await store.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(main())
