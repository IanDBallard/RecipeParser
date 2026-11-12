"""
On demand: run the golden questions in ask_goldens.yaml through the Library's deeper-search agent
and write a report to read (Cayenne AI search section 10).

It runs the same flow as POST /search/ask (recipeparser/adapters/search_api.py): catalogue, tool box,
agent loop, ownership check, finalize. It is NOT a test. It calls Anthropic and Gemini and reads the
live library as the given cook, so it costs real money (roughly a few dollars for all 30). Run it
before the Library's launch and whenever the prompt changes. The report gives per question the kind
(checked against expect_kind), the recipe titles per group with the note and label, model calls,
seconds, tokens and the estimated cost. Judging the grouping and the voice is left to you.

    python scripts/ask_goldens.py --user <uuid> --out report.md
    python scripts/ask_goldens.py --user <uuid> --only menu-picnic --out one.md

Requires AI_READER_DATABASE_URL, ANTHROPIC_API_KEY and GOOGLE_API_KEY in the environment. The
database role is ai_reader and the cook is the --user uuid (claims sub + role authenticated).
A refinement entry (`previous: <id>`) is fed the earlier entry's question and finalized answer.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter

from recipeparser import usage as usage_module
from recipeparser.adapters.ai_search.agent import run_agent
from recipeparser.adapters.ai_search.ai_db import AiReaderDb
from recipeparser.adapters.ai_search.answer import Answer, Previous, finalize, raw_ids
from recipeparser.adapters.ai_search.prompt import request_text, system_blocks
from recipeparser.adapters.ai_search.tools import ToolBox

MODEL = "claude-opus-5-5"
DEFAULT_MEASURING = "metric, °C"
KINDS = {"recipes", "text", "none", "out_of_scope"}
# Dollars per million tokens.
INPUT_RATE = 4.0
OUTPUT_RATE = 20.0
CACHE_READ_RATE = 0.20
CACHE_WRITE_RATE = INPUT_RATE * 1.25
_TITLES_SQL = "select id::text, title from ai.recipes where id = any(%s::uuid[])"
_ANSWER: TypeAdapter[Answer] = TypeAdapter(Answer)


@dataclass(frozen=True)
class Golden:
    id: str
    question: str
    expect_kind: str | None = None
    filters: tuple[str, ...] = ()
    today: date | None = None
    previous: str | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    calls: int = 0

    def add(self, u: object) -> None:
        """Add one response's usage (uncached input, output, cache reads and writes)."""
        self.input_tokens += getattr(u, "input_tokens", None) or 0
        self.output_tokens += getattr(u, "output_tokens", None) or 0
        self.cache_read += getattr(u, "cache_read_input_tokens", None) or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", None) or 0
        self.calls += 1


@dataclass
class Result:
    id: str
    question: str
    expect_kind: str | None
    kind: str | None
    answer: dict[str, Any] | None
    titles: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0
    usage: Usage = field(default_factory=Usage)
    error: str | None = None


def estimate_cost(u: Usage) -> float:
    return (
        u.input_tokens * INPUT_RATE
        + u.output_tokens * OUTPUT_RATE
        + u.cache_read * CACHE_READ_RATE
        + u.cache_write * CACHE_WRITE_RATE
    ) / 1_000_000


def total_cost(results: list[Result]) -> float:
    return sum(estimate_cost(r.usage) for r in results)


def load_goldens(path: Path) -> list[Golden]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries: list[Golden] = []
    seen: set[str] = set()
    for item in raw:
        previous = item.get("previous")
        if previous is not None and previous not in seen:
            raise ValueError(f"{item['id']}: previous {previous!r} must name an earlier entry")
        kind = item.get("expect_kind")
        if kind is not None and kind not in KINDS:
            raise ValueError(f"{item['id']}: unknown expect_kind {kind!r}")
        today = item.get("today")
        entries.append(
            Golden(
                id=item["id"],
                question=item["question"],
                expect_kind=kind,
                filters=tuple(item.get("filters") or ()),
                today=date.fromisoformat(str(today)) if today else None,
                previous=previous,
            )
        )
        seen.add(item["id"])
    return entries


def dumped_ids(answer: dict[str, Any] | None) -> list[str]:
    """Every recipe id in a dumped answer, first appearance first."""
    if not answer:
        return []
    found: list[str] = []
    if answer.get("kind") == "recipes":
        for alt in answer.get("alternatives", []):
            for group in alt.get("groups", []):
                found.extend(str(i) for i in group.get("recipe_ids", []))
    elif answer.get("kind") == "text":
        found.extend(str(i) for i in answer.get("recipe_ids", []))
    return list(dict.fromkeys(found))


def render_report(results: list[Result]) -> str:
    out = ["# Ask goldens report", ""]
    for r in results:
        if r.expect_kind is None:
            mark = "-"
        else:
            mark = "✓" if r.kind == r.expect_kind else "✗"
        out.append(f"## {r.id} {mark}")
        out.append(f"> {r.question}")
        out.append("")
        out.append(f"- kind: {r.kind or 'none returned'} (expected {r.expect_kind or 'not set'}) {mark}")
        out.append(
            f"- model calls: {r.usage.calls}, seconds: {r.seconds:.1f}, "
            f"tokens: {r.usage.input_tokens} in, {r.usage.cache_read} cached, "
            f"{r.usage.cache_write} cache written, {r.usage.output_tokens} out, "
            f"cost: ${estimate_cost(r.usage):.3f}"
        )
        if r.error:
            out.append(f"- error: {r.error}")
        a = r.answer or {}
        if a.get("kind") in ("text", "none"):
            out.append(f"- text: {a.get('text', '')}")
        if a.get("kind") == "text":
            out.extend(f"  - {r.titles.get(i, i)}" for i in a.get("recipe_ids", []))
        if a.get("kind") == "recipes":
            for n, alt in enumerate(a.get("alternatives", []), 1):
                label = alt.get("label")
                out.append(f"- alternative {n}" + (f" ({label})" if label else "") + f": {alt.get('note', '')}")
                for group in alt.get("groups", []):
                    out.append(f"  - {group.get('heading') or '(no heading)'}")
                    out.extend(f"    - {r.titles.get(i, i)}" for i in group.get("recipe_ids", []))
        out.append("")
    cost = total_cost(results)
    calls = sum(r.usage.calls for r in results)
    out.append(f"Total: {len(results)} questions, {calls} model calls, estimated cost ${cost:.2f}")
    return "\n".join(out) + "\n"


# -- the live part: not unit tested, run by the owner ---------------------------------------------


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} is not set")
    return value


def _titles(db: AiReaderDb, claims: dict[str, Any], ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    result = db.run(claims, _TITLES_SQL, [ids], max_rows=len(ids))
    return {str(row[0]): str(row[1]) for row in result.rows}


def run_golden(
    entry: Golden,
    *,
    db: AiReaderDb,
    claims: dict[str, Any],
    client: Any,
    embed: Any,
    catalogue: Any,
    measuring: str,
    previous: Previous | None,
) -> tuple[Result, Answer | None]:
    """The /search/ask flow for one question (see search_api._work), timed and with usage tallied."""
    use = Usage()
    original = usage_module.record_claude

    def capture(step: str, model: str, u: object | None) -> None:
        use.add(u)

    usage_module.record_claude = capture  # type: ignore[assignment]
    started = time.monotonic()
    answer: Answer | None = None
    error: str | None = None
    try:
        toolbox = ToolBox(db, claims, embed, catalogue)
        if previous is not None:
            toolbox.admit_previous(previous.answer)
        raw = run_agent(
            client=client,
            model=MODEL,
            system=system_blocks(catalogue),
            user_text=request_text(
                today=entry.today or date.today(),
                measuring=measuring,
                filters=list(entry.filters),
                question=entry.question,
                previous=previous,
            ),
            toolbox=toolbox,
            on_progress=lambda text: print(f"    {text}", file=sys.stderr),
            cancel=threading.Event(),
        )
        answer = finalize(raw, permitted=toolbox.permitted(raw_ids(raw)))
    except Exception as exc:  # noqa: BLE001 - one failed question must not lose the rest of the run
        error = f"{type(exc).__name__}: {exc}"
    finally:
        usage_module.record_claude = original
    seconds = time.monotonic() - started
    dumped = _ANSWER.dump_python(answer, mode="json") if answer is not None else None
    titles = _titles(db, claims, dumped_ids(dumped)) if dumped else {}
    result = Result(
        id=entry.id,
        question=entry.question,
        expect_kind=entry.expect_kind,
        kind=answer.kind if answer is not None else None,
        answer=dumped,
        titles=titles,
        seconds=seconds,
        usage=use,
        error=error,
    )
    return result, answer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", required=True, help="the cook's user uuid (jwt sub)")
    parser.add_argument("--only", help="run only this entry id (a refinement then runs without its earlier turn)")
    parser.add_argument("--out", required=True, type=Path, help="where to write the markdown report")
    parser.add_argument("--measuring", default=DEFAULT_MEASURING)
    parser.add_argument("--goldens", type=Path, default=Path(__file__).with_name("ask_goldens.yaml"))
    args = parser.parse_args(argv)

    dsn = _require("AI_READER_DATABASE_URL")
    api_key = _require("GOOGLE_API_KEY")
    _require("ANTHROPIC_API_KEY")

    import anthropic  # noqa: PLC0415
    from google import genai  # noqa: PLC0415

    from recipeparser import gemini  # noqa: PLC0415

    entries = load_goldens(args.goldens)
    selected = [e for e in entries if args.only in (None, e.id)]
    if not selected:
        raise SystemExit(f"no entry with id {args.only!r}")

    claims: dict[str, Any] = {"sub": args.user, "role": "authenticated"}
    gemini_client = genai.Client(api_key=api_key)
    client = anthropic.Anthropic()
    db = AiReaderDb(dsn)
    results: list[Result] = []
    finished: dict[str, tuple[str, Answer, int]] = {}
    try:
        catalogue = db.catalogue()
        for entry in selected:
            print(f"{entry.id}: {entry.question}", file=sys.stderr)
            previous: Previous | None = None
            if entry.previous and entry.previous in finished:
                question, prior, round_no = finished[entry.previous]
                previous = Previous(question=question, answer=prior, round=round_no)
            result, answer = run_golden(
                entry,
                db=db,
                claims=claims,
                client=client,
                embed=lambda text: gemini.get_embeddings(text, gemini_client, max_retries=1),
                catalogue=catalogue,
                measuring=args.measuring,
                previous=previous,
            )
            results.append(result)
            if answer is not None:
                # A turn's round: 1 for a fresh question, one more than the turn it refines.
                finished[entry.id] = (entry.question, answer, previous.round + 1 if previous else 1)
    finally:
        db.close()
        args.out.write_text(render_report(results), encoding="utf-8")
    print(f"wrote {args.out}; estimated cost ${total_cost(results):.2f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
