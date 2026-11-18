"""Batch store invariants, against the isolated Redis (real Lua)."""

import pytest

from app.services import batches


def item(n):
    return batches.Item("yt", f"id{n:09d}", f"https://youtu.be/id{n:09d}", f"Video {n}", 60, "video")


@pytest.fixture
def batch(request_store):
    return batches.create(
        user=7,
        chat=1,
        card=50,
        reply_to=40,
        lang="en",
        items=[item(i) for i in range(4)],
        meta={},
        quality="720",
    )


def test_create_and_load(batch):
    b = batches.load(batch)
    assert (b.user, b.chat, b.card, b.reply_to, b.phase, b.cursor) == (7, 1, 50, 40, "draft", -1)
    assert b.chosen == [0, 1, 2, 3] and b.items[2].title == "Video 2" and b.quality == "720"


def test_only_the_owner_edits_a_draft(batch):
    assert batches.edit(batch, 8, "t", "1") == batches.NOT_OWNER
    assert batches.edit(batch, 7, "t", "1") == batches.OK
    assert batches.edit(batch, 7, "t", "9") == batches.EMPTY  # out of range
    assert batches.edit(batch, 7, "q", "mp3") == batches.OK
    b = batches.load(batch)
    assert b.chosen == [0, 2, 3] and b.quality == "mp3"
    batches.edit(batch, 7, "none")
    assert batches.start(batch, 7) == batches.EMPTY
    batches.edit(batch, 7, "all")
    assert batches.load(batch).chosen == [0, 1, 2, 3]
    assert batches.edit("missing", 7, "all") == batches.EXPIRED


def test_items_run_one_after_another_and_duplicates_are_ignored(batch):
    batches.edit(batch, 7, "t", "1")  # skip item 1
    assert batches.start(batch, 8) == batches.NOT_OWNER
    assert batches.start(batch, 7) == batches.OK
    assert batches.start(batch, 7) == batches.WRONG_PHASE
    assert batches.edit(batch, 7, "t", "1") == batches.WRONG_PHASE  # no edits once running
    assert batches.begin(batch, -1) == 0
    assert batches.begin(batch, -1) == batches.DUPLICATE  # e.g. a redelivered advance
    assert batches.begin(batch, 0) == 2
    assert batches.begin(batch, 0) == batches.DUPLICATE
    assert batches.begin(batch, 2) == 3
    assert batches.begin(batch, 3) == batches.FINISHED
    assert batches.load(batch).phase == "done"
    assert batches.begin(batch, 3) == batches.GONE  # the final summary is drawn only once


def test_busy_user_gives_the_item_back(batch):
    batches.start(batch, 7)
    assert batches.begin(batch, -1) == 0
    assert batches.release(batch, 0, -1)
    assert batches.begin(batch, -1) == 0  # the same item, later


def test_stop_lets_the_current_item_finish_then_ends(batch):
    batches.start(batch, 7)
    assert batches.begin(batch, -1) == 0
    assert batches.stop(batch, 8) == batches.NOT_OWNER
    assert batches.stop(batch, 7) == 1
    assert batches.load(batch).phase == "stopping"
    assert batches.begin(batch, 0) == batches.FINISHED
    assert batches.load(batch).phase == "stopped"


def test_cancel_a_draft(batch):
    assert batches.stop(batch, 7) == 2
    assert batches.start(batch, 7) == batches.WRONG_PHASE


def test_results_are_recorded_per_item(batch):
    batches.record(batch, 0, batches.SENT)
    batches.record(batch, 2, batches.FAILED, "⚠️ <b>gone</b> | really")
    b = batches.load(batch)
    assert b.results == {0: ("sent", ""), 2: ("failed", "⚠️ <b>gone</b> | really")}
    assert b.count(batches.SENT, batches.CACHED) == 1 and b.outcome(2) == "failed"


def test_running_batch_outlives_the_queue_ttl(batch, request_store):
    from app.config import get_settings

    assert request_store.ttl(batches.key(batch)) <= get_settings().info_cache_ttl
    batches.start(batch, 7)
    assert request_store.ttl(batches.key(batch)) > get_settings().queue_ttl
