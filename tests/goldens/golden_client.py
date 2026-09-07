"""A stand-in for google.genai.Client that records real replies and replays them.

Keying (spec §5.2) is the interesting part and lives in the pure functions at
the top of this module: which recorded file a call belongs to is decided by the
identity of the work unit in the prompt, never by a global call counter, so
replay is stable at any thread-pool size.
"""
from __future__ import annotations

import hashlib
from typing import Iterable, Tuple


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
