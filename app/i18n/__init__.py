"""User-interface languages.

Texts are written in English in the code and translated by `t()`, gettext style: the English text
is the key of each language's catalog (``app/i18n/fa.py``), so the code stays readable and a
missing translation falls back to English instead of breaking a message.

The active language lives in a context variable. The bot sets it per update from the user's
choice (`LanguageMiddleware`); the worker sets it per task from the language captured when the
download was queued. Each user's choice is stored in Redis without expiry.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

from app.i18n import fa

log = logging.getLogger(__name__)

# code -> (flag, native name). The first entry is listed first in /language.
LANGUAGES = {"fa": ("🇮🇷", "فارسی"), "en": ("🇬🇧", "English")}
CATALOGS = {"fa": fa.TEXTS}
RTL = {"fa"}

_current: ContextVar[str | None] = ContextVar("ui_language", default=None)
missing: set[str] = set()  # English texts without a translation, for tests and logs


def default() -> str:
    from app.config import get_settings

    return get_settings().default_language


def current() -> str:
    return _current.get() or default()


def use(lang: str | None) -> str:
    """Make `lang` (or the default for unknown values) the language of the current context."""
    lang = lang if lang in LANGUAGES else default()
    _current.set(lang)
    return lang


def t(text: str, /, **values) -> str:
    """Translate an English text into the current language, then fill in `{placeholders}`."""
    lang = current()
    if lang != "en":
        catalog = CATALOGS.get(lang, {})
        translated = catalog.get(text)
        if translated is None and text in _translations(lang):
            translated = text  # already translated (e.g. a translated text wrapped in an error)
        if translated is None:
            if text not in missing:
                missing.add(text)
                log.warning("missing %s translation: %r", lang, text[:80])
        else:
            text = translated
    return text.format(**values) if values else text


_values: dict[str, frozenset[str]] = {}


def _translations(lang: str) -> frozenset[str]:
    if lang not in _values:
        _values[lang] = frozenset(CATALOGS.get(lang, {}).values())
    return _values[lang]


def label(lang: str) -> str:
    flag, name = LANGUAGES[lang]
    return f"{flag} {name}"


# --- per-user choice (Redis, shared by bot and worker; cached in-process by the bot)

_cache: dict[int, str] = {}
CACHE_LIMIT = 50_000


def _key(user_id: int) -> str:
    from app.services.cache import namespace

    return f"uilang:{namespace()}:{user_id}"


def load(user_id: int) -> str:
    """The user's language (the default until they choose one). Blocking: call in a thread."""
    if (lang := _cache.get(user_id)) is not None:
        return lang
    from app.services.cache import client

    stored = client().get(_key(user_id))
    lang = stored if stored in LANGUAGES else default()
    _remember(user_id, lang)
    return lang


def save(user_id: int, lang: str) -> None:
    if lang not in LANGUAGES:
        raise ValueError(f"unknown language {lang!r}")
    from app.services.cache import client

    client().set(_key(user_id), lang)
    _remember(user_id, lang)


def _remember(user_id: int, lang: str) -> None:
    if len(_cache) >= CACHE_LIMIT:
        _cache.clear()  # crude bound; misses just read Redis again
    _cache[user_id] = lang
