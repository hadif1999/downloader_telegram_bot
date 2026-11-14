"""Redact credentials and signed query strings, including exception tracebacks."""

import logging
import re
from urllib.parse import urlsplit, urlunsplit

TOKEN = re.compile(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{20,}\b")
URL = re.compile(r"(?:https?|rediss?)://[^\s<>\"']+")


def redact(text: str) -> str:
    def clean(match):
        try:
            url = urlsplit(match.group())
            return urlunsplit(
                (url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "REDACTED" if url.query else "", "")
            )
        except ValueError:
            return "[invalid URL]"

    return TOKEN.sub("[BOT_TOKEN]", URL.sub(clean, text))


class RedactingFormatter(logging.Formatter):
    def __init__(self, inner: logging.Formatter):
        super().__init__()
        self.inner = inner

    def format(self, record):
        return redact(self.inner.format(record))


def redact_logs(logger: logging.Logger | None = None, **kwargs) -> None:
    for handler in (logger or logging.getLogger()).handlers:
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(handler.formatter or logging.Formatter()))
