"""The hidden /admin panel: access control, statistics, queue inspection, actions, broadcast."""

import asyncio
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, CopyMessage, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app import i18n
from app.bot import activity, admin, membership
from app.bot.admin import broadcast, views
from app.bot.admin import handlers as admin_handlers
from app.config import Settings
from app.services import clock, ops, requests, stats

ADMIN, USER = 7, 42
NOW = 1_763_460_000.0  # 2025-11-18 10:00 UTC


# ------------------------------------------------------------------ configuration


def test_admin_ids_are_parsed_and_validated():
    assert Settings(admin_ids="7, 8,7").admin_ids == [7, 8]
    assert Settings(admin_ids="").admin_ids == []
    with pytest.raises(ValueError, match="ADMIN_IDS"):
        Settings(admin_ids="@someone")
    with pytest.raises(ValueError, match="ADMIN_IDS"):
        Settings(admin_ids="-1001234")


def test_statistics_time_zone_accepts_fixed_offsets():
    assert Settings(stats_timezone="+03:30").stats_timezone == "+03:30"
    with pytest.raises(ValueError, match="STATS_TIMEZONE"):
        Settings(stats_timezone="Mars/Olympus")
    tehran = clock.parse_timezone("+03:30")
    late = datetime(2025, 11, 18, 21, 0, tzinfo=UTC).timestamp()  # 00:30 next day in Tehran
    assert datetime.fromtimestamp(late, tehran).strftime("%Y%m%d") == "20251119"
    assert clock.parse_timezone("UTC-5").utcoffset(None).total_seconds() == -5 * 3600


# ------------------------------------------------------------------ statistics store


def test_activity_is_throttled_and_new_users_counted_once(request_store):
    assert stats.record_activity(USER, "Ann", "ann", "en", now=NOW)
    assert not stats.record_activity(USER, "Ann", "ann", "en", now=NOW + 10)  # throttled in-process
    stats.forget_throttle()
    assert stats.record_activity(USER, "Ann", "ann", "en", now=NOW + 20)
    assert stats.record_activity(USER + 1, "Bob", None, None, now=NOW + 30)
    o = stats.overview(now=NOW + 60)
    assert (o.users, o.new_today, o.dau_today, o.active["1h"]) == (2, 2, 2, 2)
    p = stats.profile(USER)
    assert (p.name, p.username, p.language, p.first) == ("Ann", "ann", "en", int(NOW))


def test_profile_change_is_written_despite_the_throttle(request_store):
    stats.record_activity(USER, "Ann", "ann", now=NOW)
    assert stats.record_activity(USER, "Ann", "ann_new", now=NOW + 5)
    assert stats.find_user("@ann_new") == USER
    assert stats.find_user("ann") is None  # the stale username no longer points at the user
    assert stats.find_user(str(USER)) == USER
    assert stats.find_user("https://t.me/ann_new") == USER


def test_active_windows_and_daily_series(request_store):
    for offset, user in [(0, 1), (2 * 86400, 2), (10 * 86400, 3), (40 * 86400, 4)]:
        stats.record_activity(user, now=NOW - offset)
    o = stats.overview(now=NOW)
    assert o.active == {"1h": 1, "24h": 1, "7d": 2, "30d": 3}
    days = stats.daily(14, now=NOW)
    assert len(days) == 14 and days[-1].active == 1 and days[-3].active == 1 and days[-1].new == 1
    assert stats.unique_active(7, now=NOW) == 2
    hours = stats.hourly(24, now=NOW)
    assert len(hours) == 24 and hours[-1].active == 1


def test_downloads_are_aggregated_per_day_hour_platform_and_user(request_store):
    stats.record_download("yt", "ok", USER, size=100, seconds=4, now=NOW)
    stats.record_download("yt", "cached", USER, now=NOW)
    stats.record_download("instagram", "fail", 5, error="UserError: private", now=NOW)
    stats.record_download("yt", "bogus", USER, now=NOW)  # unknown outcomes are ignored
    o = stats.overview(now=NOW)
    assert o.today == {
        "ok": 1,
        "cached": 1,
        "fail": 1,
        "bytes": 100,
        "secs": 4.0,
        "p:yt:ok": 1,
        "p:yt:cached": 1,
        "p:instagram:fail": 1,
    }
    assert stats.platforms(7, now=NOW) == {"yt": {"ok": 1, "cached": 1}, "instagram": {"fail": 1}}
    assert stats.hourly(1, now=NOW)[0].downloads["ok"] == 1
    assert [(p.id, n) for p, n in stats.top_users()] == [(USER, 2)]
    assert stats.recent_errors()[0]["e"] == "UserError: private"


def test_error_log_is_bounded(request_store):
    for i in range(stats.ERRORS_KEPT + 5):
        stats.record_download("yt", "fail", i, error=str(i), now=NOW)
    errors = stats.recent_errors(100)
    assert len(errors) == stats.ERRORS_KEPT and errors[0]["e"] == str(stats.ERRORS_KEPT + 4)


def test_recipients_skip_blocked_and_banned_users(request_store):
    for user in (1, 2, 3, 4):
        stats.record_activity(user, now=NOW - user * 86400)
    stats.mark_blocked(2)
    stats.set_banned(3, True)
    assert sorted(stats.recipients(batch=1)) == [1, 4]
    assert list(stats.recipients(since=NOW - 1.5 * 86400)) == [1]
    stats.forget_throttle()
    stats.record_activity(2, now=NOW)  # coming back unblocks
    assert sorted(stats.recipients()) == [1, 2, 4]


def test_export_lists_every_user(request_store):
    stats.record_activity(USER, 'Ann, "the" tester', "ann", "fa", now=NOW)
    stats.record_download("yt", "ok", USER, now=NOW)
    stats.set_banned(USER, True)
    header, row = list(stats.export_users())
    assert header[0] == "id" and row[0] == USER and row[1] == "ann" and row[6] == 1 and row[8] == 1


def test_live_downloads_and_nodes_expire_from_their_index(request_store):
    stats.publish_live("r1", {"user": USER, "started": 2})
    stats.publish_live("r2", {"user": 5, "started": 1})
    assert [item["id"] for item in stats.live_downloads()] == ["r2", "r1"]
    stats.clear_live("r2")
    request_store.delete(stats.key("live:r1"))  # expired entry, still indexed
    assert stats.live_downloads() == []
    stats.publish_node("worker:h:1", {"role": "worker", "host": "h"})
    assert stats.nodes()[0]["role"] == "worker"


# ------------------------------------------------------------------ queue inspection


def test_queued_celery_tasks_are_decoded(request_store):
    from celery import Celery

    path = request_store.connection_pool.connection_kwargs["path"]
    app = Celery("probe", broker=f"redis+socket://{path}")
    try:
        args = (100, 200, True, "yt", "media", "720", None, USER, None)
        app.send_task("download_media", args=args, kwargs={"batch": ["b", 1]}, task_id="first")
        app.send_task("advance_batch", args=("b", 1), task_id="second")
    finally:
        app.close()
    assert ops.queue_length() == 2
    first, second = ops.queued()
    assert (first.id, first.name, first.platform, first.fmt, first.user) == (
        "first",
        "download_media",
        "yt",
        "720",
        USER,
    )
    assert (first.chat, first.message, first.is_caption, first.batch) == (100, 200, True, True)
    assert second.name == "advance_batch"
    assert ops.queue_summary() == {"download_media": 1, "advance_batch": 1}


def test_admin_cancel_uses_the_request_owner(request_store):
    assert requests.create("rid", USER)
    assert ops.cancel("rid", USER) == requests.CANCEL_STOPPED
    assert requests.load("rid", USER)["phase"] == "cancelled"


def test_redis_info_is_summarised(request_store):
    info = ops.redis_info()
    assert info["version"] and info["keys"] >= 0


# ------------------------------------------------------------------ rendering


def populated():
    stats.record_activity(USER, "Ann <b>", "ann", "en", chat=(-100, "supergroup", "Group"), now=time.time())
    stats.record_download("yt", "ok", USER, size=5 << 20, seconds=3)
    stats.record_download("yt", "fail", USER, error="TimeoutError: <slow>")
    stats.publish_live(
        "a" * 32,
        {
            "platform": "yt",
            "fmt": "720",
            "user": USER,
            "title": "Clip <1>",
            "started": time.time() - 70,
            "stage": "download",
            "unit": "bytes",
            "done": 50 << 20,
            "total": 100 << 20,
            "speed": 2 << 20,
            "retries": 1,
            "stalled": 20,
        },
    )
    stats.publish_node(
        "worker:h:1",
        {
            "role": "worker",
            "host": "h",
            "at": time.time(),
            "started": time.time() - 3600,
            "rss": 80 << 20,
            "mem": 300 << 20,
            "mem_max": 1 << 30,
            "cpu": 12.5,
            "cpus": 1,
            "load": [0.5, 0.4, 0.3],
            "disk": {"total": 10 << 30, "used": 2 << 30, "free": 8 << 30},
            "retained": {"items": 2, "bytes": 300 << 20},
        },
    )


@pytest.mark.parametrize("language", ["en", "fa"])
def test_every_view_renders_within_telegram_limits(request_store, language):
    populated()
    i18n.use(language)
    rendered = [
        views.home(views.collect_home()),
        views.users(views.collect_users()),
        views.downloads(views.collect_downloads()),
        views.queue(views.collect_queue()),
        views.system(views.collect_system()),
        views.top(stats.top_users()),
        views.errors(stats.recent_errors(), time.time()),
        views.user_card(stats.profile(USER), time.time(), stats.live_downloads()[0]),
        views.user_card(stats.profile(999), time.time()),
        views.broadcast_menu({"all": 3}, False),
        views.broadcast_menu({}, True),
        views.broadcast_confirm("everyone", 3),
        views.maintenance("Back soon"),
        views.maintenance(None),
        views.confirm_cancel({"id": "a" * 32, "platform": "yt", "fmt": "720", "user": USER, "title": "x"}),
    ]
    for text, markup in rendered:
        assert 0 < len(text) < 4096
        assert all(
            b.callback_data.startswith("adm|")
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data
        )
    queue_text = rendered[3][0]
    assert "Clip &lt;1&gt;" in queue_text and "50%" in queue_text
    assert "Ann &lt;b&gt;" in rendered[7][0]  # user-provided names are escaped


def test_empty_store_renders_without_data(request_store):
    for text, _ in (
        views.home(views.collect_home()),
        views.queue(views.collect_queue()),
        views.system(views.collect_system()),
    ):
        assert text
    assert "No worker is online" in views.system(views.collect_system())[0]


def test_queue_view_offers_cancel_buttons_for_running_and_queued(request_store):
    populated()
    data = views.collect_queue()
    data.queued = [ops.QueuedTask("q" * 32, "download_media", "yt", "mp3", USER, 1, 2)]
    _, markup = views.queue(data)
    actions = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"adm|kill|{'a' * 32}" in actions and f"adm|kill|{'q' * 32}" in actions


def test_charts_scale_to_the_largest_value():
    assert views.spark([0, 5, 10]) == "▁▅█"
    assert views.spark([0, 0]) == "▁▁"
    chart = views.bars([("a", 10, ""), ("b", 5, "")], width=4)
    assert "▇▇▇▇ 10" in chart and "▇▇░░ 5" in chart


# ------------------------------------------------------------------ dispatch: access and actions


def message(user=USER, text="/admin", chat_type="private", chat=None):
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=chat or user, type=chat_type),
        from_user=User(id=user, is_bot=False, first_name="Test"),
        text=text,
    )


def callback(data, user=USER):
    return CallbackQuery(
        id=f"cb-{data}",
        from_user=User(id=user, is_bot=False, first_name="T"),
        chat_instance="i",
        message=message(user, "panel"),
        data=data,
    )


@pytest.fixture
def dispatch(request_store, monkeypatch):
    """A real dispatcher with the admin router, activity middleware and an echo handler."""
    monkeypatch.setattr(activity, "get_settings", lambda: NS(admin_ids=[ADMIN]))
    activity.flags.invalidate()
    calls, reached = [], []
    b = Bot("123456:offline-tests")
    dp = Dispatcher()
    dp.include_router(admin.router)
    usage = activity.ActivityMiddleware()
    dp.message.outer_middleware(usage)
    dp.callback_query.outer_middleware(usage)

    rest = Router(name="rest")  # after the admin router, like the bot's own handlers

    @rest.message()
    async def anything(m: Message):
        reached.append(m.text)

    dp.include_router(rest)

    async def request(bot, method, timeout=None):
        calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText, CopyMessage)):
            return message(ADMIN, "panel")
        if isinstance(method, AnswerCallbackQuery):
            return True
        raise AssertionError(f"unexpected {type(method).__name__}")

    monkeypatch.setattr(b.session, "make_request", request)

    async def feed(update):
        await dp.feed_update(b, Update(update_id=len(calls) + 1, **update))
        await asyncio.gather(*activity._pending)

    yield NS(feed=feed, calls=calls, reached=reached, bot=b)
    admin.router._parent_router = None  # the module router can join the next test's dispatcher
    asyncio.run(b.session.close())


def sent(calls):
    return [c.text for c in calls if isinstance(c, (SendMessage, EditMessageText))]


def test_admin_command_is_invisible_to_other_users(dispatch):
    asyncio.run(dispatch.feed({"message": message(USER)}))
    assert dispatch.reached == ["/admin"]  # an ordinary text for everyone else
    assert sent(dispatch.calls) == []
    asyncio.run(dispatch.feed({"callback_query": callback("adm|users", USER)}))
    assert not any(isinstance(c, EditMessageText) for c in dispatch.calls)


def test_admin_opens_the_panel_and_navigates(dispatch):
    asyncio.run(dispatch.feed({"message": message(ADMIN)}))
    assert dispatch.reached == [] and "Admin panel" in sent(dispatch.calls)[0]
    asyncio.run(dispatch.feed({"callback_query": callback("adm|users", ADMIN)}))
    assert "<b>Users</b>" in sent(dispatch.calls)[-1]
    answers = [c for c in dispatch.calls if isinstance(c, AnswerCallbackQuery)]
    assert len(answers) == 1  # every button press is answered exactly once


def test_admin_command_is_ignored_in_groups(dispatch):
    asyncio.run(dispatch.feed({"message": message(ADMIN, chat_type="supergroup", chat=-100)}))
    assert dispatch.reached == ["/admin"]


def test_ban_takes_effect_immediately_and_unban_restores(dispatch):
    asyncio.run(dispatch.feed({"callback_query": callback(f"adm|ban|{USER}", ADMIN)}))
    assert USER in stats.banned_ids()
    asyncio.run(dispatch.feed({"message": message(USER, "https://youtu.be/abcdefghijk")}))
    assert dispatch.reached == []  # dropped silently
    asyncio.run(dispatch.feed({"callback_query": callback(f"adm|unban|{USER}", ADMIN)}))
    asyncio.run(dispatch.feed({"message": message(USER, "hello")}))
    assert dispatch.reached == ["hello"]


def test_admins_cannot_be_banned(dispatch):
    asyncio.run(dispatch.feed({"callback_query": callback(f"adm|ban|{ADMIN}", ADMIN)}))
    assert ADMIN not in stats.banned_ids()


def test_maintenance_answers_users_but_not_admins(dispatch):
    asyncio.run(dispatch.feed({"callback_query": callback("adm|mnton", ADMIN)}))
    asyncio.run(dispatch.feed({"message": message(USER, "hello")}))
    assert dispatch.reached == [] and "under maintenance" in sent(dispatch.calls)[-1]
    asyncio.run(dispatch.feed({"message": message(ADMIN, "admin text")}))
    assert dispatch.reached == ["admin text"]
    asyncio.run(dispatch.feed({"callback_query": callback("adm|mntoff", ADMIN)}))
    asyncio.run(dispatch.feed({"message": message(USER, "back")}))
    assert dispatch.reached == ["admin text", "back"]


def test_maintenance_stays_quiet_about_unrelated_group_chatter(dispatch):
    stats.set_maintenance("Back soon")
    activity.flags.invalidate()
    asyncio.run(dispatch.feed({"message": message(USER, "chatting", chat_type="supergroup", chat=-100)}))
    assert sent(dispatch.calls) == [] and dispatch.reached == []


def test_find_user_by_username_through_text_input(dispatch):
    stats.record_activity(USER, "Ann", "ann", now=time.time())
    asyncio.run(dispatch.feed({"callback_query": callback("adm|find", ADMIN)}))
    asyncio.run(dispatch.feed({"message": message(ADMIN, "@ann")}))
    assert f"<code>{USER}</code>" in sent(dispatch.calls)[-1]
    assert dispatch.reached == []  # the input went to the admin flow, not to link handling


def test_activity_is_recorded_for_every_user(dispatch):
    asyncio.run(dispatch.feed({"message": message(USER, "hello")}))
    assert stats.overview().users == 1


def test_admin_cancels_a_queued_download_and_redraws_the_users_card(dispatch, monkeypatch):
    assert requests.create("r" * 32, USER)
    task = ops.QueuedTask("r" * 32, "download_media", "yt", "720", USER, USER, 55, False)
    monkeypatch.setattr(ops, "queued", lambda limit=20: [task])
    edit = AsyncMock()
    monkeypatch.setattr(admin_handlers.delivery, "edit_status", edit)
    asyncio.run(dispatch.feed({"callback_query": callback(f"adm|killok|{'r' * 32}", ADMIN)}))
    assert requests.load("r" * 32, USER)["phase"] == "cancelled"
    assert edit.await_args.args[1:3] == (USER, 55)


def test_admins_bypass_the_channel_gate(monkeypatch):
    gate = membership.MembershipMiddleware([membership.Channel(-1001, "C", "https://t.me/+c")], "bot")
    monkeypatch.setattr(membership, "is_admin", lambda user_id: user_id == ADMIN)
    handler = AsyncMock(return_value="ok")
    data = {"bot": NS(get_chat_member=AsyncMock(side_effect=AssertionError("no lookup for admins")))}
    assert asyncio.run(gate(handler, message(ADMIN, "https://youtu.be/abcdefghijk"), data)) == "ok"


def test_blocking_the_bot_is_tracked(request_store):
    from aiogram.types import ChatMemberBanned, ChatMemberMember, ChatMemberUpdated

    user = User(id=USER, is_bot=False, first_name="T")
    bot_user = User(id=1, is_bot=True, first_name="B")
    update = ChatMemberUpdated(
        chat=Chat(id=USER, type="private"),
        from_user=user,
        date=datetime.now(UTC),
        old_chat_member=ChatMemberMember(user=bot_user),
        new_chat_member=ChatMemberBanned(user=bot_user, until_date=0),
    )
    asyncio.run(activity.bot_membership_changed(update))
    assert stats.overview().blocked == 1


# ------------------------------------------------------------------ broadcast


def test_broadcast_counts_outcomes_retries_and_marks_blocked(request_store, monkeypatch):
    monkeypatch.setattr(broadcast.asyncio, "sleep", AsyncMock())
    method = MagicMock()
    outcomes = {
        1: [None],
        2: [TelegramForbiddenError(method=method, message="blocked")],
        3: [TelegramRetryAfter(method=method, message="flood", retry_after=3), None],
        4: [TelegramBadRequest(method=method, message="chat not found")],
    }

    async def copy_message(user, chat, msg, **_):
        result = outcomes[user].pop(0)
        if isinstance(result, Exception):
            raise result

    bot = NS(copy_message=copy_message)
    reports = []

    async def report(b):
        reports.append(b.finished)

    job = broadcast.Broadcast(ADMIN, 9, [1, 2, 3, 4])
    asyncio.run(broadcast.run(bot, job, report))
    assert (job.sent, job.blocked, job.failed, job.finished) == (2, 1, 1, True)
    assert reports[-1] is True
    assert stats.overview().blocked == 1


def test_broadcast_stops_when_asked(request_store, monkeypatch):
    monkeypatch.setattr(broadcast.asyncio, "sleep", AsyncMock())
    job = broadcast.Broadcast(ADMIN, 9, [1, 2, 3])

    async def copy_message(user, *a, **k):
        job.stop()

    asyncio.run(broadcast.run(NS(copy_message=copy_message), job, AsyncMock()))
    assert (job.sent, job.stopped) == (1, True)


def test_snapshot_of_progress_is_json_serialisable():
    from app.services.progress import Monitor

    monitor = Monitor()
    monitor(10, 100, connections=2)
    snap = monitor.snapshot()
    assert json.loads(json.dumps(snap))["connections"] == 2 and snap["done"] == 10
