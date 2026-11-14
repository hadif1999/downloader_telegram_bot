from aiogram import Bot, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.platforms.registry import PLATFORMS
from app.services import captions
from app.services.keyboards import add_to_group_kb

router = Router(name="start")


async def send_welcome(message: Message, bot: Bot, *, edit: bool = False) -> None:
    me = await bot.me()
    text = captions.welcome(PLATFORMS, me.username)
    markup = add_to_group_kb(me.username)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message, bot: Bot) -> None:
    await send_welcome(message, bot)
