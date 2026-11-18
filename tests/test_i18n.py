import ast
import asyncio
import re
import string
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from app import i18n
from app.bot import language
from app.config import Settings
from app.i18n import fa
from app.models import FormatOption, GalleryItem, MediaInfo
from app.platforms.registry import PLATFORMS
from app.services import captions, errors, keyboards
from app.services.progress import Monitor

ROOT = Path(__file__).parent.parent / "app"
PERSIAN = re.compile(r"[؀-ۿ]")
MB = 1 << 20


def source_texts() -> set[str]:
    """Every English text the code passes to t(), plus status tables and user-facing errors."""
    found = set()
    for path in ROOT.rglob("*.py"):
        if path.parent.name == "i18n":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name == "t" and node.args:
                    arg = node.args[0]
                    options = [arg.body, arg.orelse] if isinstance(arg, ast.IfExp) else [arg]
                    found |= {o.value for o in options if isinstance(o, ast.Constant)}
                if (
                    name in {"UserError", "StorageFull"}
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                ):
                    found.add(node.args[0].value)
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Dict)
                and any(getattr(target, "id", "") in {"TEXTS", "COMMANDS"} for target in node.targets)
            ):
                found |= {v.value for v in node.value.values if isinstance(v, ast.Constant)}
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.IfExp)  # text = "a" if ... else "b"; t(text, ...)
                and any(getattr(t, "id", "") == "text" for t in node.targets)
            ):
                found |= {
                    o.value for o in (node.value.body, node.value.orelse) if isinstance(o, ast.Constant)
                }
    from app.services import progress, storage

    found |= {text for _, text in progress.STEPS}
    found |= {text for stage in progress.PROCESS_STAGES.values() for text in stage[1:]}
    found.add(storage.LOW_STORAGE)
    return found


def fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_every_interface_text_has_a_farsi_translation():
    missing = sorted(text for text in source_texts() if text.strip() and text not in fa.TEXTS)
    assert not missing


@pytest.mark.parametrize("english", sorted(fa.TEXTS))
def test_translations_keep_placeholders_and_markup(english):
    persian = fa.TEXTS[english]
    assert fields(persian) == fields(english)
    assert re.findall(r"</?\w+>", persian) == re.findall(r"</?\w+>", english)
    assert PERSIAN.search(persian)


def test_farsi_is_the_default_language(monkeypatch):
    monkeypatch.delenv("DEFAULT_LANGUAGE")
    assert Settings(bot_token="1:x").default_language == "fa"


def test_texts_follow_the_active_language():
    assert captions.BUSY.startswith("⏳ You already")
    i18n.use("fa")
    assert fa.TEXTS[captions.TEXTS["BUSY"]] == captions.BUSY
    assert fa.TEXTS[errors.TEXTS["TEMPORARY"]] == errors.TEMPORARY
    i18n.use("xx")  # unknown codes fall back to the default
    assert i18n.current() == "en"


def test_already_translated_text_is_left_alone():
    i18n.use("fa")
    i18n.missing.clear()
    assert i18n.t(captions.EXPIRED) == captions.EXPIRED  # e.g. UserError(captions.EXPIRED)
    assert not i18n.missing


def test_user_errors_are_shown_in_farsi():
    from app.platforms.ytdlp import UserError

    i18n.use("fa")
    text = errors.user_message(UserError("This Threads post has no photos or videos."))
    assert text == "⚠️ " + fa.TEXTS["This Threads post has no photos or videos."]


def test_farsi_screens_render_without_missing_translations():
    i18n.use("fa")
    i18n.missing.clear()
    info = MediaInfo(
        "yt",
        "id",
        "https://youtu.be/x",
        "Title",
        uploader="U",
        duration=100,
        formats=[FormatOption("720", "720p", 10 * MB, 720), FormatOption("mp3", "MP3", MB)],
    )
    gallery = MediaInfo("ig", "g", "https://ig/x", "G", kind="gallery", items=[GalleryItem("u", "photo")] * 2)
    screens = [
        captions.welcome(PLATFORMS, "bot"),
        captions.info_caption(info, 2000 * MB),
        captions.info_caption(gallery, 2000 * MB),
        captions.status_caption(gallery, captions.QUEUED, "all"),
        captions.extracting_caption("YouTube", 12),
        captions.force_join_caption(["A"]),
        captions.force_join_caption(["A", "B"]),
        captions.result_caption(info, "bot"),
        captions.language_menu("fa"),
        captions.language_changed(),
    ]
    now = [0.0]
    m = Monitor(now=lambda: now[0], timeout=90)
    screens.append(m.render())
    m(MB, 400 * MB, track="audio", part=1, parts=2, part_done=MB, part_size=200 * MB)
    now[0] += 5
    m(50 * MB, 400 * MB, track="audio", part=1, parts=2, part_done=50 * MB, part_size=200 * MB)
    screens.append(m.render())
    m(
        50 * MB,
        400 * MB,
        retry=1,
        retries=3,
        reason="the server stopped sending data",
        wait=4,
        kept=50 * MB,
        part=1,
        parts=2,
    )
    screens.append(m.render())
    m.restarting(2, 3, "the server replied 503", 8)
    screens.append(m.render())
    m(60 * MB, 400 * MB)
    now[0] += 30
    screens.append(m.render())  # stalled, recovered
    for stage in ("merge", "convert", "finalize"):
        m(1, 1, stage=stage)
        screens.append(m.render())
    m.set_stage("upload", size=400 * MB)
    m.cancel.set()
    screens.append(m.render())
    files = Monitor("files")
    files(1, 3)
    screens.append(files.render())
    assert not i18n.missing, i18n.missing
    assert all(PERSIAN.search(s) for s in screens)
    restart, stalled = screens[13], screens[14]
    assert "سرور با کد 503 پاسخ داد" in restart and "تلاش <b>2</b> از 3" in restart
    assert "⏱ زمان سپری‌شده: 35 ثانیه" in stalled and "داده‌ای نرسیده" in stalled


def test_full_farsi_status_fits_a_photo_caption():
    i18n.use("fa")
    now = [0.0]
    m = Monitor(now=lambda: now[0], timeout=90)
    m(0, 1900 * MB, track="video", part=1, parts=10, part_done=0, part_size=200 * MB)
    now[0] += 3700
    m(1210 * MB, 1900 * MB, track="video", part=7, parts=10, part_done=10 * MB, part_size=200 * MB)
    m(
        1210 * MB,
        1900 * MB,
        retry=3,
        retries=3,
        reason="the connection dropped mid-transfer",
        wait=9,
        kept=1210 * MB,
        part=7,
        parts=10,
    )
    m.cancel.set()
    info = MediaInfo(
        "yt",
        "id",
        "https://youtu.be/x",
        "T" * 300,
        uploader="U" * 60,
        duration=4000,
        formats=[FormatOption("1080", "1080p", 1, 1080)],
    )
    text = captions.status_caption(info, m.render(), "1080", "de-DE")
    assert len(text.encode("utf-16-le")) // 2 <= captions.CAPTION_LIMIT
    assert text.startswith("⬇️ <b>در حال دانلود</b>")


def test_buttons_are_translated():
    i18n.use("fa")
    assert keyboards.cancel_kb("r").inline_keyboard[0][0].text == "✖️ لغو دانلود"
    assert keyboards.add_to_group_kb("bot").inline_keyboard[1][0].text == "افزودن به گروه ⤴️"
    assert keyboards.add_to_group_kb("bot").inline_keyboard[0][0].text == "📖 راهنمای استفاده"


# --------------------------------------------------------------- storage and middleware


def test_choice_is_stored_per_user(request_store):
    assert i18n.load(1) == "en"  # tests run with DEFAULT_LANGUAGE=en
    i18n.save(1, "fa")
    i18n._cache.clear()
    assert i18n.load(1) == "fa" and i18n.load(2) == "en"
    with pytest.raises(ValueError):
        i18n.save(1, "xx")


def test_middleware_applies_the_users_language(request_store):
    i18n.save(7, "fa")
    seen = {}

    async def handler(event, data):
        seen.update(data=data["ui_language"], current=i18n.current(), text=captions.BUSY)

    asyncio.run(language.LanguageMiddleware()(handler, NS(from_user=NS(id=7)), {}))
    assert seen["data"] == seen["current"] == "fa" and PERSIAN.search(seen["text"])


def test_middleware_falls_back_to_default_when_redis_is_down(monkeypatch):
    monkeypatch.setattr(i18n, "load", MagicMock(side_effect=ConnectionError))
    seen = {}

    async def handler(event, data):
        seen["lang"] = data["ui_language"]

    asyncio.run(language.LanguageMiddleware()(handler, NS(from_user=NS(id=7)), {}))
    assert seen["lang"] == "en"


# --------------------------------------------------------------- /language


def test_language_command_lists_flags_and_marks_current():
    i18n.use("fa")
    msg = MagicMock(spec=Message)
    msg.answer = AsyncMock()
    asyncio.run(language.choose_language(msg))
    text = msg.answer.await_args.args[0]
    assert "زبان ربات را انتخاب کنید" in text and "Choose the bot's language" in text
    rows = msg.answer.await_args.kwargs["reply_markup"].inline_keyboard
    assert [r[0].text for r in rows] == ["✅ 🇮🇷 فارسی", "🇬🇧 English"]
    assert [r[0].callback_data for r in rows] == ["uilang|fa", "uilang|en"]


def test_choosing_a_language_saves_it_and_confirms_in_that_language(request_store):
    msg = MagicMock(spec=Message)
    msg.edit_text = AsyncMock()
    cq = MagicMock(spec=CallbackQuery)
    cq.data, cq.message, cq.from_user, cq.answer = "uilang|fa", msg, NS(id=5), AsyncMock()
    asyncio.run(language.set_language(cq))
    assert i18n.load(5) == "fa"
    text = msg.edit_text.await_args.args[0]
    assert text.startswith("✅ زبان ربات به فارسی تغییر کرد.") and "/language" in text


def test_unknown_language_button_is_ignored(request_store):
    cq = MagicMock(spec=CallbackQuery)
    cq.data, cq.message, cq.from_user, cq.answer = "uilang|xx", None, NS(id=5), AsyncMock()
    asyncio.run(language.set_language(cq))
    assert request_store.keys("uilang:*") == []


def test_language_can_be_changed_before_joining_required_channels():
    from app.bot import membership
    from tests.test_membership import CHANNEL, bot, callback, message

    gate = membership.MembershipMiddleware((CHANNEL,), "test_bot")
    for event in (message("/language"), message("/language@test_bot"), callback("uilang|en")):
        handler = AsyncMock(return_value="handled")
        assert asyncio.run(gate(handler, event, {"bot": bot()})) == "handled"
    handler = AsyncMock()
    asyncio.run(gate(handler, message("/languages"), {"bot": bot()}))
    handler.assert_not_awaited()


def test_welcome_and_link_hint_point_to_language_command():
    i18n.use("fa")
    assert "🌐 برای تغییر زبان، /language رو بفرست" in captions.welcome(PLATFORMS, "bot")


def test_command_menu_is_published_per_language():
    b = MagicMock()
    b.set_my_commands = AsyncMock()
    asyncio.run(language.publish_commands(b))
    calls = {c.kwargs.get("language_code"): c.args[0] for c in b.set_my_commands.await_args_list}
    assert [c.command for c in calls["fa"]] == ["start", "language", "help"]
    assert calls["fa"][1].description == "تغییر زبان ربات 🌐"
    assert calls["en"][1].description == "Change the bot's language 🌐"
    assert calls[None] == calls["en"]  # tests default to English; production defaults to Farsi


def test_queued_task_carries_the_requesters_language(request_store, monkeypatch):
    from app.services import jobs
    from app.worker import tasks
    from tests.test_jobs import bot as jobs_bot
    from tests.test_jobs import card, info

    publish = MagicMock()
    monkeypatch.setattr(tasks.download_media, "apply_async", publish)
    i18n.use("fa")
    c = card()
    asyncio.run(jobs.request_download(jobs_bot(), info(), "720", None, chat_id=1, user_id=10, card=c))
    assert publish.call_args.kwargs["kwargs"] == {"ui_language": "fa"}
    assert c.edit_text.call_args.args[0].startswith("🕓 <b>در صف</b>")
    assert c.edit_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0].text == "✖️ لغو دانلود"


def test_worker_task_speaks_the_requesters_language(monkeypatch):
    from app.worker import tasks

    seen = {}

    async def run(*args):
        seen["lang"] = i18n.current()
        seen["text"] = captions.UPLOADING

    monkeypatch.setattr(tasks, "_run", run)
    tasks.download_media.run(1, 2, False, "yt", "id", "720", None, 10, None, ui_language="fa")
    assert seen["lang"] == "fa" and seen["text"].startswith("📤 <b>در حال ارسال")
    tasks.download_media.run(1, 2, False, "yt", "id", "720", None, 10)  # tasks queued before this change
    assert seen["lang"] == "en"
