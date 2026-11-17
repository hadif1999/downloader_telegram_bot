from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot.errors import acknowledge_callback
from app.platforms.registry import PLATFORMS
from app.services import captions
from app.services.keyboards import HELP, add_to_group_kb

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
async def start(message: Message, bot: Bot) -> None:
    await send_welcome(message, bot)


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(captions.help_guide(PLATFORMS), disable_web_page_preview=True)


@router.callback_query(F.data == HELP)
async def help_button(cq: CallbackQuery) -> None:
    await acknowledge_callback(cq)
    if isinstance(cq.message, Message):
        await cq.message.answer(captions.help_guide(PLATFORMS), disable_web_page_preview=True)
