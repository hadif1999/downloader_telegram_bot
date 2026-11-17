"""Inline keyboards. Callback data layout: "<action>|<platform>|<id>|<fmt>|<lang>"."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup

from app.i18n import LANGUAGES, label, t
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
    rows: list[list[Btn]] = []
    if info.kind == "gallery":
        rows.append(
            [Btn(text=t("📥 Download ({n})", n=len(info.items)), callback_data=cb("fmt", info, fmt="all"))]
        )
    else:
        video = [
            Btn(text=f"📹 {f.label}", callback_data=cb("fmt", info, fmt=f.key))
            for f in info.formats
            if f.key != "mp3" and (f.size is None or f.size <= max_bytes)
        ]
        rows += _rows(video, 3)
        if info.format("mp3"):
            rows.append([Btn(text="🔉 MP3", callback_data=cb("fmt", info, fmt="mp3"))])
    extras = []
    if info.thumbnail:
        extras.append(Btn(text="🖼", callback_data=cb("thumb", info)))
    if info.description:
        extras.append(Btn(text="📝", callback_data=cb("desc", info)))
    if extras:
        rows.append(extras)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def languages_kb(info: MediaInfo, fmt: str) -> InlineKeyboardMarkup:
    buttons = [Btn(text=t("🎬 Original"), callback_data=cb("dl", info, fmt=fmt))]
    buttons += [
        Btn(text=t.label, callback_data=cb("dl", info, fmt=fmt, lang=t.lang))
        for t in info.audio_tracks
        if not t.original
    ]
    rows = _rows(buttons, 3)
    rows.append([Btn(text="◀️", callback_data=cb("back", info))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


CANCEL = "stop"


def cancel_kb(request_id: str) -> InlineKeyboardMarkup:
    """Shown on the status card while a download is queued or running."""
    data = f"{CANCEL}{SEP}{request_id}"
    assert len(data.encode()) <= 64, data
    return InlineKeyboardMarkup(inline_keyboard=[[Btn(text=t("✖️ Cancel download"), callback_data=data)]])


def result_kb(platform: str, media_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                Btn(
                    text=t("🎵 Download music from video"),
                    callback_data=cb("music", platform, media_id, "mp3"),
                )
            ]
        ]
    )


def add_to_group_kb(bot_username: str) -> InlineKeyboardMarkup:
    url = f"https://t.me/{bot_username}?startgroup=true"
    return InlineKeyboardMarkup(inline_keyboard=[[Btn(text=t("Add to a group ⤴️"), url=url)]])


def force_join_kb(links: str | list[tuple[str, str]], user_id: int) -> InlineKeyboardMarkup:
    """One join button per channel still to join (`(title, url)` pairs), then the check button."""
    links = [("", links)] if isinstance(links, str) else list(links)
    if len(links) == 1:
        rows = [[Btn(text=t("Join channel ↗️"), url=links[0][1])]]
    else:
        rows = [[Btn(text=t("Join {title} ↗️", title=_short_title(title)), url=url)] for title, url in links]
    rows.append([Btn(text=t("✅ Check membership"), callback_data=f"force_join:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _short_title(title: str, limit: int = 32) -> str:
    title = " ".join(title.split()) or "channel"
    return title if len(title) <= limit else title[: limit - 1] + "…"


LANGUAGE = "uilang"


def language_kb(current: str) -> InlineKeyboardMarkup:
    """One flag button per language; the active one is ticked."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                Btn(
                    text=("✅ " if code == current else "") + label(code),
                    callback_data=f"{LANGUAGE}{SEP}{code}",
                )
            ]
            for code in LANGUAGES
        ]
    )


BATCH = "bt"


def batch_cb(batch_id: str, op: str, arg: str | int = "") -> str:
    data = SEP.join([BATCH, batch_id, op, str(arg)])
    assert len(data.encode()) <= 64, data
    return data


def batch_summary_kb(batch) -> InlineKeyboardMarkup:
    from app.services.batches import PRESETS
    from app.services.captions import quality_label

    rows: list[list[Btn]] = []
    if not batch.meta.get("audio_only"):
        rows.append(
            [
                Btn(
                    text=("✅ " if batch.quality == preset else "") + quality_label(preset),
                    callback_data=batch_cb(batch.id, "q", preset),
                )
                for preset in PRESETS
            ]
        )
    rows.append(
        [
            Btn(text=t("📥 Download {n}", n=len(batch.chosen)), callback_data=batch_cb(batch.id, "go")),
            Btn(text=t("☑️ Choose items"), callback_data=batch_cb(batch.id, "p", 0)),
        ]
    )
    rows.append([Btn(text=t("✖️ Cancel"), callback_data=batch_cb(batch.id, "x"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def batch_pages(batch) -> int:
    from app.services.batches import PICKER_PAGE

    return max(1, -(-len(batch.items) // PICKER_PAGE))


def batch_picker_kb(batch, page: int) -> InlineKeyboardMarkup:
    from app.services.batches import PICKER_PAGE
    from app.services.captions import _short, human_duration

    pages = batch_pages(batch)
    page = min(max(page, 0), pages - 1)
    rows: list[list[Btn]] = []
    for index in range(page * PICKER_PAGE, min(len(batch.items), (page + 1) * PICKER_PAGE)):
        item = batch.items[index]
        mark = "✅" if batch.selected[index : index + 1] == "1" else "⬜"
        duration = f" · {human_duration(item.duration)}" if item.duration else ""
        rows.append(
            [
                Btn(
                    text=f"{mark} {index + 1}. {_short(item.title, 40)}{duration}",
                    callback_data=batch_cb(batch.id, "t", index),
                )
            ]
        )
    if pages > 1:
        rows.append(
            [
                Btn(text="◀️", callback_data=batch_cb(batch.id, "p", (page - 1) % pages)),
                Btn(text=f"{page + 1}/{pages}", callback_data=batch_cb(batch.id, "p", page)),
                Btn(text="▶️", callback_data=batch_cb(batch.id, "p", (page + 1) % pages)),
            ]
        )
    rows.append(
        [
            Btn(text=t("☑️ Select all"), callback_data=batch_cb(batch.id, "all", page)),
            Btn(text=t("⬜ Select none"), callback_data=batch_cb(batch.id, "none", page)),
        ]
    )
    rows.append([Btn(text=t("✔️ Done"), callback_data=batch_cb(batch.id, "back"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def batch_running_kb(batch) -> InlineKeyboardMarkup | None:
    if batch.phase != "running":
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[Btn(text=t("⏹ Stop after this item"), callback_data=batch_cb(batch.id, "stop"))]]
    )
