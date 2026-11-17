import json
import os
import subprocess
import sys

import pydantic
import pytest
from aiogram import Bot
from aiogram.methods import GetChatMember, SendMediaGroup, SendMessage, SendVideo
from aiogram.types import ChatMemberMember, Message, Update

from app import lean_models


def aiogram_models():
    return sorted(
        (c for c in lean_models._subclasses(pydantic.BaseModel) if c.__module__.startswith("aiogram.")),
        key=lambda c: c.__qualname__,
    )


@pytest.mark.skipif(os.environ.get("AIOGRAM_EAGER_MODELS") == "1", reason="eager mode requested")
def test_lazy_building_is_active_in_this_process():
    assert lean_models._installed


def test_every_aiogram_model_still_resolves_all_forward_references():
    failures = []
    for cls in aiogram_models():
        try:
            cls.model_rebuild(force=True)
        except Exception as error:  # report every broken model at once
            failures.append(f"{cls.__qualname__}: {error}")
    assert not failures, failures[:5]


@pytest.mark.parametrize(
    "method,result,expected",
    [
        (
            SendMessage(chat_id=1, text="x"),
            {"message_id": 5, "date": 0, "chat": {"id": 1, "type": "private"}, "text": "x"},
            Message,
        ),
        (
            SendVideo(chat_id=1, video="file:///downloads/a.mp4"),
            {
                "message_id": 6,
                "date": 0,
                "chat": {"id": 1, "type": "private"},
                "video": {"file_id": "v", "file_unique_id": "u", "width": 1, "height": 1, "duration": 1},
            },
            Message,
        ),
        (
            SendMediaGroup(
                chat_id=1, media=[{"type": "photo", "media": "a"}, {"type": "photo", "media": "b"}]
            ),
            [{"message_id": n, "date": 0, "chat": {"id": 1, "type": "private"}, "photo": []} for n in (7, 8)],
            list,
        ),
        (
            GetChatMember(chat_id=-100, user_id=2),
            {"status": "member", "user": {"id": 2, "is_bot": False, "first_name": "u"}},
            ChatMemberMember,
        ),
    ],
)
def test_api_responses_parse_into_lazily_built_types(method, result, expected):
    bot = Bot("123456:offline-tests")
    response = bot.session.check_response(bot, method, 200, json.dumps({"ok": True, "result": result}))
    assert isinstance(response.result, expected)


def method_classes():
    from aiogram.methods.base import TelegramMethod

    # Concrete API methods only; generic TelegramMethod[...] helpers can't be built in any mode.
    return sorted(
        (
            c
            for c in lean_models._subclasses(TelegramMethod)
            if c.__module__.startswith("aiogram.methods.") and "[" not in c.__name__
        ),
        key=lambda c: c.__qualname__,
    )


FRESH_RESPONSES = r"""
import json, app
from aiogram import Bot
from aiogram.methods import GetChat, GetChatMember, GetMe, SendMessage
from aiogram.methods.base import Response, TelegramMethod
from app import lean_models

bot = Bot("123456:offline-tests")
chat = {
    "id": -100, "type": "channel", "title": "Channel", "username": "channel", "accent_color_id": 0,
    "max_reaction_count": 11, "available_reactions": [{"type": "emoji", "emoji": "+"}],
    "accepted_gift_types": {"unlimited_gifts": False, "limited_gifts": False, "unique_gifts": False,
                            "premium_subscription": False, "gifts_from_channels": False},
}
user = {"id": 2, "is_bot": True, "first_name": "b", "username": "b"}
calls = [
    (GetChat(chat_id="@channel"), chat),
    (GetMe(), user),
    (GetChatMember(chat_id=-100, user_id=2), {"status": "member", "user": user}),
    (SendMessage(chat_id=1, text="x"), {"message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"}}),
]
for method, result in calls:  # first use of each response type happens here, after install()
    bot.session.check_response(bot, method, 200, json.dumps({"ok": True, "result": result}))
methods = [c for c in lean_models._subclasses(TelegramMethod)
           if c.__module__.startswith("aiogram.methods.") and "[" not in c.__name__]
failures = []
for method in methods:
    try:
        Response[method.__returning__].model_rebuild(force=True)
    except Exception as error:
        failures.append(f"{method.__qualname__}: {error}"[:200])
print(lean_models._installed, len(methods), failures[:3])
"""


def test_runtime_created_response_types_resolve_in_a_fresh_process():
    """Generic Response[...] classes are created after install(), on first use. Run in a fresh
    interpreter: earlier tests in this process force-build models and would hide a failure."""
    result = subprocess.run(
        [sys.executable, "-c", FRESH_RESPONSES],
        env={**os.environ, "AIOGRAM_EAGER_MODELS": ""},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.split(" ", 2)[0] == "True"
    count = int(result.stdout.split(" ", 2)[1])
    assert count > 100 and result.stdout.strip().endswith("[]"), result.stdout


def test_updates_round_trip_through_lazy_models():
    data = {
        "update_id": 1,
        "callback_query": {
            "id": "1",
            "chat_instance": "c",
            "data": "dl|yt|id|720|",
            "from": {"id": 2, "is_bot": False, "first_name": "x"},
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": 1, "type": "private"},
                "caption": "c",
                "reply_markup": {"inline_keyboard": [[{"text": "a", "callback_data": "b"}]]},
            },
        },
    }
    update = Update.model_validate(data)
    assert update.callback_query.message.reply_markup.inline_keyboard[0][0].callback_data == "b"
    assert update.model_dump(exclude_none=True)["callback_query"]["data"] == "dl|yt|id|720|"


def probe(env):
    code = (
        "import app, sys, pydantic\n"
        "from app import lean_models\n"
        "pending = [c for c in lean_models._subclasses(pydantic.BaseModel)\n"
        "           if c.__module__.startswith('aiogram.') and not c.__pydantic_complete__]\n"
        "print(lean_models._installed, len(pending))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], env={**os.environ, **env}, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    installed, pending = result.stdout.split()
    return installed == "True", int(pending)


def test_fresh_process_defers_hundreds_of_models():
    installed, pending = probe({"AIOGRAM_EAGER_MODELS": ""})
    assert installed and pending > 300


def test_eager_mode_can_be_restored_by_environment():
    installed, pending = probe({"AIOGRAM_EAGER_MODELS": "1"})
    assert not installed and pending == 0
