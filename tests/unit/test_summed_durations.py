"""A time written as steps is stored as its total (Cayenne Fix Roadmap F-688).

The inputs are the shapes found in the live library's Paprika imports (2026-11-20).
"""
import pytest

from recipeparser.core.durations import parse_duration, summed_duration_text

SUMS = [
    ("2 minutes (fry baris) + 4 minutes (fry onions) + 2 minutes (add garlic/ginger) + 30 minutes (simmer bari)",
     38, 38),
    ("7 to 8 minutes (fry onions) + 2 to 3 minutes (fry garlic and ginger) + 10 minutes (cook chickpeas with spices)",
     19, 21),
    ("4 1/2 hours (simmer dals) + 30 minutes (simmer with yogurt) + 4 minutes (fry onions) "
     "+ 1 minute (fry ginger with onions)", 305, 305),
    ("2 hours (pork firming) + 30 minutes (shiitake soaking)", 150, 150),
    ("1 hour (simmer pork); 30 minutes (simmer carrots)", 90, 90),
    ("30 minutes (meatloaf); 20–30 minutes (steam stew)", 50, 60),
    ("1–2 hours (for stock), then about 30 minutes (duck), then about 5 minutes (konnyaku)", 95, 155),
    ("45 minutes, then a further 1 1/2-1 3/4 hours", 135, 150),
    ("1 hour 30 minutes simmer, 25 minutes bake", 115, 115),
    ("4–5 minutes (blanching); 2 hours (simmering)", 124, 125),
]


@pytest.mark.parametrize("text,lo,hi", SUMS, ids=[s[0][:40] for s in SUMS])
def test_a_sum_of_steps_is_stored_as_its_total_with_the_steps_as_the_note(text, lo, hi):
    rewritten = summed_duration_text(text)
    assert rewritten is not None and rewritten.endswith(f"({text})")
    span = parse_duration(rewritten)
    assert (span.min, span.max) == (lo, hi)
    # The writer's steps survive as the note the cook reads.
    # (parse_duration's note is the normalised text: lower case, ASCII dashes.)
    last_word = text.replace(")", "").split()[-1].lower()
    assert span.note is not None and span.note.endswith(last_word)


NOT_SUMS = [
    "20 mins",
    "2 hours (steam) or 30 minutes (pressure cook)",
    "2 hours (or 20 minutes in pressure cooker)",
    "10 MINS, PLUS MAKING THE BASE CURRY SAUCE AND COOKING THE CHICKEN",
    "55 minutes, plus at least 6 hours’ chilling",
    "2 to 3 minutes (syrup), 4 to 5 minutes (per jamun batch), 5 minutes (simmer per batch)",
    "1 hour (simmer karhi) + 10 minutes (cook karhi) + 6 to 7 minutes per batch (fry pakoris)",
    "1 hour (simmer karhi) + 10 minutes (cook) + (optional: 6 to 7 minutes per batch for pakoris)",
    "12 minutes (14 if from cold, 16 if frozen)",
    "overnight (refrigeration) + 45 minutes (bring to room temp)",
    "7–8 minutes, then about 10 minutes, then until tender",
    "35 minutes for mini loaves or 1–1½ hours for a large loaf",
    "30 seconds (blanch); 5 minutes (simmer stock); 1–2 minutes (simmer stew)",
    "Soak the rice in plenty of cold water for at least 4 hours or overnight.",
    "",
    None,
]


@pytest.mark.parametrize("text", NOT_SUMS)
def test_anything_but_a_sum_of_steps_is_left_alone(text):
    assert summed_duration_text(text) is None
