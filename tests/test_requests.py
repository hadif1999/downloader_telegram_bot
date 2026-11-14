import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.config import get_settings
from app.services import cache, requests
from app.services.delivery import Journal


def test_queue_lock_covers_queue_wait_and_execution_renews_owned_lease(request_store):
    assert requests.create("a", 10)
    assert request_store.ttl(requests.keys("a", 10)[2]) > get_settings().queue_ttl
    assert not requests.create("b", 10)
    assert requests.claim("a", 10, "owner-a") == 1
    assert requests.renew("a", 10, "owner-a")
    assert requests.claim("a", 10, "owner-b") == 0
    assert not requests.renew("a", 10, "owner-b")


def test_publication_refreshes_queue_lease_after_slow_card_edit(request_store):
    requests.create("a", 10)
    user = requests.keys("a", 10)[2]
    request_store.expire(user, 5)
    assert requests.prepare_queue("a", 10)
    assert request_store.ttl(user) > get_settings().queue_ttl
    request_store.set(user, "newer-request")
    assert not requests.prepare_queue("a", 10)
    assert request_store.get(user) == "newer-request"


def test_expired_owner_cannot_release_new_owners_lock(request_store):
    assert requests.create("a", 10)
    assert requests.claim("a", 10, "old") == 1
    request_store.delete(requests.keys("a", 10)[1])  # simulate expired execution lease
    assert requests.claim("a", 10, "new") == 1
    requests.release("a", 10, "old")
    assert request_store.get(requests.keys("a", 10)[1]) == "new"
    assert request_store.get(requests.keys("a", 10)[2]) == "a"


def test_expired_request_cannot_take_another_requests_user_lock(request_store):
    requests.create("a", 10)
    request_store.delete(requests.keys("a", 10)[2])
    assert requests.create("b", 10)
    assert requests.claim("a", 10, "old") == -1
    assert request_store.get(requests.keys("a", 10)[2]) == "b"


def test_concurrent_user_requests_are_atomic(request_store):
    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(lambda key: requests.create(key, 10), ["a", "b"]))
    assert sum(result) == 1


def test_concurrent_execution_has_one_owner(request_store):
    requests.create("a", 10)
    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(lambda owner: requests.claim("a", 10, owner), ["one", "two"]))
    assert sorted(result) == [0, 1]


def test_completed_delivery_is_not_claimed_again(request_store):
    requests.create("a", 10)
    requests.claim("a", 10, "owner")
    requests.transition("a", 10, "owner", "done", [["video", "fid"]], 1)
    requests.release("a", 10, "owner")
    assert requests.claim("a", 10, "new") == -1
    assert request_store.get(requests.keys("a", 10)[2]) is None


def test_crash_after_send_intent_becomes_uncertain_not_resent(request_store):
    requests.create("a", 10)
    requests.claim("a", 10, "owner")
    requests.transition("a", 10, "owner", "sending", [], 1)
    request_store.delete(requests.keys("a", 10)[1])
    assert requests.claim("a", 10, "new") == -1
    assert requests.load("a", 10)["phase"] == "uncertain"


def test_cancelled_queue_cannot_start_and_running_queue_cannot_cancel(request_store):
    requests.create("a", 10)
    assert requests.cancel_queued("a", 10)
    assert requests.claim("a", 10, "owner") == -1
    requests.create("b", 10)
    requests.claim("b", 10, "owner")
    assert not requests.cancel_queued("b", 10)


def test_journal_refuses_send_after_lost_ownership(request_store):
    requests.create("a", 10)
    requests.claim("a", 10, "old")
    request_store.set(requests.keys("a", 10)[1], "new")
    called = []

    async def send():
        called.append(True)

    with pytest.raises(requests.LeaseLost):
        asyncio.run(Journal("a", 10, "old").send(send))
    assert not called


def test_file_cache_is_scoped_by_bot_identity(request_store, monkeypatch):
    cache.save_file_id("yt", "id", "720", None, "old-id")
    monkeypatch.setattr(cache, "get_settings", lambda: type("Settings", (), {"bot_token": "999999:other"})())
    assert cache.get_file_id("yt", "id", "720", None) is None
