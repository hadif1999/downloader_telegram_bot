import asyncio
import logging

from aiogram import Dispatcher

from app.bot import activity, admin, language
from app.bot.errors import on_error
from app.bot.handlers import batch, callbacks, links, start
from app.bot.membership import MembershipMiddleware
from app.config import get_settings
from app.logging_config import redact_logs
from app.services import health
from app.services.telegram import make_bot


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    redact_logs()
    bot = make_bot()
    dp = Dispatcher()
    dp.errors.register(on_error)
    # admin first: its text-input states must win over link detection (admins only, see IsAdmin)
    dp.include_routers(
        admin.router,
        activity.router,
        language.router,
        start.router,
        batch.router,
        callbacks.router,
        links.router,
    )
    languages = language.LanguageMiddleware()
    dp.message.outer_middleware(languages)
    dp.callback_query.outer_middleware(languages)
    # statistics, bans and maintenance mode, before the channel gate (which costs API calls)
    usage = activity.ActivityMiddleware()
    dp.message.outer_middleware(usage)
    dp.callback_query.outer_middleware(usage)
    health.start_heartbeat("bot")
    try:
        membership = await MembershipMiddleware.configure(bot, get_settings())
        dp.message.outer_middleware(membership)
        dp.callback_query.outer_middleware(membership)
        await bot.delete_webhook(drop_pending_updates=False, request_timeout=15)
        await language.publish_commands(bot)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False)
    finally:
        try:
            await bot.session.close()
        except Exception:
            logging.getLogger(__name__).exception("bot session cleanup failed")


if __name__ == "__main__":
    asyncio.run(main())
