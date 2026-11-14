import asyncio
import logging

from aiogram import Dispatcher

from app.bot.errors import on_error
from app.bot.handlers import callbacks, links, start
from app.bot.membership import MembershipMiddleware
from app.config import get_settings
from app.logging_config import redact_logs
from app.services.telegram import make_bot


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    redact_logs()
    bot = make_bot()
    dp = Dispatcher()
    dp.errors.register(on_error)
    dp.include_routers(start.router, callbacks.router, links.router)
    try:
        membership = await MembershipMiddleware.configure(bot, get_settings())
        dp.message.outer_middleware(membership)
        dp.callback_query.outer_middleware(membership)
        await bot.delete_webhook(drop_pending_updates=False, request_timeout=15)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False)
    finally:
        try:
            await bot.session.close()
        except Exception:
            logging.getLogger(__name__).exception("bot session cleanup failed")


if __name__ == "__main__":
    asyncio.run(main())
