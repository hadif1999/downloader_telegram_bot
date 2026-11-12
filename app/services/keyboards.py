"""Inline keyboards. Callback data layout: "<action>|<platform>|<id>|<fmt>|<lang>"."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup

from app.models import MediaInfo

SEP = "|"


def cb(action: str, info_or_platform, media_id: str = "", fmt: str = "", lang: str = "") -> str:
    if isinstance(info_or_platform, MediaInfo):
        platform, media_id = info_or_platform.platform, info_or_platform.id
    else:
        platform = info_or_platform
    data = SEP.join([action, platform, media_id, fmt, lang])
    assert len(data.encode()) <= 64, data
    return data


def parse_cb(data: str) -> tuple[str, str, str, str, str]:
    parts = (data.split(SEP) + [""] * 5)[:5]
    return parts[0], parts[1], parts[2], parts[3], parts[4]


def _rows(buttons: list[Btn], per_row: int) -> list[list[Btn]]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def formats_kb(info: MediaInfo, max_bytes: int) -> InlineKeyboardMarkup:
    video = [
        Btn(text=f"📹 {f.label}", callback_data=cb("fmt", info, fmt=f.key))
        for f in info.formats
        if f.key != "mp3" and f.size is not None and f.size <= max_bytes
    ]
    rows = _rows(video, 3)
    if info.format("mp3"):
        rows.append([Btn(text="🔉 MP3", callback_data=cb("fmt", info, fmt="mp3"))])
    rows.append(
        [
            Btn(text="🖼", callback_data=cb("thumb", info)),
            Btn(text="📝", callback_data=cb("desc", info)),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def languages_kb(info: MediaInfo, fmt: str) -> InlineKeyboardMarkup:
    buttons = [Btn(text="🎬 Original", callback_data=cb("dl", info, fmt=fmt))]
    buttons += [
        Btn(text=t.label, callback_data=cb("dl", info, fmt=fmt, lang=t.lang))
        for t in info.audio_tracks
        if not t.original
    ]
    rows = _rows(buttons, 3)
    rows.append([Btn(text="◀️", callback_data=cb("back", info))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def result_kb(platform: str, media_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                Btn(
                    text="🎵 Download music from video",
                    callback_data=cb("music", platform, media_id, "mp3"),
                )
            ]
        ]
    )


def add_to_group_kb(bot_username: str) -> InlineKeyboardMarkup:
    url = f"https://t.me/{bot_username}?startgroup=true"
    return InlineKeyboardMarkup(inline_keyboard=[[Btn(text="Add to a group ⤴️", url=url)]])
