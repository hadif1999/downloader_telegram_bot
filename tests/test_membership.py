import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import CallbackQuery, Message
from pydantic import ValidationError

from app.bot import main as bot_main
from app.bot import membership
from app.config import Settings
from app.services import captions
from app.services.keyboards import force_join_kb


def message(text="/start", chat_type=ChatType.PRIVATE):
    msg = MagicMock(spec=Message)
    msg.text, msg.caption = text, None
    msg.chat = NS(type=chat_type, id=10)
    msg.from_user = NS(id=42, is_bot=False)
    msg.answer = AsyncMock()
    msg.edit_text = AsyncMock()
    msg.reply_markup = force_join_kb("https://t.me/channel", 42)
    return msg


def callback(data="force_join:42", msg=None, user_id=42):
    cq = MagicMock(spec=CallbackQuery)
    cq.data, cq.message = data, msg or message()
    cq.from_user = NS(id=user_id, is_bot=False)
    cq.answer = AsyncMock()
    return cq


def bot(status=ChatMemberStatus.LEFT):
    b = MagicMock()
    b.get_chat_member = AsyncMock(return_value=NS(status=status, is_member=False))
    b.me = AsyncMock(return_value=NS(id=123456, username="test_bot"))
    b.get_chat = AsyncMock(
        return_value=NS(
            id=-100123, type=ChatType.CHANNEL, title="Channel <&>", username="channel", invite_link=None
        )
    )
    return b


def gate():
    return membership.MembershipMiddleware(-100123, "Channel <&>", "https://t.me/channel", "test_bot")


def invoke(g, event, b, handler=None):
    h = handler or AsyncMock(return_value="handled")
    result = asyncio.run(g(h, event, {"bot": b}))
    return result, h


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("-100123", -100123),
        (-100123, -100123),
        (" @my_channel ", "@my_channel"),
    ],
)
def test_channel_settings_normalize_empty_ids_and_usernames(value, expected):
    assert Settings(_env_file=None, force_join_channel_id=value).force_join_channel_id == expected


@pytest.mark.parametrize("value", ["12345", 0, "false", "channel", "@", True])
def test_invalid_channel_ids_fail_validation(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, force_join_channel_id=value)


def test_channel_settings_load_from_dotenv(tmp_path):
    path = tmp_path / ".env"
    path.write_text("FORCE_JOIN_CHANNEL_ID=-100123\nFORCE_JOIN_CHANNEL_URL=https://t.me/+Invite_123\n")
    configured = Settings(_env_file=path)
    assert configured.force_join_channel_id == -100123
    assert configured.force_join_channel_url == "https://t.me/+Invite_123"
    path.write_text("FORCE_JOIN_CHANNEL_ID=\nFORCE_JOIN_CHANNEL_URL=\n")
    assert Settings(_env_file=path).force_join_channel_id is None


@pytest.mark.parametrize(
    "url", ["https://t.me/channel", "https://t.me/+Invite_123", "https://t.me/joinchat/Invite-123", ""]
)
def test_channel_invite_urls_are_supported(url):
    assert Settings(_env_file=None, force_join_channel_url=url).force_join_channel_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://t.me/channel",
        "https://evil.invalid/channel",
        "https://t.me@evil.invalid/channel",
        "https://t.me/c/123/4",
        "javascript:alert(1)",
    ],
)
def test_invalid_join_urls_fail_validation(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, force_join_channel_url=url)


def test_disabled_gate_has_no_api_calls_or_behavior_change():
    b = bot()
    g = asyncio.run(membership.MembershipMiddleware.configure(b, Settings(_env_file=None)))
    result, h = invoke(g, message(), b)
    assert result == "handled"
    h.assert_awaited_once()
    b.get_chat_member.assert_not_awaited()
    b.get_chat.assert_not_awaited()


def test_public_channel_resolves_canonical_id_and_link():
    b = bot(ChatMemberStatus.ADMINISTRATOR)
    g = asyncio.run(
        membership.MembershipMiddleware.configure(
            b, Settings(_env_file=None, force_join_channel_id="@channel")
        )
    )
    assert (g.channel_id, g.url) == (-100123, "https://t.me/channel")
    b.get_chat.assert_awaited_once_with("@channel", request_timeout=10)
    b.get_chat_member.assert_awaited_once_with(-100123, 123456, request_timeout=10)


@pytest.mark.parametrize(
    "status", [ChatMemberStatus.MEMBER, ChatMemberStatus.LEFT, ChatMemberStatus.RESTRICTED]
)
def test_startup_requires_bot_administrator(status):
    with pytest.raises(RuntimeError, match="administrator"):
        asyncio.run(
            membership.MembershipMiddleware.configure(
                bot(status), Settings(_env_file=None, force_join_channel_id=-100123)
            )
        )


def test_group_cannot_be_mistaken_for_channel():
    b = bot(ChatMemberStatus.ADMINISTRATOR)
    b.get_chat.return_value.type = ChatType.SUPERGROUP
    with pytest.raises(RuntimeError, match="Telegram channel"):
        asyncio.run(
            membership.MembershipMiddleware.configure(
                b, Settings(_env_file=None, force_join_channel_id=-100123)
            )
        )


@pytest.mark.parametrize(
    "url,metadata_url", [("https://t.me/+Configured", None), ("", "https://t.me/+Existing")]
)
def test_private_channel_uses_explicit_or_existing_invite(url, metadata_url):
    b = bot(ChatMemberStatus.ADMINISTRATOR)
    b.get_chat.return_value.username = None
    b.get_chat.return_value.invite_link = metadata_url
    g = asyncio.run(
        membership.MembershipMiddleware.configure(
            b, Settings(_env_file=None, force_join_channel_id=-100123, force_join_channel_url=url)
        )
    )
    assert g.url == (url or metadata_url)


def test_private_channel_without_invite_fails_clearly():
    b = bot(ChatMemberStatus.ADMINISTRATOR)
    b.get_chat.return_value.username = None
    with pytest.raises(RuntimeError, match="FORCE_JOIN_CHANNEL_URL"):
        asyncio.run(
            membership.MembershipMiddleware.configure(
                b, Settings(_env_file=None, force_join_channel_id=-100123)
            )
        )


@pytest.mark.parametrize("text", ["/start", "/help", "https://youtu.be/abcdefghijk"])
def test_nonmember_cannot_skip_gate_with_commands_or_direct_links(text):
    b, msg = bot(), message(text)
    result, h = invoke(gate(), msg, b)
    assert result is None
    h.assert_not_awaited()
    body = msg.answer.call_args.args[0]
    assert "Channel &lt;&amp;&gt;" in body and captions.JOIN_REQUIRED in body
    buttons = msg.answer.call_args.kwargs["reply_markup"].inline_keyboard
    assert buttons[0][0].url == "https://t.me/channel"
    assert buttons[1][0].callback_data == "force_join:42"


@pytest.mark.parametrize("action", ["fmt", "dl", "music", "thumb", "desc", "back"])
def test_old_download_buttons_cannot_bypass_membership(action):
    b, cq = bot(), callback(f"{action}|yt|id|720|")
    _, h = invoke(gate(), cq, b)
    h.assert_not_awaited()
    cq.answer.assert_awaited_once_with(captions.JOIN_REQUIRED, show_alert=True, request_timeout=5)
    cq.message.answer.assert_awaited_once()


@pytest.mark.parametrize(
    "status,is_member,allowed",
    [
        (ChatMemberStatus.MEMBER, False, True),
        (ChatMemberStatus.ADMINISTRATOR, False, True),
        (ChatMemberStatus.CREATOR, False, True),
        (ChatMemberStatus.RESTRICTED, True, True),
        (ChatMemberStatus.RESTRICTED, False, False),
        (ChatMemberStatus.LEFT, False, False),
        (ChatMemberStatus.KICKED, False, False),
    ],
)
def test_membership_statuses_are_handled_without_allowing_left_or_banned_users(status, is_member, allowed):
    b = bot(status)
    b.get_chat_member.return_value.is_member = is_member
    _, h = invoke(gate(), message(), b)
    assert h.await_count == int(allowed)


def test_membership_is_rechecked_when_user_leaves():
    b, g, h = bot(), gate(), AsyncMock()
    b.get_chat_member.side_effect = [NS(status=ChatMemberStatus.MEMBER), NS(status=ChatMemberStatus.LEFT)]
    invoke(g, message(), b, h)
    invoke(g, message(), b, h)
    assert h.await_count == 1 and b.get_chat_member.await_count == 2


def test_check_acknowledges_before_lookup_and_then_shows_welcome(monkeypatch):
    b, cq = bot(ChatMemberStatus.MEMBER), callback()
    events = []
    cq.answer.side_effect = lambda **kwargs: events.append("ack")

    async def checked(*a, **kw):
        events.append("check")
        return NS(status=ChatMemberStatus.MEMBER)

    b.get_chat_member.side_effect = checked
    welcome = AsyncMock()
    monkeypatch.setattr(membership, "send_welcome", welcome)
    _, h = invoke(gate(), cq, b)
    assert events == ["ack", "check"]
    h.assert_not_awaited()
    welcome.assert_awaited_once_with(cq.message, b, edit=True)


def test_check_does_not_unlock_a_nonmember():
    cq = callback()
    _, h = invoke(gate(), cq, bot())
    h.assert_not_awaited()
    cq.message.edit_text.assert_awaited_once()
    assert captions.JOIN_NOT_FOUND in cq.message.edit_text.call_args.args[0]


def test_other_users_cannot_take_over_group_membership_prompts():
    b, cq = bot(), callback(user_id=99)
    _, h = invoke(gate(), cq, b)
    h.assert_not_awaited()
    b.get_chat_member.assert_not_awaited()
    cq.answer.assert_awaited_once_with(
        "Send /start to check your own membership.", show_alert=True, request_timeout=5
    )


def test_forged_check_data_must_match_original_keyboard():
    b, cq = bot(), callback("force_join:99", user_id=99)
    _, h = invoke(gate(), cq, b)
    h.assert_not_awaited()
    b.get_chat_member.assert_not_awaited()


@pytest.mark.parametrize(
    "error", [TimeoutError(), TelegramNetworkError(method=MagicMock(), message="unavailable")]
)
def test_membership_api_failure_never_grants_access(error):
    b, msg = bot(), message()
    b.get_chat_member.side_effect = error
    _, h = invoke(gate(), msg, b)
    h.assert_not_awaited()
    assert captions.JOIN_UNAVAILABLE in msg.answer.call_args.args[0]


def test_failed_check_callback_stops_spinner_and_shows_retry():
    b, cq = bot(), callback()
    b.get_chat_member.side_effect = TimeoutError()
    _, h = invoke(gate(), cq, b)
    h.assert_not_awaited()
    cq.answer.assert_awaited_once_with(request_timeout=5)
    assert captions.JOIN_UNAVAILABLE in cq.message.edit_text.call_args.args[0]


@pytest.mark.parametrize("text", ["hello everyone", "   ", "/start@other_bot"])
def test_unrelated_group_chatter_is_not_spammed_with_join_prompts(text):
    b, msg = bot(), message(text, ChatType.SUPERGROUP)
    result, h = invoke(gate(), msg, b)
    assert result == "handled"
    msg.answer.assert_not_awaited()
    b.get_chat_member.assert_not_awaited()


def test_group_download_link_is_still_protected():
    msg = message("https://youtu.be/abcdefghijk", ChatType.SUPERGROUP)
    _, h = invoke(gate(), msg, bot())
    h.assert_not_awaited()
    msg.answer.assert_awaited_once()


def test_anonymous_or_bot_authored_messages_cannot_bypass_gate():
    for user in [None, NS(id=42, is_bot=True)]:
        b, msg = bot(), message()
        msg.from_user = user
        _, h = invoke(gate(), msg, b)
        h.assert_not_awaited()
        b.get_chat_member.assert_not_awaited()


def test_repeated_check_tolerates_unchanged_message():
    cq = callback()
    cq.message.edit_text.side_effect = TelegramBadRequest(
        method=MagicMock(), message="message is not modified"
    )
    _, h = invoke(gate(), cq, bot())
    h.assert_not_awaited()


def test_old_check_button_recovers_after_gate_is_disabled(monkeypatch):
    b, welcome = bot(), AsyncMock()
    monkeypatch.setattr(membership, "send_welcome", welcome)
    invoke(membership.MembershipMiddleware(), callback(), b)
    b.get_chat_member.assert_not_awaited()
    welcome.assert_awaited_once()


def test_gate_configuration_failure_closes_session_before_polling(monkeypatch):
    b = bot()
    b.get_chat.side_effect = TelegramBadRequest(method=MagicMock(), message="chat not found")
    b.session.close = AsyncMock()
    dp = MagicMock()
    dp.start_polling = AsyncMock()
    monkeypatch.setattr(bot_main, "make_bot", lambda: b)
    monkeypatch.setattr(bot_main, "Dispatcher", lambda: dp)
    monkeypatch.setattr(
        bot_main, "get_settings", lambda: Settings(_env_file=None, force_join_channel_id=-100123)
    )
    with pytest.raises(RuntimeError, match="Cannot access"):
        asyncio.run(bot_main.main())
    b.session.close.assert_awaited_once()
    dp.start_polling.assert_not_awaited()


def test_same_gate_is_installed_on_messages_and_callbacks(monkeypatch):
    b = bot()
    b.delete_webhook = AsyncMock()
    b.session.close = AsyncMock()
    dp = MagicMock()
    dp.start_polling = AsyncMock()
    monkeypatch.setattr(bot_main, "make_bot", lambda: b)
    monkeypatch.setattr(bot_main, "Dispatcher", lambda: dp)
    asyncio.run(bot_main.main())
    gate_instance = dp.message.outer_middleware.call_args.args[0]
    dp.callback_query.outer_middleware.assert_called_once_with(gate_instance)


def test_real_dispatcher_blocks_both_paths_before_download_handlers(monkeypatch):
    from datetime import UTC, datetime

    from aiogram import Bot, Dispatcher, F
    from aiogram.methods import AnswerCallbackQuery, SendMessage
    from aiogram.types import Chat, ChatMemberLeft, ChatMemberMember, Update, User

    async def scenario():
        b = Bot("123456:offline-tests")
        dp = Dispatcher()
        dp.message.outer_middleware(gate())
        dp.callback_query.outer_middleware(gate())
        reached = []

        async def on_link(message):
            reached.append("link")

        async def on_download(query):
            reached.append("callback")

        dp.message.register(on_link, F.text)
        dp.callback_query.register(on_download, F.data.startswith("dl|"))
        user = User(id=42, is_bot=False, first_name="Test user")
        msg = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=42, type="private"),
            from_user=user,
            text="https://youtu.be/abcdefghijk",
        )
        query = CallbackQuery(
            id="check-id", from_user=user, chat_instance="instance", message=msg, data="dl|yt|id|720|"
        )
        states = [
            ChatMemberLeft(user=user),
            ChatMemberLeft(user=user),
            ChatMemberMember(user=user),
            ChatMemberMember(user=user),
        ]
        lookup = AsyncMock(side_effect=states)
        monkeypatch.setattr(b, "get_chat_member", lookup)

        async def request(bot, method, timeout=None):
            if isinstance(method, SendMessage):
                return msg
            if isinstance(method, AnswerCallbackQuery):
                return True
            raise AssertionError("Unexpected API method in offline dispatch test")

        monkeypatch.setattr(b.session, "make_request", request)
        try:
            await dp.feed_update(b, Update(update_id=1, message=msg))
            await dp.feed_update(b, Update(update_id=2, callback_query=query))
            assert reached == []
            await dp.feed_update(b, Update(update_id=3, message=msg))
            await dp.feed_update(b, Update(update_id=4, callback_query=query))
            assert reached == ["link", "callback"]
            assert lookup.await_count == 4
        finally:
            await b.session.close()

    asyncio.run(scenario())
