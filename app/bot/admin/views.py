"""Rendering of the /admin panel: pure functions from collected data to (HTML text, keyboard).

Data is gathered by `collect_*` (blocking Redis reads, called in a thread) so the renderers stay
testable without Redis. Callback data: "adm|<action>|<argument>".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from html import escape

from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup

from app.config import get_settings
from app.i18n import t
from app.platforms.registry import PLATFORMS
from app.services import clock, health, ops, stats
from app.services.captions import human_size

PREFIX = "adm"
SPARK = "▁▂▃▄▅▆▇█"
NODE_STALE = 75  # seconds without a heartbeat before a node is shown as unresponsive
LIST_LIMIT = 8
TITLES = {p.name: p.title for p in PLATFORMS}


def cb(action: str, arg: str | int = "") -> str:
    data = f"{PREFIX}|{action}|{arg}"
    assert len(data.encode()) <= 64, data
    return data


def btn(text: str, action: str, arg: str | int = "") -> Btn:
    return Btn(text=text, callback_data=cb(action, arg))


def kb(*rows: list[Btn]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def nav(view: str, arg: str | int = "") -> list[Btn]:
    return [btn(t("🔄 Refresh"), view, arg), btn(t("⬅️ Back"), "home")]


# ------------------------------------------------------------------ formatting


def num(value) -> str:
    return f"{int(value or 0):,}"


def pct(part, whole) -> str:
    return f"{part * 100 / whole:.0f}%" if whole else "—"


def span(seconds) -> str:
    """Compact duration: 45s, 3m 05s, 2h 10m, 4d 03h."""
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return t("{s}s", s=seconds)
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return t("{m}m {s:02}s", m=minutes, s=seconds)
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return t("{h}h {m:02}m", h=hours, m=minutes)
    days, hours = divmod(hours, 24)
    return t("{d}d {h:02}h", d=days, h=hours)


def ago(ts, now: float) -> str:
    return t("{time} ago", time=span(now - float(ts))) if ts else "—"


def stamp(ts) -> str:
    return clock.local(float(ts)).strftime("%Y-%m-%d %H:%M") if ts else "—"


def size(value) -> str:
    return human_size(int(value)) if value else "0"


def title(platform: str) -> str:
    return TITLES.get(platform, platform.capitalize())


def person(user_id, name: str = "", username: str = "") -> str:
    parts = [f"<code>{user_id}</code>"]
    if name:
        parts.append(escape(name[:32]))
    if username:
        parts.append(f"@{escape(username)}")
    return " ".join(parts)


def bars(rows: list[tuple[str, float, str]], width: int = 12) -> str:
    """Monospaced horizontal bars: label ▇▇▇░░ value suffix."""
    top = max((value for _, value, _ in rows), default=0) or 1
    lines = []
    for label, value, suffix in rows:
        filled = round(value / top * width)
        lines.append(f"{label} {'▇' * filled}{'░' * (width - filled)} {num(value)}{suffix}")
    return "<pre>" + escape("\n".join(lines)) + "</pre>"


def spark(values: list[float]) -> str:
    top = max(values, default=0)
    if not top:
        return SPARK[0] * len(values)
    return "".join(SPARK[min(len(SPARK) - 1, round(v / top * (len(SPARK) - 1)))] for v in values)


def outcomes(counts: dict) -> str:
    """✅ sent · ♻️ cached resends · ❌ failed · ✖️ cancelled · ❔ uncertain (symbols only)."""
    marks = (("✅", "ok"), ("♻️", "cached"), ("❌", "fail"), ("✖️", "cancel"), ("❔", "uncertain"))
    return " · ".join(f"{mark} {num(counts.get(name))}" for mark, name in marks)


def delivered(counts: dict) -> float:
    return counts.get("ok", 0) + counts.get("cached", 0)


# ------------------------------------------------------------------ data collection


@dataclass
class Snapshot:
    now: float
    overview: stats.Overview
    live: list[dict] = field(default_factory=list)
    queue: int = 0
    in_flight: int = 0
    nodes: list[dict] = field(default_factory=list)
    maintenance: str | None = None


def collect_home() -> Snapshot:
    return Snapshot(
        time.time(),
        stats.overview(),
        stats.live_downloads(),
        ops.queue_length(),
        ops.in_flight(),
        stats.nodes(),
        stats.maintenance(),
    )


def workers(nodes: list[dict], now: float) -> list[dict]:
    return [n for n in nodes if n.get("role") == "worker" and now - n.get("at", 0) < NODE_STALE]


# ------------------------------------------------------------------ views


def home(s: Snapshot) -> tuple[str, InlineKeyboardMarkup]:
    o, today = s.overview, s.overview.today
    online = len(workers(s.nodes, s.now))
    lines = [
        t("🛠 <b>Admin panel</b>"),
        t("🕒 {time} · time zone {zone}", time=stamp(s.now), zone=escape(get_settings().stats_timezone)),
        "",
        t("👥 <b>Users</b>: {total} · 🆕 today {new}", total=num(o.users), new=num(o.new_today)),
        t(
            "🟢 Active: 1h {h} · 24h {d} · 7d {w} · 30d {m}",
            h=num(o.active.get("1h")),
            d=num(o.active.get("24h")),
            w=num(o.active.get("7d")),
            m=num(o.active.get("30d")),
        ),
        "",
        t("📥 <b>Downloads today</b>: {outcomes}", outcomes=outcomes(today)),
        t(
            "⚙️ <b>Now</b>: ⬇️ {running} running · ⏳ {queued} waiting",
            running=num(len(s.live)),
            queued=num(s.queue),
        ),
        t("🖥 Workers online: {workers}", workers=num(online)) + (" ⚠️" if not online else ""),
    ]
    if s.maintenance:
        lines.append(t("🚧 <b>Maintenance mode is on</b>"))
    markup = kb(
        [btn(t("👥 Users"), "users"), btn(t("📥 Downloads"), "dl")],
        [btn(t("⚙️ Queue"), "queue"), btn(t("🖥 System"), "sys")],
        [btn(t("🏆 Top users"), "top"), btn(t("⚠️ Errors"), "err")],
        [btn(t("🔎 Find user"), "find"), btn(t("📣 Broadcast"), "bc")],
        [btn(t("🚧 Maintenance"), "mnt"), btn(t("📄 Export users"), "csv")],
        [btn(t("🔄 Refresh"), "home"), btn(t("✖️ Close"), "close")],
    )
    return "\n".join(lines), markup


@dataclass
class UsersData:
    overview: stats.Overview
    days: list[stats.Day]
    hours: list[stats.Hour]
    wau: int
    mau: int


def collect_users() -> UsersData:
    return UsersData(
        stats.overview(), stats.daily(14), stats.hourly(24), stats.unique_active(7), stats.unique_active(30)
    )


def users(d: UsersData) -> tuple[str, InlineKeyboardMarkup]:
    o = d.overview
    peak = max(d.hours, key=lambda h: h.active, default=None)
    lines = [
        t("👥 <b>Users</b>"),
        "",
        t("Total: <b>{total}</b>", total=num(o.users)),
        t(
            "🆕 New: today {today} · yesterday {yesterday}",
            today=num(o.new_today),
            yesterday=num(o.new_yesterday),
        ),
        t(
            "🟢 Last seen within: 1h {h} · 24h {d} · 7d {w} · 30d {m}",
            h=num(o.active.get("1h")),
            d=num(o.active.get("24h")),
            w=num(o.active.get("7d")),
            m=num(o.active.get("30d")),
        ),
        t(
            "📊 DAU {dau} · WAU {wau} · MAU {mau} · DAU/MAU {ratio}",
            dau=num(o.dau_today),
            wau=num(d.wau),
            mau=num(d.mau),
            ratio=pct(o.dau_today, d.mau),
        ),
        t(
            "🚫 Blocked the bot: {blocked} · ⛔ Banned: {banned}",
            blocked=num(o.blocked),
            banned=num(o.banned),
        ),
        t(
            "👪 Groups: {groups} ({active} active in 7 days)",
            groups=num(o.groups),
            active=num(o.groups_active),
        ),
        "",
        t("<b>Active users per day</b> (new users after +)"),
        bars([(day.label, day.active, f" +{day.new}") for day in d.days]),
        t("<b>Active users per hour</b> (last 24 h)"),
        "<pre>" + spark([h.active for h in d.hours]) + "</pre>",
    ]
    if peak and peak.active:
        lines.append(t("Peak: {count} users at {hour}:00", count=num(peak.active), hour=peak.label))
    return "\n".join(lines), kb(nav("users"))


@dataclass
class DownloadsData:
    overview: stats.Overview
    days: list[stats.Day]
    hours: list[stats.Hour]
    platforms: dict[str, dict[str, int]]


def collect_downloads() -> DownloadsData:
    return DownloadsData(stats.overview(), stats.daily(14), stats.hourly(24), stats.platforms(7))


def downloads(d: DownloadsData) -> tuple[str, InlineKeyboardMarkup]:
    today = d.overview.today
    week = {}
    for day in d.days[-7:]:
        for name in stats.OUTCOMES:
            week[name] = week.get(name, 0) + day.downloads.get(name, 0)
    finished = sum(week.get(name, 0) for name in ("ok", "cached", "fail", "uncertain"))
    average = today.get("secs", 0) / today["ok"] if today.get("ok") else None
    lines = [
        t("📥 <b>Downloads</b>"),
        "",
        t("Today: {outcomes}", outcomes=outcomes(today)),
        t("Yesterday: {outcomes}", outcomes=outcomes(d.overview.yesterday)),
        t(
            "📦 Uploaded today: {size} · ⏱ average {time} per download",
            size=size(today.get("bytes")),
            time=span(average),
        ),
        t(
            "🎯 Success rate (7 days): {rate} of {count} finished",
            rate=pct(delivered(week), finished),
            count=num(finished),
        ),
        "",
        t("<b>Delivered per day</b> (failures after ❌)"),
        bars(
            [(day.label, delivered(day.downloads), f" ❌{num(day.downloads.get('fail'))}") for day in d.days]
        ),
        t("<b>Downloads per hour</b> (last 24 h)"),
        "<pre>" + spark([delivered(h.downloads) for h in d.hours]) + "</pre>",
    ]
    if d.platforms:
        lines.append(t("<b>Platforms</b> (7 days: delivered, success rate, failures)"))
        rows = sorted(d.platforms.items(), key=lambda item: -delivered(item[1]))
        table = []
        for platform, counts in rows:
            done = delivered(counts)
            ended = done + counts.get("fail", 0) + counts.get("uncertain", 0)
            table.append(
                f"{title(platform)[:11]:<11} {num(done):>6} {pct(done, ended):>5}  ❌{num(counts.get('fail'))}"
            )
        lines.append("<pre>" + escape("\n".join(table)) + "</pre>")
    return "\n".join(lines), kb(nav("dl"))


@dataclass
class QueueData:
    now: float
    live: list[dict]
    queued: list[ops.QueuedTask]
    summary: dict[str, int]
    in_flight: int
    nodes: list[dict]
    profiles: dict[int, stats.Profile]


def collect_queue() -> QueueData:
    live = stats.live_downloads()
    queued = ops.queued(LIST_LIMIT)
    users = {item.get("user") for item in live} | {task.user for task in queued}
    profiles = {u: stats.profile(int(u)) for u in users if u}
    return QueueData(time.time(), live, queued, ops.queue_summary(), ops.in_flight(), stats.nodes(), profiles)


def _who(d: QueueData, user) -> str:
    p = d.profiles.get(user)
    return person(user, p.name, p.username) if p else person(user)


def _live_line(item: dict) -> str:
    stage = item.get("stage", "")
    done, total = item.get("done") or 0, item.get("total")
    if stage == "download" and item.get("unit") == "bytes":
        amount = f"{size(done)}/{size(total)}" if total else size(done)
        share = f" {pct(done, total)}" if total else ""
        speed = f" · {size(item['speed'])}/s" if item.get("speed") else ""
        extra = (
            t(" · ⏸ no data for {time}", time=span(item["stalled"]))
            if (item.get("stalled") or 0) >= 15
            else ""
        )
        return f"⬇️{share} · {amount}{speed}{extra}"
    if stage == "download":
        return t("⬇️ {done} of {total} files", done=num(done), total=num(total) if total else "?")
    if stage == "upload":
        return t("📤 uploading {size}", size=size(item.get("size")))
    if stage in {"merge", "convert", "finalize"}:
        return t("🎞 processing ({stage})", stage=stage)
    return t("⏳ preparing")


def queue(d: QueueData) -> tuple[str, InlineKeyboardMarkup]:
    s = get_settings()
    online = workers(d.nodes, d.now)
    capacity = len(online) * s.max_parallel_downloads
    waiting = sum(d.summary.values())
    lines = [
        t("⚙️ <b>Download queue</b>"),
        "",
        t(
            "⬇️ Running: {running} of {capacity} slots ({workers} workers × {per})",
            running=num(len(d.live)),
            capacity=num(capacity),
            workers=num(len(online)),
            per=s.max_parallel_downloads,
        ),
        t(
            "⏳ Waiting: {waiting} ({downloads} downloads, {steps} batch steps)",
            waiting=num(waiting),
            downloads=num(d.summary.get("download_media")),
            steps=num(d.summary.get("advance_batch")),
        ),
        t("📬 Handed to workers, not finished: {count}", count=num(d.in_flight)),
    ]
    rows: list[list[Btn]] = []
    if d.live:
        lines += ["", t("<b>Running now</b>")]
        for i, item in enumerate(d.live[:LIST_LIMIT], 1):
            name = escape(item.get("title") or "")[:48]
            lines.append(
                f"{i}. {title(item.get('platform', ''))} · {escape(str(item.get('fmt', '')))} · {_who(d, item.get('user'))}"
            )
            lines.append(f"   {name}")
            elapsed = span(d.now - item.get("started", d.now))
            retries = t(" · 🔁 {n} retries", n=item["retries"]) if item.get("retries") else ""
            lines.append(f"   {_live_line(item)} · ⏱ {elapsed}{retries}")
        rows.append([btn(f"✖️ {i}", "kill", item["id"]) for i, item in enumerate(d.live[:LIST_LIMIT], 1)])
    if d.queued:
        lines += ["", t("<b>Next in queue</b>")]
        for i, task in enumerate(d.queued, 1):
            if task.name == "download_media":
                batch = t(" · from a batch") if task.batch else ""
                lines.append(
                    f"Q{i}. {title(task.platform)} · {escape(task.fmt)} · {_who(d, task.user)}{batch}"
                )
            else:
                lines.append(f"Q{i}. {escape(task.name)}")
        cancellable = [(i, task) for i, task in enumerate(d.queued, 1) if task.name == "download_media"]
        if cancellable:
            rows.append([btn(f"✖️ Q{i}", "kill", task.id) for i, task in cancellable])
    if not d.live and not waiting:
        lines += ["", t("😴 Nothing is running or waiting.")]
    return "\n".join(lines), kb(*rows, nav("queue"))


def confirm_cancel(target: dict) -> tuple[str, InlineKeyboardMarkup]:
    text = "\n".join(
        [
            t("✖️ <b>Cancel this download?</b>"),
            "",
            f"{title(target.get('platform', ''))} · {escape(str(target.get('fmt', '')))} · {person(target.get('user'))}",
            escape(target.get("title") or ""),
            "",
            t("The user sees that it was cancelled and can request it again."),
        ]
    )
    return text, kb([btn(t("✖️ Yes, cancel it"), "killok", target["id"]), btn(t("⬅️ Back"), "queue")])


@dataclass
class SystemData:
    now: float
    nodes: list[dict]
    redis: dict | None


def collect_system() -> SystemData:
    try:
        redis = ops.redis_info()
    except Exception:
        redis = None
    return SystemData(time.time(), stats.nodes(), redis)


def _node(n: dict, now: float) -> list[str]:
    head = t(
        "<b>{role}</b> · {host} · up {uptime}",
        role=escape(n.get("role", "?")),
        host=escape(str(n.get("host", ""))[:16]),
        uptime=span(now - n.get("started", now)),
    )
    late = now - n.get("at", 0)
    if late >= NODE_STALE:
        head += t(" · ⚠️ no heartbeat for {time}", time=span(late))
    lines = [head]
    mem, limit = n.get("mem"), n.get("mem_max")
    memory = (
        f"{size(mem)} / {size(limit)} ({pct(mem, limit)})" if mem and limit else size(mem or n.get("rss"))
    )
    lines.append(t("   🧠 RAM {memory} · process {rss}", memory=memory, rss=size(n.get("rss"))))
    cpu = f"{n['cpu']:.0f}%" if n.get("cpu") is not None else "—"
    load = " ".join(f"{v:.2f}" for v in n["load"]) if n.get("load") else "—"
    cpus = n.get("cpus")
    lines.append(
        t(
            "   ⚙️ CPU {cpu} of one core ({cpus} available) · load {load}",
            cpu=cpu,
            cpus=f"{cpus:g}" if cpus else "?",
            load=load,
        )
    )
    if n.get("disk"):
        disk, kept = n["disk"], n.get("retained") or {}
        lines.append(
            t(
                "   💾 Disk {free} free of {total} ({used} used) · kept {items} files ({kept})",
                free=size(disk["free"]),
                total=size(disk["total"]),
                used=pct(disk["used"], disk["total"]),
                items=num(kept.get("items")),
                kept=size(kept.get("bytes")),
            )
        )
    return lines


def system(d: SystemData) -> tuple[str, InlineKeyboardMarkup]:
    s = get_settings()
    lines = [t("🖥 <b>System</b>"), ""]
    if d.nodes:
        for n in d.nodes:
            lines += _node(n, d.now)
    else:
        lines.append(t("⚠️ No bot or worker heartbeat in the last 90 s."))
    if not workers(d.nodes, d.now):
        lines.append(t("⚠️ <b>No worker is online</b>: downloads wait in the queue."))
    lines.append("")
    r = d.redis
    if r:
        hits, misses = r.get("hits") or 0, r.get("misses") or 0
        limit = f" / {size(r['maxmemory'])}" if r.get("maxmemory") else ""
        lines += [
            t(
                "<b>Redis</b> {version} · up {uptime}",
                version=escape(str(r.get("version"))),
                uptime=span(r.get("uptime")),
            ),
            t(
                "   🧠 {memory}{limit} (peak {peak}) · 🔑 {keys} keys",
                memory=size(r.get("memory")),
                limit=limit,
                peak=size(r.get("peak")),
                keys=num(r.get("keys")),
            ),
            t(
                "   ⚡️ {ops} ops/s · {clients} clients · AOF {aof} · cache hit rate {hits}",
                ops=num(r.get("ops")),
                clients=num(r.get("clients")),
                aof=t("on") if r.get("aof") else t("off"),
                hits=pct(hits, hits + misses),
            ),
        ]
    else:
        lines.append(t("⚠️ Redis is unreachable."))
    enabled = ", ".join(p.title for p in PLATFORMS) or "—"
    lines += [
        "",
        t("<b>Settings</b>"),
        t("Platforms: {platforms}", platforms=escape(enabled)),
        t(
            "Downloads per worker {parallel} · connections per file {connections} · retries {retries} · stall timeout {timeout}s",
            parallel=s.max_parallel_downloads,
            connections=s.download_connections,
            retries=s.download_retries,
            timeout=f"{s.download_timeout:g}",
        ),
        t(
            "Max file {file} MB · queue expiry {queue} · task limit {task} · lookups at once {slots}",
            file=s.max_file_mb,
            queue=span(s.queue_ttl),
            task=span(s.task_time_limit),
            slots=s.extract_slots,
        ),
        t(
            "Required channels {channels} · admins {admins} · bot uptime {uptime}",
            channels=len(s.force_join_channel_id),
            admins=len(s.admin_ids),
            uptime=span(d.now - health.STARTED),
        ),
    ]
    return "\n".join(lines), kb(nav("sys"))


def top(rows: list[tuple[stats.Profile, int]]) -> tuple[str, InlineKeyboardMarkup]:
    lines = [t("🏆 <b>Top users</b> (delivered downloads, all time)"), ""]
    if not rows:
        lines.append(t("No downloads recorded yet."))
    for i, (p, count) in enumerate(rows, 1):
        lines.append(f"{i}. {person(p.id, p.name, p.username)} · {num(count)}")
    buttons = [btn(str(i), "u", p.id) for i, (p, _) in enumerate(rows, 1)]
    return "\n".join(lines), kb(buttons[:5], buttons[5:10], nav("top"))


def errors(items: list[dict], now: float) -> tuple[str, InlineKeyboardMarkup]:
    lines = [t("⚠️ <b>Recent failures</b>"), ""]
    if not items:
        lines.append(t("No failures recorded. 🎉"))
    for item in items:
        mark = "❔" if item.get("o") == "uncertain" else "❌"
        lines.append(
            f"{mark} {ago(item.get('t'), now)} · {title(item.get('p', ''))} · {person(item.get('u'))}"
        )
        if item.get("e"):
            lines.append(f"   <i>{escape(item['e'][:160])}</i>")
    return "\n".join(lines), kb(nav("err"))


def user_card(p: stats.Profile, now: float, running: dict | None = None) -> tuple[str, InlineKeyboardMarkup]:
    if not p.known:
        return t("🔎 No user with ID <code>{id}</code> has used the bot.", id=p.id), kb(
            [btn(t("🔎 Find another"), "find"), btn(t("⬅️ Back"), "home")]
        )
    status = []
    if p.banned:
        status.append(t("⛔ banned"))
    if p.blocked:
        status.append(t("🚫 blocked the bot"))
    lines = [
        t("👤 <b>User</b> {who}", who=person(p.id, p.name, p.username)),
        t('<a href="tg://user?id={id}">Open profile</a>', id=p.id),
        "",
        t("🆕 First seen: {time}", time=stamp(p.first)),
        t("🟢 Last seen: {time} ({ago})", time=stamp(p.last), ago=ago(p.last, now)),
        t("🌐 Telegram language: {lang}", lang=escape(p.language or "—")),
        t("📥 Delivered downloads: {count}", count=num(p.downloads)),
    ]
    if running:
        lines.append(
            t(
                "⬇️ Downloading now: {platform} · {fmt}",
                platform=title(running.get("platform", "")),
                fmt=escape(str(running.get("fmt", ""))),
            )
        )
    if status:
        lines.append(" · ".join(status))
    ban = btn(t("✅ Unban"), "unban", p.id) if p.banned else btn(t("⛔ Ban"), "ban", p.id)
    return "\n".join(lines), kb(
        [ban, btn(t("✉️ Message"), "msg", p.id)],
        [btn(t("🔄 Refresh"), "u", p.id), btn(t("⬅️ Back"), "home")],
    )


def prompt(text: str) -> tuple[str, InlineKeyboardMarkup]:
    return text, kb([btn(t("✖️ Cancel"), "home")])


AUDIENCES = {"all": None, "30d": 30 * 86400, "7d": 7 * 86400, "24h": 86400}


def broadcast_menu(counts: dict[str, int], running: bool) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        t("📣 <b>Broadcast</b>"),
        "",
        t("Choose who receives the message. Users who blocked the bot or are banned are skipped."),
    ]
    if running:
        lines += ["", t("⚠️ A broadcast is already running; wait for it to finish or stop it.")]
        return "\n".join(lines), kb([btn(t("⏹ Stop broadcast"), "bcstop")], [btn(t("⬅️ Back"), "home")])
    labels = {
        "all": t("Everyone ({n})"),
        "30d": t("Active in 30 days ({n})"),
        "7d": t("Active in 7 days ({n})"),
        "24h": t("Active in 24 hours ({n})"),
    }
    rows = [[btn(labels[name].format(n=num(counts.get(name))), "bca", name)] for name in AUDIENCES]
    return "\n".join(lines), kb(*rows, [btn(t("⬅️ Back"), "home")])


def broadcast_confirm(audience: str, count: int) -> tuple[str, InlineKeyboardMarkup]:
    text = t(
        "📣 Send the message above to <b>{count}</b> users ({audience})?\n\nIt is copied as is, without a forward header. About {time} at 20 messages per second.",
        count=num(count),
        audience=escape(audience),
        time=span(count / 20),
    )
    return text, kb([btn(t("📣 Send"), "bcgo"), btn(t("✖️ Cancel"), "home")])


def broadcast_progress(b) -> tuple[str, InlineKeyboardMarkup]:
    done = b.sent + b.failed + b.blocked
    state = t("✅ <b>Broadcast finished</b>") if b.finished else t("📣 <b>Broadcasting…</b>")
    if b.finished and b.stopped:
        state = t("⏹ <b>Broadcast stopped</b>")
    lines = [
        state,
        "",
        t(
            "Progress: {done} of {total} ({share})",
            done=num(done),
            total=num(b.total),
            share=pct(done, b.total),
        ),
        t(
            "✅ Delivered {sent} · 🚫 blocked {blocked} · ❌ failed {failed}",
            sent=num(b.sent),
            blocked=num(b.blocked),
            failed=num(b.failed),
        ),
        "⏱ " + span(time.monotonic() - b.started),
    ]
    markup = kb([btn(t("⬅️ Back"), "home")]) if b.finished else kb([btn(t("⏹ Stop broadcast"), "bcstop")])
    return "\n".join(lines), markup


def maintenance(notice: str | None) -> tuple[str, InlineKeyboardMarkup]:
    if notice:
        text = "\n".join(
            [
                t("🚧 <b>Maintenance mode is on</b>"),
                "",
                t("Users get this notice instead of the bot; admins keep full access:"),
                f"<blockquote>{escape(notice)}</blockquote>",
                t("Downloads already queued keep running."),
            ]
        )
        return text, kb([btn(t("✅ Turn off"), "mntoff")], [btn(t("⬅️ Back"), "home")])
    text = "\n".join(
        [
            t("🚧 <b>Maintenance mode is off</b>"),
            "",
            t(
                "While it is on, users only get a notice (the bot ignores their links); admins keep full access."
            ),
        ]
    )
    return text, kb(
        [btn(t("🚧 Turn on with the default notice"), "mnton")],
        [btn(t("✏️ Turn on with my own notice"), "mntown")],
        [btn(t("⬅️ Back"), "home")],
    )
