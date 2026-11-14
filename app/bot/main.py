import asyncio
import logging

from aiogram import Dispatcher

from app.bot.handlers import callbacks, links, start
from app.services.telegram import make_bot


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot = make_bot()
    dp = Dispatcher()
    dp.include_routers(start.router, callbacks.router, links.router)
    try:
        await bot.delete_webhook(drop_pending_updates=False, request_timeout=15)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
