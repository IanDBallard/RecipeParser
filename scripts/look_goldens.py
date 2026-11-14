"""
On demand: run the golden looks in look_goldens.yaml through the look agent behind POST /search/look
and write a report to read (Cayenne AI search Stage 5, §7.3).

It runs the same flow as the endpoint (recipeparser/adapters/search_api.py ``_look_work``): the look's
prompt and request, ``run_look`` with the seeded hosts, then ``check_candidates``. The cook's library
is not read: each entry seeds its own host list, and nothing is classified. It is NOT a test. It
calls Anthropic's web search and web fetch, so it costs real money (roughly 10-30 cents a look).
Run it with the owner's OK, after a prompt or limit change. The report gives per look the seconds,
model calls, searches, fetches, tokens and estimated cost, and per candidate the agent offered
(before the server's checks) whether its address is on an allowed host, whether it loads, and
whether the checks kept it, so the run measures what the checks remove.

    python scripts/look_goldens.py --out report.md --yes
    python scripts/look_goldens.py --only vegan-side --out one.md --yes

Requires ANTHROPIC_API_KEY in the environment. WARNING: importing recipeparser loads the
repository's .env file, so the key may be satisfied by .env without you having set it. Nothing runs
until you pass --yes; without it the script prints the model and the number of looks and exits 2.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from recipeparser import usage as usage_module
from recipeparser.adapters.ai_search.look import (
    RawCandidate,
    check_candidates,
    fetch_key,
    is_unambiguous,
    run_look,
    site_of,
    url_host,
)
from recipeparser.adapters.ai_search.look_prompt import look_request_text, look_system_blocks
from recipeparser.config import AI_SEARCH_MODEL

MODEL = AI_SEARCH_MODEL
DEFAULT_MEASURING = "metric, °C"
# Dollars per million tokens, and per web search.
INPUT_RATE = 4.0
OUTPUT_RATE = 20.0
CACHE_READ_RATE = 0.20
CACHE_WRITE_RATE = INPUT_RATE * 1.25
SEARCH_RATE = 10.0 / 1000
LOAD_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class LookGolden:
    id: str
    question: str
    gaps: tuple[tuple[str | None, str], ...]  # (heading, missing)
    hosts: tuple[str, ...]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    searches: int = 0
    fetches: int = 0
    calls: int = 0

    def add(self, u: object) -> None:
        self.input_tokens += getattr(u, "input_tokens", None) or 0
        self.output_tokens += getattr(u, "output_tokens", None) or 0
        self.cache_read += getattr(u, "cache_read_input_tokens", None) or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", None) or 0
        tools = getattr(u, "server_tool_use", None)
        self.searches += getattr(tools, "web_search_requests", None) or 0
        self.fetches += getattr(tools, "web_fetch_requests", None) or 0
        self.calls += 1


@dataclass(frozen=True)
class Scored:
    candidate: RawCandidate
    on_host: bool
    loads: bool | None  # None: not tried (off host)
    kept: bool  # whether it survived check_candidates


@dataclass
class LookResult:
    id: str
    question: str
    seconds: float = 0.0
    usage: Usage = field(default_factory=Usage)
    scored: list[Scored] = field(default_factory=list)  # every candidate the agent offered
    error: str | None = None


def estimate_cost(u: Usage) -> float:
    tokens = (
        u.input_tokens * INPUT_RATE
        + u.output_tokens * OUTPUT_RATE
        + u.cache_read * CACHE_READ_RATE
        + u.cache_write * CACHE_WRITE_RATE
    ) / 1_000_000
    return tokens + u.searches * SEARCH_RATE


def score_host(url: str, hosts: tuple[str, ...] | list[str] | set[str]) -> bool:
    """Whether ``url`` is ``https``, unambiguous (every parser reads one host) and on one of ``hosts``
    (or a subdomain of one)."""
    return url.lower().startswith("https://") and is_unambiguous(url) and site_of(url_host(url), hosts) is not None


def score_candidates(
    offered: list[RawCandidate], kept: list[RawCandidate], hosts: tuple[str, ...], loads: Any
) -> list[Scored]:
    """Each offered candidate, as the agent gave it, with its host check, ``loads(url)`` when on a
    host, and whether the server's checks kept it (``kept`` is ``check_candidates``'s output)."""
    kept_keys = {(k.gap, fetch_key(k.url)) for k in kept}
    scored: list[Scored] = []
    for c in offered:
        on_host = score_host(c.url, hosts)
        survived = is_unambiguous(c.url) and (c.gap, fetch_key(c.url)) in kept_keys
        scored.append(Scored(c, on_host, loads(c.url) if on_host else None, survived))
    return scored


def load_goldens(path: Path) -> list[LookGolden]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries: list[LookGolden] = []
    for item in raw:
        gaps: list[tuple[str | None, str]] = []
        for gap in item["gaps"]:
            if isinstance(gap, str):
                gaps.append((None, gap))
            else:
                gaps.append((gap.get("heading"), str(gap["missing"])))
        hosts = tuple(str(h) for h in item["hosts"])
        if not gaps or not hosts:
            raise ValueError(f"{item['id']}: needs at least one gap and one host")
        entries.append(LookGolden(id=str(item["id"]), question=str(item["question"]), gaps=tuple(gaps), hosts=hosts))
    return entries


def _mark(value: bool | None) -> str:
    return "–" if value is None else ("✓" if value else "✗")


def render_report(results: list[LookResult]) -> str:
    lines = ["# Look goldens", ""]
    total = sum(estimate_cost(r.usage) for r in results)
    lines.append(f"{len(results)} looks, estimated ${total:.2f}, {sum(r.seconds for r in results):.0f} s in all.")
    for r in results:
        u = r.usage
        lines += [
            "",
            f"## {r.id}",
            f"{r.question}",
            "",
            f"{r.seconds:.1f} s · {u.calls} calls · {u.searches} searches · {u.fetches} fetches · "
            f"{u.input_tokens + u.cache_read + u.cache_write} in / {u.output_tokens} out · ${estimate_cost(u):.3f}",
            f"Offered {len(r.scored)}, kept {sum(1 for s in r.scored if s.kept)}.",
        ]
        if r.error:
            lines.append(f"Error: {r.error}")
        for s in r.scored:
            c = s.candidate
            lines.append(
                f"- gap {c.gap} · host {_mark(s.on_host)} · loads {_mark(s.loads)} · kept {_mark(s.kept)}"
                f" · {c.title} · {c.url!r}"
            )
            if c.note:
                lines.append(f"  {c.note}")
    return "\n".join(lines) + "\n"


def _loads(url: str) -> bool:
    import httpx  # noqa: PLC0415

    try:
        resp = httpx.get(url, follow_redirects=True, timeout=LOAD_TIMEOUT_S, headers={"User-Agent": "Mozilla/5.0"})
    except httpx.HTTPError:
        return False
    return resp.status_code < 400


def run_one(entry: LookGolden, *, client: Any, measuring: str) -> LookResult:
    use = Usage()
    original = usage_module.record_claude

    def capture(step: str, model: str, u: object | None) -> None:
        use.add(u)

    usage_module.record_claude = capture  # type: ignore[assignment]
    result = LookResult(id=entry.id, question=entry.question, usage=use)
    started = time.monotonic()
    try:
        raw, fetched = run_look(
            client=client,
            model=MODEL,
            system=look_system_blocks(),
            user_text=look_request_text(
                sites=list(entry.hosts),
                measuring=measuring,
                question=entry.question,
                gaps=list(entry.gaps),
                exclude=[],
            ),
            hosts=list(entry.hosts),
            on_progress=lambda text: print(f"    {text}", file=sys.stderr),
            cancel=threading.Event(),
        )
        checked = check_candidates(
            raw, hosts=set(entry.hosts), fetched=fetched, exclude=set(), gap_count=len(entry.gaps)
        )
        result.seconds = time.monotonic() - started
        result.scored = score_candidates(raw, checked, entry.hosts, _loads)
    except Exception as exc:  # noqa: BLE001 - one failed look must not lose the rest of the run
        result.error = f"{type(exc).__name__}: {exc}"
        result.seconds = time.monotonic() - started
    finally:
        usage_module.record_claude = original
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="run only this entry id")
    parser.add_argument("--out", required=True, type=Path, help="where to write the markdown report")
    parser.add_argument("--yes", action="store_true", help="go ahead: this spends real money")
    parser.add_argument("--measuring", default=DEFAULT_MEASURING)
    parser.add_argument("--goldens", type=Path, default=Path(__file__).with_name("look_goldens.yaml"))
    args = parser.parse_args(argv)

    entries = [e for e in load_goldens(args.goldens) if args.only in (None, e.id)]
    if not entries:
        raise SystemExit(f"no entry with id {args.only!r}")
    print(f"model: {MODEL}; looks: {len(entries)}", file=sys.stderr)
    if not args.yes:
        print("Not running: pass --yes to run these looks (they spend real money).", file=sys.stderr)
        return 2

    import anthropic  # noqa: PLC0415

    client = anthropic.Anthropic()
    results: list[LookResult] = []
    try:
        for entry in entries:
            print(f"{entry.id}: {entry.question}", file=sys.stderr)
            result = run_one(entry, client=client, measuring=args.measuring)
            print(f"    {result.seconds:.1f} s, ${estimate_cost(result.usage):.3f}", file=sys.stderr)
            results.append(result)
    finally:
        args.out.write_text(render_report(results), encoding="utf-8")
    print(f"wrote {args.out}; estimated cost ${sum(estimate_cost(r.usage) for r in results):.2f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
