"""ShareWorker: the share_accept copy job (recipe-sharing design, Part 3).

copy_shared_item and finish_recipe_share are emulated over the fake's rows as the database
plan defines them (tests/unit/share_fakes.py); Cayenne's CI probes prove the functions. These
tests prove the worker's half: the order, the ids, the picture, and that a crash costs nothing.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import pytest

from recipeparser.adapters.share_worker import SHARE_NAMESPACE, ShareWorker, bucket_key, copy_id
from tests.unit.share_fakes import (
    PUBLIC_PREFIX,
    SUPABASE_URL,
    FakeImageStore,
    FakeSupabase,
    emulate_copy,
    emulate_finish,
)

SENDER = "11111111-1111-4111-8111-111111111111"
RECIPIENT = "22222222-2222-4222-8222-222222222222"
SHARE = "44444444-4444-4444-8444-444444444444"
JOB = "66666666-6666-4666-8666-666666666666"
LONG_AGO = "2000-01-01T00:00:00+00:00"


def _item(n: int, source: str, status: str = "pending") -> Dict[str, Any]:
    return {"id": f"99999999-9999-4999-8999-00000000000{n}", "share_id": SHARE, "sender_id": SENDER,
            "recipient_id": RECIPIENT, "source_recipe_id": source, "status": status,
            "position": n, "copied_recipe_id": None}


def _recipe(rid: str, owner: str = SENDER, **extra: Any) -> Dict[str, Any]:
    return {"id": rid, "user_id": owner, "title": f"Recipe {rid[-1]}", "image_url": None,
            "image_source": None, "copied_from_recipe_id": None, **extra}


def _seed(fake: FakeSupabase, items: List[Dict[str, Any]], recipes: List[Dict[str, Any]], *,
          job_status: str = "pending", updated_at: str = LONG_AGO) -> None:
    fake.tables["recipe_shares"] = [{"id": SHARE, "sender_id": SENDER, "recipient_id": RECIPIENT,
                                     "status": "accepting", "accepted_count": 0}]
    fake.tables["recipe_share_items"] = items
    fake.tables["recipes"] = recipes
    fake.tables["ingestion_jobs"] = [{"id": JOB, "user_id": RECIPIENT, "kind": "share_accept",
                                      "status": job_status, "stage": "IDLE", "params": {"share_id": SHARE},
                                      "created_at": LONG_AGO, "updated_at": updated_at}]
    fake.rpcs["copy_shared_item"] = emulate_copy(fake)
    fake.rpcs["finish_recipe_share"] = emulate_finish(fake)


def _worker(fake: FakeSupabase, images: Optional[FakeImageStore] = None) -> ShareWorker:
    return ShareWorker(fake, images or FakeImageStore(), supabase_url=SUPABASE_URL)


def _job(fake: FakeSupabase) -> Dict[str, Any]:
    return fake.tables["ingestion_jobs"][0]


def _run_to_end(worker: ShareWorker, fake: FakeSupabase, limit: int = 20) -> int:
    for polls in range(1, limit + 1):
        worker.run_once()
        if _job(fake)["status"] in ("done", "error"):
            return polls
    raise AssertionError(f"the job did not finish in {limit} polls")


def _status(fake: FakeSupabase) -> List[str]:
    return [i["status"] for i in sorted(fake.tables["recipe_share_items"], key=lambda i: i["position"])]


def _copies(fake: FakeSupabase) -> List[Dict[str, Any]]:
    return [r for r in fake.tables["recipes"] if r["user_id"] == RECIPIENT]


A, B, C = ("55555555-5555-4555-8555-00000000000a", "55555555-5555-4555-8555-00000000000b",
           "55555555-5555-4555-8555-00000000000c")


# ── the copy ─────────────────────────────────────────────────────────────────

def test_a_share_of_three_copies_one_marks_a_deleted_original_and_a_duplicate():
    # The spec's case: one original deleted since sending, one already received from an
    # earlier share, one to copy.
    fake = FakeSupabase()
    earlier = _recipe("77777777-7777-4777-8777-777777777777", owner=RECIPIENT, copied_from_recipe_id=C)
    _seed(fake, [_item(1, A), _item(2, B), _item(3, C)], [_recipe(A), _recipe(C), earlier])
    _run_to_end(_worker(fake), fake)

    assert _status(fake) == ["accepted", "unavailable", "duplicate"]
    new = [r for r in _copies(fake) if r["copied_from_recipe_id"] == A]
    assert [r["id"] for r in new] == [copy_id(_item(1, A)["id"])]
    assert fake.tables["recipe_shares"][0]["status"] == "accepted"
    job = _job(fake)
    assert (job["status"], job["stage"], job["progress_pct"]) == ("done", "DONE", 100)
    assert job["recipe_count"] == 2            # accepted + duplicate, as accepted_count counts


def test_the_copy_id_is_uuid5_of_the_item_and_fixed():
    item_id = _item(1, A)["id"]
    assert copy_id(item_id) == str(uuid.uuid5(SHARE_NAMESPACE, item_id))
    assert SHARE_NAMESPACE == uuid.UUID("fdcd01df-db30-423b-91de-7586d4ec7ea2")


def test_one_item_per_poll_in_position_order():
    fake = FakeSupabase()
    _seed(fake, [_item(2, B), _item(1, A)], [_recipe(A), _recipe(B)])
    worker = _worker(fake)
    worker.run_once()
    assert _status(fake) == ["accepted", "pending"]
    assert _job(fake)["progress_pct"] == 50
    worker.run_once()
    assert _status(fake) == ["accepted", "accepted"]
    assert _job(fake)["status"] == "running"
    worker.run_once()
    assert _job(fake)["status"] == "done"


def test_skipped_items_are_not_copied_or_counted():
    fake = FakeSupabase()
    _seed(fake, [_item(1, A, status="skipped"), _item(2, B)], [_recipe(A), _recipe(B)])
    worker = _worker(fake)
    worker.run_once()
    assert _job(fake)["progress_pct"] == 99    # one of one taken, held below 100 until done
    assert [p["p_item"] for p in fake.calls("copy_shared_item")] == [_item(2, B)["id"]]
    assert _status(fake) == ["skipped", "accepted"]


def test_a_copy_that_fails_twice_fails_its_item_and_the_rest_go_on():
    fake = FakeSupabase()
    _seed(fake, [_item(1, A), _item(2, B)], [_recipe(A), _recipe(B)])
    real = fake.rpcs["copy_shared_item"]

    def flaky(p):
        if p["p_item"] == _item(1, A)["id"]:
            raise RuntimeError("statement timeout")
        return real(p)

    fake.rpcs["copy_shared_item"] = flaky
    _run_to_end(_worker(fake), fake)
    assert _status(fake) == ["failed", "accepted"]
    assert len([p for p in fake.calls("copy_shared_item") if p["p_item"] == _item(1, A)["id"]]) == 2
    assert _job(fake)["status"] == "done"


def test_a_copy_that_fails_once_is_retried_and_lands():
    fake = FakeSupabase()
    _seed(fake, [_item(1, A)], [_recipe(A)])
    real, failures = fake.rpcs["copy_shared_item"], [RuntimeError("connection reset")]

    def once(p):
        if failures:
            raise failures.pop()
        return real(p)

    fake.rpcs["copy_shared_item"] = once
    _run_to_end(_worker(fake), fake)
    assert _status(fake) == ["accepted"]


# ── claiming, resuming, and what a crash costs ───────────────────────────────

def test_a_job_killed_after_the_first_item_resumes_with_one_copy_per_item():
    fake = FakeSupabase()
    _seed(fake, [_item(1, A), _item(2, B), _item(3, C)], [_recipe(A), _recipe(B), _recipe(C)])
    _worker(fake).run_once()                            # claims and copies item 1, then "dies"
    assert _status(fake) == ["accepted", "pending", "pending"]
    # It died mid-way through item 2: marked copying, no row yet. Its lease lapses.
    fake.tables["recipe_share_items"][1]["status"] = "copying"
    _job(fake)["updated_at"] = LONG_AGO

    _run_to_end(_worker(fake), fake)                    # a fresh worker, as after a restart
    assert _status(fake) == ["accepted", "accepted", "accepted"]
    assert sorted(r["id"] for r in _copies(fake)) == sorted(copy_id(i["id"]) for i in fake.tables["recipe_share_items"])
    assert fake.tables["recipe_shares"][0]["accepted_count"] == 3


def test_a_redo_whose_copy_already_exists_makes_no_second_row():
    # The function's own redo guard: with a copy already under the item's id, a second call
    # records the item and makes no second row.
    fake = FakeSupabase()
    item = _item(1, A, status="copying")
    done = _recipe(copy_id(item["id"]), owner=RECIPIENT, copied_from_recipe_id=A)
    _seed(fake, [item], [_recipe(A), done], job_status="running")
    _run_to_end(_worker(fake), fake)
    assert len(_copies(fake)) == 1
    assert _status(fake) == ["accepted"]


def test_a_live_running_job_is_not_reclaimed():
    fake = FakeSupabase()
    _seed(fake, [_item(1, A)], [_recipe(A)], job_status="running", updated_at="2999-01-01T00:00:00+00:00")
    assert _worker(fake).run_once() == 0
    assert fake.calls("copy_shared_item") == []


def test_no_job_is_zero_so_the_loop_sleeps():
    fake = FakeSupabase()
    _seed(fake, [], [])
    fake.tables["ingestion_jobs"] = []
    assert _worker(fake).run_once() == 0


def test_a_recategorise_job_is_not_taken():
    fake = FakeSupabase()
    _seed(fake, [_item(1, A)], [_recipe(A)])
    _job(fake)["kind"] = "recategorize"
    assert _worker(fake).run_once() == 0


def test_a_share_that_no_longer_exists_ends_the_job_in_error():
    fake = FakeSupabase()
    _seed(fake, [_item(1, A)], [_recipe(A)])
    fake.tables["recipe_shares"] = []
    _worker(fake).run_once()
    job = _job(fake)
    assert (job["status"], job["stage"]) == ("error", "ERROR")
    assert job["error_message"] == "The share no longer exists"


def test_a_database_failure_leaves_the_job_running_for_the_lease():
    fake = FakeSupabase()
    _seed(fake, [_item(1, A)], [_recipe(A)])
    fake.raises[("recipe_share_items", "select")] = RuntimeError("connection refused")
    worker = _worker(fake)
    assert worker.run_once() == 0                       # idle, so run_workers still sleeps
    assert _job(fake)["status"] == "running"            # not error: the share would be stranded
    del fake.raises[("recipe_share_items", "select")]
    _job(fake)["updated_at"] = LONG_AGO
    _run_to_end(worker, fake)
    assert _status(fake) == ["accepted"]


def test_a_database_that_cannot_be_reached_is_an_idle_poll():
    # run_workers skips its sleep after a busy poll (keeps_loop_busy). A poll that failed
    # must read as idle, or an outage spins the loop and starves the other workers.
    fake = FakeSupabase()
    _seed(fake, [_item(1, A)], [_recipe(A)])
    fake.raises[("ingestion_jobs", "select")] = RuntimeError("connection refused")
    assert _worker(fake).run_once() == 0


# ── the picture ──────────────────────────────────────────────────────────────

def test_a_bucket_picture_is_stored_again_under_the_copys_id():
    fake = FakeSupabase()
    images = FakeImageStore()
    fake.objects[f"{A}.png"] = b"png-bytes"
    original = _recipe(A, image_url=f"{PUBLIC_PREFIX}{A}.png?v=1727700000", image_source="generated")
    _seed(fake, [_item(1, A)], [original])
    _run_to_end(_worker(fake, images), fake)

    new_id = copy_id(_item(1, A)["id"])
    assert images.puts == [(b"png-bytes", new_id, "image/png")]
    copy = _copies(fake)[0]
    assert copy["image_url"] == f"{PUBLIC_PREFIX}{new_id}.jpg"
    assert copy["image_source"] == "generated"      # the marker travels with the picture


def test_the_copys_picture_survives_the_sender_replacing_theirs():
    # D11: the copy points at its own object, so the sender's next picture cannot reach it.
    fake = FakeSupabase()
    fake.objects[f"{A}.jpg"] = b"jpeg-bytes"
    _seed(fake, [_item(1, A)], [_recipe(A, image_url=f"{PUBLIC_PREFIX}{A}.jpg")])
    _run_to_end(_worker(fake), fake)
    fake.objects[f"{A}.jpg"] = b"the-senders-new-picture"
    copy = _copies(fake)[0]
    assert bucket_key(copy["image_url"], SUPABASE_URL) != f"{A}.jpg"


def test_a_picture_outside_the_bucket_is_copied_as_it_is():
    fake = FakeSupabase()
    images = FakeImageStore()
    _seed(fake, [_item(1, A)], [_recipe(A, image_url="https://cdn.example.com/soup.jpg")])
    _run_to_end(_worker(fake, images), fake)
    assert images.puts == []
    assert _copies(fake)[0]["image_url"] == "https://cdn.example.com/soup.jpg"


def test_a_picture_that_cannot_be_read_falls_back_to_no_picture():
    fake = FakeSupabase()
    _seed(fake, [_item(1, A)], [_recipe(A, image_url=f"{PUBLIC_PREFIX}{A}.jpg", image_source="generated")])
    _run_to_end(_worker(fake), fake)                # the object is missing from the bucket
    copy = _copies(fake)[0]
    assert (copy["image_url"], copy["image_source"]) == (None, None)
    assert _status(fake) == ["accepted"]


def test_a_picture_that_cannot_be_stored_falls_back_to_no_picture():
    fake = FakeSupabase()
    fake.objects[f"{A}.jpg"] = b"jpeg-bytes"
    _seed(fake, [_item(1, A)], [_recipe(A, image_url=f"{PUBLIC_PREFIX}{A}.jpg")])
    _run_to_end(_worker(fake, FakeImageStore(fail=True)), fake)
    assert _copies(fake)[0]["image_url"] is None
    assert _status(fake) == ["accepted"]


def test_no_picture_is_stored_for_a_recipe_already_received():
    fake = FakeSupabase()
    images = FakeImageStore()
    fake.objects[f"{A}.jpg"] = b"jpeg-bytes"
    earlier = _recipe("77777777-7777-4777-8777-777777777777", owner=RECIPIENT, copied_from_recipe_id=A)
    _seed(fake, [_item(1, A)], [_recipe(A, image_url=f"{PUBLIC_PREFIX}{A}.jpg"), earlier])
    _run_to_end(_worker(fake, images), fake)
    assert images.puts == []
    assert _status(fake) == ["duplicate"]


def test_a_picture_stored_for_an_original_deleted_meanwhile_is_removed():
    # The original was there when its picture was copied, and gone by the copy itself.
    fake = FakeSupabase()
    images = FakeImageStore()
    fake.objects[f"{A}.jpg"] = b"jpeg-bytes"
    _seed(fake, [_item(1, A)], [_recipe(A, image_url=f"{PUBLIC_PREFIX}{A}.jpg")])
    real = fake.rpcs["copy_shared_item"]

    def deleted_meanwhile(p):
        fake.tables["recipes"] = [r for r in fake.tables["recipes"] if r["id"] != A]
        return real(p)

    fake.rpcs["copy_shared_item"] = deleted_meanwhile
    _run_to_end(_worker(fake, images), fake)
    assert _status(fake) == ["unavailable"]
    assert images.removed == [copy_id(_item(1, A)["id"])]


def test_a_picture_stored_for_an_item_that_fails_is_removed():
    fake = FakeSupabase()
    images = FakeImageStore()
    fake.objects[f"{A}.jpg"] = b"jpeg-bytes"
    _seed(fake, [_item(1, A)], [_recipe(A, image_url=f"{PUBLIC_PREFIX}{A}.jpg")])

    def down(p):
        raise RuntimeError("statement timeout")

    fake.rpcs["copy_shared_item"] = down
    _run_to_end(_worker(fake, images), fake)
    assert _status(fake) == ["failed"]
    assert images.removed == [copy_id(_item(1, A)["id"])]


def test_a_copied_picture_is_kept():
    fake = FakeSupabase()
    images = FakeImageStore()
    fake.objects[f"{A}.jpg"] = b"jpeg-bytes"
    _seed(fake, [_item(1, A)], [_recipe(A, image_url=f"{PUBLIC_PREFIX}{A}.jpg")])
    _run_to_end(_worker(fake, images), fake)
    assert images.removed == []


@pytest.mark.parametrize("url,key", [
    (f"{PUBLIC_PREFIX}abc.jpg", "abc.jpg"),
    (f"{PUBLIC_PREFIX}abc.png?v=12", "abc.png"),
    ("https://cdn.example.com/abc.jpg", None),
    (f"{SUPABASE_URL}/storage/v1/object/public/other-bucket/abc.jpg", None),
    (None, None),
])
def test_bucket_key(url, key):
    assert bucket_key(url, SUPABASE_URL) == key
