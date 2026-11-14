from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from app.config import get_settings


def make_bot(*, for_upload: bool = False) -> Bot:
    s = get_settings()
    options = {"timeout": min(60 * 30, s.task_time_limit - 60) if for_upload else 30}
    if s.bot_api_url:
        server = TelegramAPIServer.from_base(s.bot_api_url, is_local=s.bot_api_local)
        options["api"] = server
    return Bot(
        s.bot_token,
        session=AiohttpSession(**options),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
