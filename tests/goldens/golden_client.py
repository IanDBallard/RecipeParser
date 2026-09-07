"""A stand-in for google.genai.Client that records real replies and replays them.

Keying (spec §5.2) is the interesting part and lives in the pure functions at
the top of this module: which recorded file a call belongs to is decided by the
identity of the work unit in the prompt, never by a global call counter, so
replay is stable at any thread-pool size.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger(__name__)


class UnknownPromptError(RuntimeError):
    """A prompt reached the golden client that no stage rule recognises."""


#: Ordered (stage, marker) rules matched against the prompt head.  Order matters:
#: `table` is last because its marker is the only one that a *chunk body* can
#: plausibly contain, and every rule above it anchors at the start of its own
#: prompt.
_STAGE_RULES: Tuple[Tuple[str, str], ...] = (
    ("refine", "culinary data refiner"),
    ("extract", "culinary data extractor"),
    ("toc-parse", "table-of-contents or contents page"),
    ("toc-classify", "table-of-contents entries from a cookbook"),
    ("vision", "ocr assistant"),
    ("connectivity", "reply with the single word ok"),
    ("table", "baker"),
)

#: Where each stage's body starts.  An empty tuple means "the whole prompt is
#: the body" — vision, both TOC calls, and the connectivity ping.
_BODY_MARKERS = {
    "extract": ("Text chunk:", "Text:"),
    "table": ("Text:",),
    "refine": ("RAW RECIPE:",),
    "toc-parse": (),
    "toc-classify": (),
    "vision": (),
    "connectivity": (),
}

#: How much of the prompt the stage sniff reads.  Long enough to reach the
#: table prompt's marker on its second line, short enough that an embedded
#: chunk body cannot reach it.
_HEAD_CHARS = 600


def text_part(contents: object) -> str:
    """The text of a genai ``contents`` argument.

    ``gemini.py`` passes a plain string everywhere except the vision call, which
    passes ``[Part.from_bytes(...), PROMPT]``.  Image bytes are corpus files, so
    only the text matters for keying.
    """
    if isinstance(contents, str):
        return contents
    if isinstance(contents, Iterable):
        parts = [p for p in contents if isinstance(p, str)]
        if parts:
            return "\n".join(parts)
    raise UnknownPromptError(f"contents carried no text part: {type(contents)!r}")


def sniff_stage(contents: object) -> str:
    """Which pipeline stage sent this prompt."""
    head = text_part(contents).strip()[:_HEAD_CHARS].lower()
    for stage, marker in _STAGE_RULES:
        if marker in head:
            return stage
    raise UnknownPromptError(
        "no stage rule matched this prompt. First 200 chars:\n"
        + text_part(contents).strip()[:200]
    )


def prompt_body(contents: object, stage: str) -> str:
    """The work unit inside the prompt: everything after the stage's marker."""
    text = text_part(contents)
    for marker in _BODY_MARKERS.get(stage, ()):
        index = text.find(marker)
        if index != -1:
            return text[index + len(marker):]
    return text


def body_sha8(body: str) -> str:
    """First eight hex digits of the SHA-256 of a prompt body."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]


def record_key(stage: str, body: str, ordinal: int) -> Tuple[str, str]:
    """The (directory, filename) a call's recording lives under."""
    return body_sha8(body), f"{stage}-{ordinal:02d}.json"


#: Config keys not worth storing.  The response schema is large and is covered
#: by its own snapshot (spec §6.3).
_CONFIG_SKIP = {"response_json_schema", "response_schema"}

_EMBEDDING_DIMS = 1536


class MissingRecordingError(AssertionError):
    """Replay was asked for a reply that was never recorded."""


@dataclass
class GoldenEmbedding:
    values: List[float]


@dataclass
class GoldenEmbedResponse:
    embeddings: List[GoldenEmbedding]


@dataclass
class GoldenResponse:
    """The slice of a genai response the production code actually reads.

    ``gemini.py`` reads ``.text`` and, on the unparseable path, ``.candidates``.
    ``toc.py`` reads ``.parsed``; replaying TOC calls is out of scope here, so
    ``parsed`` stays None and the TOC helpers fall through to their own
    "AI parsing failed" branch rather than returning wrong data.
    """

    text: str
    parsed: Optional[object] = None
    candidates: tuple = field(default_factory=tuple)


def _seeded_vector(text: str) -> List[float]:
    """A deterministic unit-ish vector derived from *text*.

    Embeddings are never recorded: the embed stage has no parse logic worth
    locking and 1536 floats per recipe would bloat the fixtures.  The assemble
    stage still receives a real-length vector.
    """
    import random

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(digest, "big"))
    return [rng.uniform(-1.0, 1.0) for _ in range(_EMBEDDING_DIMS)]


class _GoldenModels:
    """The ``client.models`` surface the production code calls."""

    def __init__(self, owner: "GoldenClient") -> None:
        self._owner = owner

    def generate_content(self, *, model: str, contents: object, config: dict) -> GoldenResponse:
        return self._owner._generate(model=model, contents=contents, config=config)

    def embed_content(self, *, model: str, contents: str, config: object = None) -> GoldenEmbedResponse:
        return GoldenEmbedResponse(embeddings=[GoldenEmbedding(values=_seeded_vector(contents))])


class GoldenClient:
    """Stands in for ``google.genai.Client`` for one corpus fixture.

    Replay (the default) serves files under ``<root>/<fixture_id>/`` and never
    touches the network.  Record mode forwards to a real client and writes each
    reply before returning it.
    """

    def __init__(self, fixture_id: str, root: Path, record: bool = False) -> None:
        self.fixture_id = fixture_id
        self.root = Path(root)
        self.record = bool(record)
        self.models = _GoldenModels(self)
        self._ordinals: Dict[tuple, int] = {}
        self._lock = threading.Lock()
        self._real: Any = None

        if self.record:
            key = os.environ.get("GOOGLE_API_KEY", "").strip()
            if not key or key == "dummy-key-for-tests":
                raise RuntimeError(
                    "--record-gemini needs a real GOOGLE_API_KEY. The test suite sets "
                    "GOOGLE_API_KEY=dummy-key-for-tests by default (tests/conftest.py), so "
                    "export a real key in this shell before recording."
                )
            from google import genai

            self._real = genai.Client(api_key=key)

    # ── internals ────────────────────────────────────────────────────────────

    @property
    def _fixture_dir(self) -> Path:
        return self.root / self.fixture_id

    def _next_ordinal(self, stage: str, body: str) -> int:
        """The next per-(body, stage) ordinal.

        Every call for one body happens on one worker thread in a fixed order,
        so this counter is only ever advanced by that thread; the lock guards
        the dict, not an ordering assumption.
        """
        with self._lock:
            key = (body_sha8(body), stage)
            ordinal = self._ordinals.get(key, 0)
            self._ordinals[key] = ordinal + 1
            return ordinal

    def _generate(self, *, model: str, contents: object, config: dict) -> GoldenResponse:
        stage = sniff_stage(contents)
        body = prompt_body(contents, stage)
        ordinal = self._next_ordinal(stage, body)
        directory, filename = record_key(stage, body, ordinal)
        path = self._fixture_dir / directory / filename

        if self.record:
            response = self._real.models.generate_content(
                model=model, contents=contents, config=config
            )
            text = getattr(response, "text", "") or ""
            self._write(path, stage, ordinal, model, config, contents, text)
            return GoldenResponse(text=text)

        if not path.exists():
            raise MissingRecordingError(
                f"No recorded Gemini reply at {path}.\n"
                f"stage={stage} ordinal={ordinal} fixture={self.fixture_id}\n"
                "Re-record with: pytest tests/goldens --record-gemini (needs a real GOOGLE_API_KEY)."
            )

        payload = json.loads(path.read_text(encoding="utf-8"))
        expected = payload.get("prompt_sha256")
        actual = hashlib.sha256(text_part(contents).encode("utf-8")).hexdigest()
        if expected and expected != actual:
            warnings.warn(
                f"prompt_sha256 mismatch for {path}: the prompt has drifted since this reply "
                "was recorded. Serving it anyway — the prompt snapshot is what guards drift.",
                stacklevel=2,
            )
        return GoldenResponse(text=payload["response_text"])

    def _write(self, path: Path, stage: str, ordinal: int, model: str,
               config: dict, contents: object, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        stored = {k: v for k, v in (config or {}).items() if k not in _CONFIG_SKIP}
        payload = {
            "stage": stage,
            "ordinal": ordinal,
            "model": model,
            "config": stored,
            "prompt_sha256": hashlib.sha256(text_part(contents).encode("utf-8")).hexdigest(),
            "response_text": text,
        }
        with self._lock:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log.info("recorded %s", path)
