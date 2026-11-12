from aiogram import Bot, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.platforms.registry import PLATFORMS
from app.services import captions
from app.services.keyboards import add_to_group_kb

router = Router(name="start")


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message, bot: Bot) -> None:
    me = await bot.me()
    await message.answer(captions.welcome(PLATFORMS, me.username), reply_markup=add_to_group_kb(me.username))
