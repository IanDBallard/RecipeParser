"""The deterministic temperature splice (Cayenne oven temperatures design, D3-D5)."""
import pytest

from recipeparser.core.temperatures import (
    find_temperatures,
    needs_model,
    place_temperature_mentions,
    repair_tokens,
    splice,
)


def tag(text: str) -> str:
    return splice(text, find_temperatures(text))


@pytest.mark.parametrize("step, expected", [
    ("Preheat the oven to 350°F.", "Preheat the oven to {{temp:350F,oven|350°F}}."),
    ("Bake at 180°C/160°C fan/gas mark 4 for 20 minutes.",
     "Bake at {{temp:180C,oven,fan=160C|180°C/160°C fan/gas mark 4}} for 20 minutes."),
    ("Roast at 200°C (180°C fan) for an hour.", "Roast at {{temp:200C,oven,fan=180C|200°C (180°C fan)}} for an hour."),
    ("Preheat oven to 425°F (220°C).", "Preheat oven to {{temp:425F,oven|425°F (220°C)}}."),
    ("Preheat to 350 degrees F.", "Preheat to {{temp:350F,oven|350 degrees F}}."),
    ("Heat the oil to 180C.", "Heat the oil to {{temp:180C|180C}}."),
    ("Bake at gas mark ½ overnight.", "Bake at {{temp:gas0.5,oven|gas mark ½}} overnight."),
    ("Bake at Gas 6.", "Bake at {{temp:gas6,oven|Gas 6}}."),
    ("Freeze at -18°C.", "Freeze at {{temp:-18C|-18°C}}."),
    ("Preheat the oven. Cook until a thermometer in the centre reads 63°C.",
     "Preheat the oven. Cook until a thermometer in the centre reads {{temp:63C|63°C}}."),
    ("Bake until the loaf reads 95°C inside.", "Bake until the loaf reads {{temp:95C|95°C}} inside."),
    ("Mix the {{ing_01|flour|all}} and bake at 200°C.", "Mix the {{ing_01|flour|all}} and bake at {{temp:200C,oven|200°C}}."),
    ("Bake at 180°C (fan 160°C).", "Bake at {{temp:180C,oven,fan=160C|180°C (fan 160°C)}}."),
    ("Bake at 350°F (convection 325°F).", "Bake at {{temp:350F,oven,fan=325F|350°F (convection 325°F)}}."),
    ("Preheat oven to 350°F (convection oven 325°F).",
     "Preheat oven to {{temp:350F,oven,fan=325F|350°F (convection oven 325°F)}}."),
    ("Bake at 160°C fan-forced (180°C conventional).",
     "Bake at {{temp:180C,oven,fan=160C|160°C fan-forced (180°C conventional)}}."),
    ("Bake at 160°C fan-forced.", "Bake at {{temp:fan=160C|160°C fan-forced}}."),
    ("Preheat oven to 190C/170C fan/Gas 5.", "Preheat oven to {{temp:190C,oven,fan=170C|190C/170C fan/Gas 5}}."),
    ("Bake until sugar reaches 115°C.", "Bake until sugar reaches {{temp:115C|115°C}}."),
    ("Fry the roasted nuts in oil at 180°C.", "Fry the roasted nuts in oil at {{temp:180C|180°C}}."),
    ("In a 350°F oven, cook until a thermometer reads 165°F.",
     "In a {{temp:350F,oven|350°F}} oven, cook until a thermometer reads {{temp:165F|165°F}}."),
    # Oven sentences from the live library's dry run (2026-10-13): "center", "temperature" and
    # "inside" are ordinary oven words, not probe words.
    ("Position a rack in the center of the oven and heat the oven to 350°F.",
     "Position a rack in the center of the oven and heat the oven to {{temp:350F,oven|350°F}}."),
    ("Reduce oven temperature to 375 degrees F.", "Reduce oven temperature to {{temp:375F,oven|375 degrees F}}."),
    ("Preheat the oven to 425 degrees F and place a 9-inch cast iron skillet inside to heat.",
     "Preheat the oven to {{temp:425F,oven|425 degrees F}} and place a 9-inch cast iron skillet inside to heat."),
    ("Bake in a water bath at 160°C.", "Bake in a water bath at {{temp:160C,oven|160°C}}."),
    # The one-character signs U+2109 and U+2103.
    ("Bake in a preheated 300℉ oven for 35 minutes.", "Bake in a preheated {{temp:300F,oven|300℉}} oven for 35 minutes."),
    ("Preheat the oven to 180 ℃.", "Preheat the oven to {{temp:180C,oven|180 ℃}}."),
])
def test_tags_the_clear_forms(step, expected):
    assert tag(step) == expected


@pytest.mark.parametrize("step", [
    "Bake in a moderate oven.",
    "Add 2 C flour.",
    "Fry at 170-180°C until golden.",
    "Fry at 170°C-180°C until golden.",
    "Bake at 350 for an hour.",
    "Leave at room temperature.",
    "Bake at {{temp:180C,oven|180°C}}.",
    "Bake at 350/375°F.",
    "Bake at 400°F or 425°F.",
    "Bake at 200°C or 220°C.",
    "Bake between 350 and 375°F.",
    "Turn the gas 3 times.",
    "Use the gas 6 burner.",
    "Turn off the gas 5 minutes before serving.",
    "Leave the gas 2 hours.",
    "The butter should be between 65 and 70°F (18 and 21°C); if not, refrigerate.",
])
def test_leaves_alone_what_it_cannot_tag_for_certain(step):
    assert tag(step) == step


@pytest.mark.parametrize("step, expected", [
    ("Bake in a moderate oven.", True),
    ("Bake at 350 for an hour.", True),
    ("Preheat the oven to {{temp:350F,oven|350°F}}.", False),
    ("Fry at 170-180°C until golden.", False),
    ("Bake for 25 minutes.", False),
    ("Roast 2 chickens.", False),
    ("Preheat the oven to 350.", True),
    ("Bake at 350, covered.", True),
    # A degree sign with no unit is the model's to read (live library, 2026-10-13).
    ("Preheat oven to 300°.", True),
    ("Bake in pre-heated 375° oven for about 35 minutes.", True),
    ("Fold the dough in half, then turn it 90° and repeat.", False),
])
def test_needs_model(step, expected):
    assert needs_model(step) is expected


def test_places_model_mentions_by_quote_and_skips_a_bad_value_or_a_token():
    text = "Mix the {{ing_01|flour|all}}, then bake in a moderate oven for an hour."
    out, dropped = place_temperature_mentions(text, [
        ("moderate oven", "bake in a moderate oven for", "180C,oven"),
        ("flour", "Mix the flour", "180C"),
        ("an hour", "for an hour", "hot"),
    ])
    assert out == "Mix the {{ing_01|flour|all}}, then bake in a {{temp:180C,oven|moderate oven}} for an hour."
    assert len(dropped) == 2


def test_words_have_balanced_parentheses():
    for step in ["Bake at 160°C fan-forced (180°C conventional).", "Bake at 180°C (fan 160°C).",
                 "Roast at 200°C (180°C fan) for an hour."]:
        for f in find_temperatures(step):
            assert f.words.count("(") == f.words.count(")")


# The live backfill (2026-10-13): the model quoted "350" in "350 degrees" (the kitchen would read
# "180°C degrees") and tagged one end of "500 to 550°F".
def test_a_placed_mention_takes_in_its_unit_words():
    out, dropped = place_temperature_mentions("Heat oven to 350 degrees. Grease a pan.",
                                              [("350", "Heat oven to 350 degrees", "350F,oven")])
    assert out == "Heat oven to {{temp:350F,oven|350 degrees}}. Grease a pan."
    assert dropped == []


def test_a_placed_mention_that_is_part_of_a_range_is_dropped():
    text = "Preheat oven to highest possible temperature setting, 500 to 550°F (260 to 288°C)."
    for quote in ("550°", "500 to 550°"):
        out, dropped = place_temperature_mentions(text, [(quote, "setting, 500 to 550°F", "550F,oven")])
        assert out == text
        assert len(dropped) == 1


@pytest.mark.parametrize("text, expected", [
    ("Heat oven to {{temp:350F,oven|350}} degrees. Grease a pan.",
     "Heat oven to {{temp:350F,oven|350 degrees}}. Grease a pan."),
    ("heat to {{temp:425F,oven|425}} degrees, or {{temp:400F,oven|400}} degrees if using convection.",
     "heat to {{temp:425F,oven|425 degrees}}, or {{temp:400F,oven|400 degrees}} if using convection."),
    ("setting, 500 to {{temp:550F,oven|550°}}F (260 to 288°C).", "setting, 500 to 550°F (260 to 288°C)."),
    ("setting, {{temp:500F,oven|500 to 550°}}F (260 to 288°C).", "setting, 500 to 550°F (260 to 288°C)."),
    ("between 65 and 70°F (18 and {{temp:21C|21°C}}); if not", "between 65 and 70°F (18 and 21°C); if not"),
    ("Bake at {{temp:180C,oven|180°C}} with the {{ing_01|flour|all}}.",
     "Bake at {{temp:180C,oven|180°C}} with the {{ing_01|flour|all}}."),
])
def test_repair_tokens(text, expected):
    from recipeparser.core.fat_tokens import strip_fat_tokens
    out, _ = repair_tokens(text)
    assert out == expected
    assert strip_fat_tokens(out) == strip_fat_tokens(text)  # no word changes
