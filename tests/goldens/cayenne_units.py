"""The unit words Cayenne's registry parses, read from its units.ts (imperial measures spec, Testing).

REFINE writes a recipe's unit as the writer's word; Cayenne renders it only if units.ts knows it.
This reads the registry's ids and alias keys so a golden can check a recorded reply against them.
The file comes from CAYENNE_UNITS_TS, else the sibling checkout ../../Cayenne; None when neither exists.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Set

_REPO = Path(__file__).resolve().parents[2]
_SIBLING = _REPO.parent.parent / "Cayenne" / "cayenne-web" / "src" / "lib" / "domain" / "units.ts"


def units_ts() -> Optional[Path]:
    """CAYENNE_UNITS_TS when set; else the sibling checkout, but only once its registry reads the
    Imperial measures. The sibling is whatever branch that shared checkout is on, and an older
    registry would fail every local run for a reason that is not this repository's."""
    env = os.environ.get("CAYENNE_UNITS_TS")
    if env:
        path = Path(env)
        return path if path.is_file() else None
    if _SIBLING.is_file() and "id: 'gill'" in _SIBLING.read_text(encoding="utf-8"):
        return _SIBLING
    return None


def known_unit_words(path: Path) -> Set[str]:
    """Every id in UNITS and every key in ALIASES, lower-cased."""
    text = path.read_text(encoding="utf-8")
    ids = set(re.findall(r"\{\s*id:\s*'([^']+)'", text))
    aliases_block = text.split("const ALIASES", 1)[1].split("};", 1)[0]
    keys = set(re.findall(r"(?:'([^']+)'|([A-Za-z][\w]*))\s*:\s*'", aliases_block))
    words = ids | {a or b for a, b in keys}
    return {w.lower() for w in words}


def normalise(unit: str) -> str:
    """As units.ts normalise(): lower-cased, trimmed, inner spaces collapsed, one trailing full stop dropped."""
    return re.sub(r"\.$", "", re.sub(r"\s+", " ", unit.lower().strip()))
