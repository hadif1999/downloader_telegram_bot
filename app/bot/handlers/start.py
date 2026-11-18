import asyncio

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot.errors import acknowledge_callback
from app.platforms import registry
from app.platforms.registry import PLATFORMS
from app.services import captions, inline, media
from app.services.jobs import request_download
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
async def start(message: Message, bot: Bot, command: CommandObject) -> None:
    payload = command.args or ""
    if message.from_user and message.chat.type == ChatType.PRIVATE:
        # Deep links from inline mode (see app.services.inline): open a link in the private
        # chat, or receive a file the inline card couldn't show.
        if payload.startswith("l_"):
            await open_link(message, payload[2:])
            return
        if payload.startswith("g_"):
            await resend(message, bot, payload[2:])
            return
    await send_welcome(message, bot)


async def open_link(message: Message, token: str) -> None:
    from app.bot.handlers.links import handle_links

    url = await asyncio.to_thread(inline.link_for_token, token)
    if not url:
        await message.answer(captions.EXPIRED)
        return
    await handle_links(message, url)


async def resend(message: Message, bot: Bot, rid: str) -> None:
    """The chosen inline result's file, sent here (from the file_id cache when it is there)."""
    choice = await asyncio.to_thread(inline.load_choice, rid)
    if choice is None or choice[3] != message.from_user.id:
        await message.answer(captions.EXPIRED)
        return
    platform_name, media_id, fmt, _ = choice
    try:
        registry.get(platform_name)
        info = await media.resolve_async(platform_name, media_id)
    except KeyError:
        info = None
    if info is None:
        await message.answer(captions.EXPIRED)
        return
    error = await request_download(
        bot, info, fmt, None, chat_id=message.chat.id, user_id=message.from_user.id
    )
    if error:
        await message.answer(error)


@router.message(Command("help"))
async def help_command(message: Message, bot: Bot) -> None:
    me = await bot.me()
    await message.answer(captions.help_guide(PLATFORMS, me.username), disable_web_page_preview=True)


@router.callback_query(F.data == HELP)
async def help_button(cq: CallbackQuery, bot: Bot) -> None:
    await acknowledge_callback(cq)
    if isinstance(cq.message, Message):
        me = await bot.me()
        await cq.message.answer(captions.help_guide(PLATFORMS, me.username), disable_web_page_preview=True)
