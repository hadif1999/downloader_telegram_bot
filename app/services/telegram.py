from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from app.config import get_settings


def make_bot() -> Bot:
    s = get_settings()
    session = None
    if s.bot_api_url:
        server = TelegramAPIServer.from_base(s.bot_api_url, is_local=s.bot_api_local)
        session = AiohttpSession(api=server, timeout=60 * 30)  # large uploads take a while
    return Bot(
        s.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
