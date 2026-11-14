import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot import main as bot_main


@pytest.mark.parametrize("startup_fails", [False, True])
def test_bot_session_closes_even_when_startup_fails(monkeypatch, startup_fails):
    bot = MagicMock()
    bot.delete_webhook = AsyncMock(side_effect=RuntimeError("unavailable") if startup_fails else None)
    bot.session.close = AsyncMock()
    dispatcher = MagicMock()
    dispatcher.start_polling = AsyncMock()
    monkeypatch.setattr(bot_main, "make_bot", lambda: bot)
    monkeypatch.setattr(bot_main, "Dispatcher", lambda: dispatcher)

    if startup_fails:
        with pytest.raises(RuntimeError, match="unavailable"):
            asyncio.run(bot_main.main())
        dispatcher.start_polling.assert_not_awaited()
    else:
        asyncio.run(bot_main.main())
        assert dispatcher.start_polling.call_args.kwargs["close_bot_session"] is False
    bot.session.close.assert_awaited_once()
