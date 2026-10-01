"""In-memory doubles for the recipe-sharing tests.

A table store shaped like the PostgREST calls this code makes, the database's functions as
handlers the test supplies, the storage bucket, and the image store. Stateful rather than
query-recording (test_recat_worker.py's shape), because what the copy job must get right is
what the rows end up as: a resumed job leaves exactly one copy per item, and only a store
that keeps rows can show that.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

SUPABASE_URL = "https://proj.supabase.co"
PUBLIC_PREFIX = f"{SUPABASE_URL}/storage/v1/object/public/recipe-images/"


class Result:
    def __init__(self, data: Any, count: Optional[int] = None) -> None:
        self.data, self.count = data, count


class Query:
    def __init__(self, fake: "FakeSupabase", table: str) -> None:
        self.fake, self.table = fake, table
        self.op = "select"
        self.payload: Any = None
        self.filters: List[Callable[[Dict[str, Any]], bool]] = []
        self.sort: Optional[Tuple[str, bool]] = None
        self.max_rows: Optional[int] = None

    def select(self, _columns: str = "*", count: Optional[str] = None) -> "Query":
        self.op = "select"
        return self

    def update(self, payload: Dict[str, Any]) -> "Query":
        self.op, self.payload = "update", payload
        return self

    def eq(self, column: str, value: Any) -> "Query":
        self.filters.append(lambda row, c=column, v=value: row.get(c) == v)
        return self

    def in_(self, column: str, values: List[Any]) -> "Query":
        allowed = list(values)
        self.filters.append(lambda row, c=column: row.get(c) in allowed)
        return self

    def lt(self, column: str, value: Any) -> "Query":
        self.filters.append(lambda row, c=column, v=value: row.get(c) is not None and row.get(c) < v)
        return self

    def order(self, column: str, desc: bool = False) -> "Query":
        self.sort = (column, desc)
        return self

    def limit(self, n: int) -> "Query":
        self.max_rows = n
        return self

    def execute(self) -> Result:
        self.fake.log.append((self.table, self.op, self.payload))
        failure = self.fake.raises.get((self.table, self.op))
        if failure is not None:
            raise failure
        rows = self.fake.tables.setdefault(self.table, [])
        hit = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
            return Result([dict(r) for r in hit])
        if self.sort is not None:
            column, desc = self.sort
            hit = sorted(hit, key=lambda r: r.get(column), reverse=desc)
        if self.max_rows is not None:
            hit = hit[: self.max_rows]
        return Result([dict(r) for r in hit])


class Rpc:
    def __init__(self, fake: "FakeSupabase", name: str, params: Dict[str, Any]) -> None:
        self.fake, self.name, self.params = fake, name, params

    def execute(self) -> Result:
        self.fake.rpc_calls.append((self.name, dict(self.params)))
        handler = self.fake.rpcs.get(self.name)
        if handler is None:
            raise AssertionError(f"the test gave no handler for rpc {self.name}")
        return Result(handler(self.params))


class _Bucket:
    def __init__(self, fake: "FakeSupabase") -> None:
        self.fake = fake

    def download(self, path: str) -> bytes:
        if path not in self.fake.objects:
            raise RuntimeError(f"Object not found: {path}")
        return self.fake.objects[path]


class _Storage:
    def __init__(self, fake: "FakeSupabase") -> None:
        self.fake = fake

    def from_(self, bucket: str) -> _Bucket:
        assert bucket == "recipe-images", bucket
        return _Bucket(self.fake)


class FakeSupabase:
    def __init__(self) -> None:
        self.tables: Dict[str, List[Dict[str, Any]]] = {}
        self.rpcs: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        # (table, op) -> exception that query's execute() raises.
        self.raises: Dict[Tuple[str, str], Exception] = {}
        self.objects: Dict[str, bytes] = {}
        self.log: List[Tuple[str, str, Any]] = []
        self.rpc_calls: List[Tuple[str, Dict[str, Any]]] = []
        self.storage = _Storage(self)

    def table(self, name: str) -> Query:
        return Query(self, name)

    def rpc(self, name: str, params: Dict[str, Any]) -> Rpc:
        return Rpc(self, name, params)

    def calls(self, name: str) -> List[Dict[str, Any]]:
        return [p for n, p in self.rpc_calls if n == name]


class FakeImageStore:
    """SupabaseImageStore.put's contract: the object's public URL, or None on any failure."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.puts: List[Tuple[bytes, str, str]] = []
        self.removed: List[str] = []

    def put(self, image_bytes: bytes, recipe_id: str, content_type: str = "image/jpeg") -> Optional[str]:
        self.puts.append((image_bytes, recipe_id, content_type))
        if self.fail:
            return None
        return f"{PUBLIC_PREFIX}{recipe_id}.jpg"

    def remove(self, recipe_id: str, keep: Optional[str] = None) -> None:
        self.removed.append(recipe_id)


def emulate_copy(fake: FakeSupabase) -> Callable[[Dict[str, Any]], str]:
    """copy_shared_item as the database plan's Task 4 defines it, over the fake's rows."""
    def copy(p: Dict[str, Any]) -> str:
        item = next(i for i in fake.tables["recipe_share_items"] if i["id"] == p["p_item"])
        if item["status"] not in ("pending", "copying"):
            return item["status"]
        share = next(s for s in fake.tables["recipe_shares"] if s["id"] == item["share_id"])
        recipes = fake.tables.setdefault("recipes", [])

        def settle(status: str, copied: Optional[str]) -> str:
            item.update(status=status, copied_recipe_id=copied)
            share["accepted_count"] += 1
            return status

        if any(r["id"] == p["p_new_id"] and r["user_id"] == item["recipient_id"] for r in recipes):
            return settle("accepted", p["p_new_id"])
        prior = next((r for r in recipes if r["user_id"] == item["recipient_id"]
                      and r.get("copied_from_recipe_id") == item["source_recipe_id"]), None)
        if prior is not None:
            return settle("duplicate", prior["id"])
        original = next((r for r in recipes if r["id"] == item["source_recipe_id"]
                         and r["user_id"] == item["sender_id"]), None)
        if original is None:
            item["status"] = "unavailable"
            return "unavailable"
        recipes.append({**original, "id": p["p_new_id"], "user_id": item["recipient_id"],
                        "copied_from_recipe_id": original["id"],
                        "image_url": p["p_image_url"], "image_source": p["p_image_source"]})
        return settle("accepted", p["p_new_id"])
    return copy


def emulate_finish(fake: FakeSupabase) -> Callable[[Dict[str, Any]], bool]:
    """finish_recipe_share: accepting -> accepted once no item is pending or copying."""
    def finish(p: Dict[str, Any]) -> bool:
        share = next((s for s in fake.tables["recipe_shares"] if s["id"] == p["p_share"]), None)
        open_items = [i for i in fake.tables["recipe_share_items"]
                      if i["share_id"] == p["p_share"] and i["status"] in ("pending", "copying")]
        if share is None or share["status"] != "accepting" or open_items:
            return False
        share["status"] = "accepted"
        return True
    return finish
