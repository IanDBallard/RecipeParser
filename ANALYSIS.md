# EPUB Recipe Parsing — Data Analysis Log

## Purpose
Document findings from inspecting real EPUB cookbooks to inform parsing strategy decisions, particularly around how different books structure their ingredient data and whether a pre-normalisation step is needed.

---

## Book 1: The Elements of Pizza — Ken Forkish

**File:** `The Elements of Pizza - Ken Forkish.epub`  
**Segments:** 19 total, 9 passed `is_recipe_candidate`  
**Recipes extracted:** 13 (from segment 14 only)

### Segment map

| Seg | Candidate | Chars  | Content |
|-----|-----------|--------|---------|
| 0   | No        | 2,759  | Table of contents |
| 1   | No        | 44     | Cover image only |
| 2   | No        | 89     | Image only |
| 3   | No        | 87     | Title page image |
| 4   | No        | 819    | Copyright page |
| 5   | No        | 1,757  | Contents listing |
| 6   | No        | 125    | Image + caption |
| 7   | Yes       | 14,676 | Introduction — narrative, no recipes |
| 8   | Yes       | 53,058 | Chapter 1: The Soul of Pizza — narrative |
| 9   | Yes       | 25,100 | Chapter 2: Pizza Styles — narrative |
| 10  | Yes       | 23,974 | Chapter 3: Eight Details — technique |
| 11  | Yes       | 49,843 | Chapter 4: Ingredients & Equipment — narrative |
| 12  | Yes       | 39,697 | Chapter 5: Methods — technique |
| 13  | Yes       | 76,458 | **Chapter 6: Pizza Dough Recipes — MISSED** |
| 14  | Yes       | 158,236| Chapter 7: Pizza Recipes — 13 recipes found ✓ |
| 15  | No        | 1,330  | Measurement conversion charts |
| 16  | No        | 2,088  | Acknowledgements |
| 17  | Yes       | 11,488 | Index — false positive |
| 18  | No        | 202    | Back matter image |

### False positives (candidate=Yes but no recipes)
- Segments 7–12: Long narrative/technique chapters that use measurement units and cooking verbs in prose discussion, triggering the heuristic. Gemini correctly returns empty for these.
- Segment 17 (Index): References recipe names alongside units.

### Missed recipes (segment 13)
**Root cause:** Ingredient tables in this book use a 3-column layout (Ingredient / Quantity / Baker's %) that renders as bare newline-separated values when HTML is stripped:

```
Water
350g
1½ cups
70%
Fine sea salt
15g
2¾ tsp
3.0%
```

Gemini cannot reliably reconstruct which number belongs to which ingredient from this flat structure, so it returns an empty recipe list rather than guess incorrectly.

**Recipes missed:** All dough recipes in Chapter 6 (estimated 8–12 distinct dough recipes based on names visible in the TOC: Saturday Pizza Dough, "I Slept In" Dough, Enzo's Pizza Dough, 24-to-48-Hour Pizza Dough, 48-to-72-Hour Biga Pizza Dough, Overnight Levain Pizza Dough, New York Pizza Dough, Saturday Pan Pizza Dough, Bar Pizza Dough, Al Taglio Pizza Dough).

### Open questions
- Is the multi-column ingredient table format common across other technical baking books?
- Do more narrative-style cookbooks (e.g. Ottolenghi, Nigella) use inline prose ingredients that would parse cleanly?
- Is segment 14's 13-recipe count complete, or did the same table format cause misses there too?

---

## Books to Analyse Next

- [x] A prose-style cookbook — The Woks of Life (see Book 2 below)
- [ ] A classic structured cookbook (e.g. Joy of Cooking style — ingredient list then numbered steps)
- [ ] Another technical baking book (to confirm if table format is common in that genre)

---

---

## Book 2: The Woks of Life — Bill Leung

**File:** `The Woks of Life - Bill Leung.epub`
**Segments:** 24 total, 13 passed `is_recipe_candidate`
**Recipes extracted:** Not yet run (API)

### Segment map

| Seg | Candidate | Chars  | Content |
|-----|-----------|--------|---------|
| 0   | No        | 85     | Cover images |
| 1   | No        | 37     | Title page image |
| 2   | No        | 1,142  | Copyright page |
| 3   | No        | 784    | Contents |
| 4   | Yes       | 45,285 | Introduction — narrative, likely no recipes |
| 5   | Yes       | 58,718 | Dim Sum — recipes ✓ |
| 6   | Yes       | 48,505 | Starters — recipes ✓ |
| 7   | Yes       | 39,174 | Noodles — recipes ✓ |
| 8   | Yes       | 30,270 | Rice — recipes ✓ |
| 9   | Yes       | 47,023 | Poultry & Eggs — recipes ✓ |
| 10  | Yes       | 55,464 | Pork, Beef & Lamb — recipes ✓ |
| 11  | Yes       | 31,943 | Fish & Shellfish — recipes ✓ |
| 12  | Yes       | 34,542 | Vegetables & Tofu — recipes ✓ |
| 13  | Yes       | 31,281 | Soups & Stocks — recipes ✓ |
| 14  | Yes       | 13,933 | Sauces — recipes ✓ |
| 15  | Yes       | 39,096 | Desserts & Sweet Things — recipes ✓ |
| 16  | Yes       | 30,390 | Building Out Your Chinese Pantry — likely no recipes |
| 17  | No        | 75     | Back matter promo |
| 18  | No        | 3,032  | Acknowledgments |
| 19  | No        | 13,963 | Index |
| 20  | No        | 423    | About the blog |
| 21  | No        | 42     | Image |
| 22  | No        | 185    | Next reads promo |
| 23  | No        | 4,358  | Contents (duplicate) |

### Ingredient format
**Clean prose-style ingredient lists** — one ingredient per line with quantity fully inline:
```
3 medium garlic cloves, finely minced
¼ cup sugar
1 tablespoon hoisin sauce
3 pounds boneless pork shoulder or butt
```
No tables, no baker's percentages, no multi-column layouts. This format is ideal for LLM extraction.

### Notable observations
- Chinese characters present in recipe names (e.g. `叉烧`, `担担面`). Gemini handles these fine; Windows terminal (cp1252) cannot — use UTF-8 file output for any diagnostic scripts.
- Each chapter is a single EPUB document containing many recipes (30–58k chars per segment). Multiple recipes per API call expected.
- `[IMAGE: ...]` markers are correctly interleaved near recipe headings.
- Segment 4 (Introduction, 45k chars) and Segment 16 (Pantry guide, 30k chars) are likely false positives for `is_recipe_candidate` but Gemini should return empty for them.
- No missed recipes anticipated — format is clean.

### Contrast with Forkish
| Aspect | Forkish (Pizza) | Woks of Life |
|--------|----------------|--------------|
| Ingredient format | 3-column table (name / weight / baker's %) | Prose, one per line |
| Units | Grams + cups + percentages | Cups/tbsp/tsp/oz |
| Recipes per segment | 13 in one giant chapter | ~5–15 per chapter |
| Expected parsing difficulty | **High** (table format breaks LLM) | **Low** (clean prose) |

---

---

## Book 3: Italian Food — Elizabeth David

**File:** `Italian Food - Elizabeth David.epub`
**Segments:** 54 total, 25 passed `is_recipe_candidate`
**Recipes extracted:** Not yet run (API)

### Segment map (candidates only)
| Seg | Chars  | Content |
|-----|--------|---------|
| 10  | 17,897 | Introduction to First Penguin Edition — likely false positive |
| 18  | 1,067  | Hors d'Oeuvre footnotes only — false positive |
| 31  | 6,530  | Italian Dishes in Foreign Kitchens — narrative |
| 32  | 43,492 | The Italian Store Cupboard — ingredient glossary, no recipes |
| 33  | 15,499 | Kitchen Equipment — no recipes |
| 34  | 27,417 | Hors d'Oeuvre and Salads — recipes ✓ |
| 35  | 24,743 | Soups — recipes ✓ |
| 36  | 33,978 | Pasta Asciutta — recipes ✓ |
| 37  | 24,725 | Ravioli, Gnocchi — recipes ✓ |
| 38  | 32,780 | Rice — recipes ✓ |
| 39  | 6,428  | Haricot Beans, Polenta — recipes ✓ |
| 40  | 27,756 | Eggs, Cheese, Pizze — recipes ✓ |
| 41  | 24,355 | Fish Soups — recipes ✓ |
| 42  | 62,921 | Fish — recipes ✓ (large chapter) |
| 43  | 68,869 | Meat — recipes ✓ (large chapter) |
| 44  | 30,706 | Poultry and Game — recipes ✓ |
| 45  | 37,867 | Vegetables — recipes ✓ |
| 46  | 36,391 | Sweets — recipes ✓ |
| 47  | 22,828 | Sauces — recipes ✓ |
| 48  | 18,285 | Preserves — recipes ✓ |
| 50  | 19,055 | Notes on Italian Wines — no recipes, false positive |
| 51  | 19,770 | Some Italian Cookery Books — bibliography, false positive |
| 53  | 32,938 | Visitors' Books — literary/historical, false positive |

### Ingredient format — **Fully embedded prose, no ingredient list at all**

Elizabeth David is a narrative food writer. Recipes have **no separate ingredient list** — quantities and ingredients are woven directly into the method text:

```
Allow 3 or 4 little slices to each person; beat them out flat,
season them with salt, pepper and lemon juice, and dust them
lightly with flour. In a thick frying pan put a good lump of
butter... add 2 tablespoonfuls of Marsala (for 8 pieces)...
```

No structured ingredients block. No servings header. No prep/cook time. Recipes flow directly from prose chapter narrative into method, often separated only by a bold recipe title and Italian name in parentheses.

### Implications for parser
- **Extraction will work** — Gemini is excellent at pulling ingredients and steps from flowing prose
- **Structured fields will be sparse** — `servings`, `prep_time`, `cook_time` will almost always be null; ingredients will be extracted from the method text itself
- **No missed recipes expected** — there is no tabular format to confuse the model
- **False positives are higher** — Store Cupboard (ingredient glossary), Wine Notes, Cookery Books bibliography all pass the heuristic but contain no recipes. Gemini will correctly return empty for these.
- **High false positive rate from `is_recipe_candidate`** — ~8 of 25 candidate segments likely contain no extractable recipes (introductions, glossaries, bibliography, literary sections)

### Contrast with previous books
| Aspect | Forkish (Pizza) | Woks of Life | Italian Food (David) |
|--------|----------------|--------------|----------------------|
| Ingredient format | 3-column weight table | Prose list, one per line | Fully embedded in method |
| Separate ingredients block | Yes (tabular) | Yes (list) | No |
| Servings/timing fields | Explicit | Explicit | Rarely present |
| False positive rate | Low | Low | **High** |
| Expected extraction difficulty | **High** (table format) | **Low** | **Medium** (prose extraction works, fields sparse) |

---

---

## Book 4: Land of Fish and Rice — Fuchsia Dunlop

**File:** `Land of Fish and Rice - Fuchsia Dunlop.epub`
**Segments:** 29 total, 19 passed `is_recipe_candidate`
**Recipes extracted:** Not yet run (API)

### Segment map (candidates only)
| Seg | Chars  | Content |
|-----|--------|---------|
| 6   | 57,134 | The Beautiful South — introduction narrative, likely false positive |
| 7   | 76,466 | Appetizers — recipes ✓ |
| 8   | 47,142 | Meat — recipes ✓ |
| 9   | 45,383 | Poultry and Eggs — recipes ✓ |
| 10  | 59,522 | Fish and Seafood — recipes ✓ |
| 11  | 21,684 | Tofu — recipes ✓ |
| 12  | 58,436 | Vegetables — recipes ✓ |
| 13  | 30,216 | Soups — recipes ✓ |
| 14  | 33,830 | Rice — recipes ✓ |
| 15  | 24,111 | Noodles — recipes ✓ |
| 16  | 34,418 | Dumplings and Snacks — recipes ✓ |
| 17  | 17,937 | Sweet Dishes — recipes ✓ |
| 18  | 14,612 | Drinks — recipes ✓ |
| 19  | 40,247 | Basic Recipes — recipes ✓ |
| 21  | 43,086 | Ingredients Essentials — glossary, likely false positive |
| 23  | 15,368 | Techniques — no recipes |
| 24  | 40,600 | Index — false positive |

### Ingredient format — **Hybrid: prose narrative opening + compact ingredient list**

Each recipe has a long narrative headnote followed by a compact ingredient list (quantity and ingredient on the same line, no headers), then a flowing prose method:

```
4 celery sticks (about 7 oz/200g)
1 large day lily bulb (about 4 oz/100g)
½ tbsp cooking oil
1 tsp sesame oil
Salt
```

Then method flows as connected prose, not numbered steps. Very similar to Woks of Life in ingredient list format — clean, one ingredient per line, quantity inline.

### Notable observations
- Chinese characters throughout (recipe names in Chinese, transliteration, and English)
- Both metric and imperial measurements given inline: `7 oz/200g`, `2¾ in (7cm)`
- No separate ingredient/method section headings — ingredient list flows directly into method prose
- `Serves` / `Makes` markers not consistently present — recipes run straight from headnote into ingredients
- Large chapters (40–76k chars) each containing many recipes — same pattern as Woks of Life

### Contrast with previous books
| Aspect | Forkish | Woks of Life | Italian Food | Land of Fish and Rice |
|--------|---------|--------------|--------------|----------------------|
| Ingredient format | 3-col weight table | Prose list | Fully embedded in prose | Compact list + prose method |
| Separate ingredients block | Yes (tabular) | Yes (list) | No | Yes (minimal list) |
| Dual units (metric+imperial) | Yes (g + cups + %) | No | No | Yes (oz/g, in/cm) |
| Chinese characters | No | Yes | No | Yes |
| Expected parsing difficulty | **High** (table) | **Low** | **Medium** | **Low** |

---

---

## Book 5: Beard on Pasta — James Beard

**File:** `Beard on Pasta - James Beard.epub`
**Segments:** 24 total, 12 passed `is_recipe_candidate`
**Recipes extracted:** Not yet run (API)

### Segment map (candidates only)
| Seg | Chars  | Content |
|-----|--------|---------|
| 6   | 34,125 | Observations — pasta types glossary, likely false positive |
| 7   | 38,486 | Making Pasta — dough recipes ✓ |
| 8   | 4,184  | Pastas in Broth — recipes ✓ |
| 9   | 23,014 | Mainly Vegetable — recipes ✓ |
| 10  | 8,982  | Fish and Seafood — recipes ✓ |
| 11  | 44,598 | Meats — recipes ✓ |
| 12  | 13,092 | Eggs and Cheese — recipes ✓ |
| 13  | 22,777 | Stuffed Pastas — recipes ✓ |
| 14  | 15,675 | Cold Pasta — recipes ✓ |
| 15  | 9,037  | Small Saucings — recipes ✓ |
| 16  | 2,194  | Desserts — recipes ✓ |
| 18  | 29,376 | Index — false positive |

### Ingredient format — **Classic American list format**

Clean, structured: a yield/servings line, then a vertical ingredient list (quantity + ingredient on same line), then prose or numbered method. No tables, no headers within the ingredient block.

```
6 to 8 servings
1 recipe Light Tomato Sauce (p. 67)
1 cup ground cooked meat
Pan juices or ¼ cup cream (optional)
1 pound penne or ziti
```

Some recipes omit a yield line and go straight to ingredients. Very consistent across chapters.

### Notable observations
- Chapter segments are compact (2–44k chars) and well-organised — each chapter is one EPUB document
- Recipes use ALL CAPS headings (`PESTO`, `FRESH TOMATO SAUCE`) — reliable recipe boundary signal
- Servings line appears as standalone text before ingredients (e.g. `6 to 8 servings`, `3 cups`)
- Variations follow each recipe as clearly labelled blocks
- No dual units, no metric, all US measurements
- No Chinese characters or unicode beyond standard fractions
- **Easiest book so far to parse** — clean structure, consistent layout, no surprises

### Contrast with all books
| Aspect | Forkish | Woks of Life | Italian Food | Land of Fish & Rice | Beard on Pasta |
|--------|---------|--------------|--------------|---------------------|----------------|
| Ingredient format | 3-col weight table | Prose list | Fully embedded | Compact list | Classic list |
| Separate ingredients block | Yes (tabular) | Yes | No | Yes | Yes |
| Dual units | Yes (g+cups+%) | No | No | Yes (oz/g) | No |
| Servings/timing | Explicit | Explicit | Rare | Partial | Explicit |
| Expected parsing difficulty | **High** (table) | Low | Medium | Low | **Very low** |

---

---

## Book 6: Classic German Baking — Luisa Weiss

**File:** `Classic German Baking - Luisa Weiss.epub`
**Segments:** 18 total, 12 passed `is_recipe_candidate`
**Recipes extracted:** Not yet run (API)

### Segment map (candidates only)
| Seg | Chars   | Content |
|-----|---------|---------|
| 0   | 7,745   | Pronunciation guide — false positive |
| 1   | 55,265  | Cookies — recipes ✓ |
| 2   | 78,463  | Cakes — recipes ✓ |
| 3   | 78,626  | Yeasted Cakes — recipes ✓ |
| 4   | 75,350  | Tortes and Strudels — recipes ✓ |
| 5   | 45,256  | Savories — recipes ✓ |
| 6   | 95,240  | Breads and Rolls — recipes ✓ |
| 7   | 104,486 | Christmas Favorites — recipes ✓ |
| 8   | 12,366  | Basics — recipes ✓ |
| 9   | 4,922   | Contents — false positive |
| 13  | 36,042  | Acknowledgments — false positive |
| 14  | 21,028  | Index — false positive |

### Ingredient format — **Dual-unit list (US volume / metric weight inline)**

A baking book that gives both US and metric measurements on the same line, in the format `volume/grams`:

```
1⅔ cups, scooped and leveled, minus 1 tablespoon/200g all-purpose flour
⅓ cup plus 1 teaspoon/50g cornstarch
¾ cup/90g confectioners' sugar
14 tablespoons/200g unsalted butter
```

- **No baker's percentage tables** — no `BAKER'S %` column, no three-column layout
- **No standalone gram-only lines** (zero found) — weights are always paired with a volume measure
- Numbered steps (1, 2, 3...) rather than prose method
- `MAKES X` header before ingredients
- German recipe names with English translations provided

### Key finding — **Confirms `BAKER'S %` as a precise trigger**
This is a baking book with weight measurements throughout, yet it uses the dual-unit prose format rather than the Forkish-style table. The `BAKER'S %` / `BAKER'S PERCENTAGE` string is completely absent. This confirms that the table format is specific to books that target professional bakers (who need baker's math), not general baking books for home cooks.

**The `BAKER'S %` string check will not false-positive on this book.**

### Segments with large char counts (>30k)
Six of the recipe chapters exceed 75k chars. These will hit the `MAX_CHUNK_CHARS = 30_000` limit and be split automatically. Expected to work fine after splitting since the format is clean.

### Updated full comparison
| Aspect | Forkish | Woks/Life | Italian Food | Dunlop | Beard | German Baking |
|--------|---------|-----------|--------------|--------|-------|---------------|
| Format | 3-col weight table | Prose list | Embedded prose | Compact list | Classic list | Dual-unit list |
| Baker's % table | **Yes** | No | No | No | No | **No** |
| Dual units (vol/weight) | Yes (separate cols) | No | No | Yes (oz/g) | No | Yes (inline) |
| Parsing difficulty | **High** | Low | Medium | Low | Very low | Low |

---

---

## Book 7: An Invitation to Indian Cooking — Madhur Jaffrey

**File:** `An Invitation to Indian Cooking - Madhur Jaffrey.epub`
**Segments:** 26 total, 15 passed `is_recipe_candidate`
**Recipes extracted:** Not yet run (API)

### Segment map (candidates only)
| Seg | Chars  | Content |
|-----|--------|---------|
| 6   | 45,714 | Introduction — narrative, likely false positive |
| 7   | 11,673 | Note on Flavorings — glossary, false positive |
| 9   | 21,682 | Soups & Appetizers — recipes ✓ |
| 10  | 69,351 | Meat — recipes ✓ (large) |
| 11  | 43,607 | Chicken — recipes ✓ |
| 12  | 31,723 | Fish & Shellfish — recipes ✓ |
| 13  | 28,544 | Barbecue & Kebabs — recipes ✓ |
| 14  | 62,957 | Vegetables — recipes ✓ (large) |
| 15  | 45,271 | Rice — recipes ✓ |
| 16  | 33,538 | Dals — recipes ✓ |
| 17  | 32,197 | Chutneys & Relishes — recipes ✓ |
| 18  | 14,908 | Breads — recipes ✓ |
| 19  | 17,120 | Desserts — recipes ✓ |
| 22  | 7,611  | Glossary — false positive |
| 23  | 20,506 | Index — false positive |

### Ingredient format — **Classic vertical list, US measurements, narrative headnotes**

Each recipe has `SERVES N` or `MAKES N` header, a brief narrative headnote, then a clean vertical ingredient list (quantity + ingredient on one line), then flowing prose method. Almost identical to Beard on Pasta structurally:

```
SERVES 4
4 cloves garlic, peeled and chopped
A piece of fresh ginger, about ½-inch cube, peeled and chopped
½ pound boneless lamb, cut into ¾-inch cubes
2 tablespoons vegetable oil
½ teaspoon ground coriander
½ teaspoon ground cumin
```

- **No baker's percentage tables** — confirmed absent
- Hindi/Urdu recipe names with English translations
- US measurements only (no metric)
- Chapters each begin with a recipe index list, then narrative intro, then recipes

### Final summary across all 7 books

| Book | Genre | Format | Baker's % table | Parsing difficulty |
|------|-------|--------|-----------------|--------------------|
| Forkish — Elements of Pizza | Professional baking | 3-col weight table | **Yes** | **High — broken** |
| Leung — Woks of Life | Chinese home cooking | Prose list, one per line | No | Low |
| David — Italian Food | Literary cooking | Fully embedded prose | No | Medium (sparse fields) |
| Dunlop — Land of Fish & Rice | Chinese regional | Compact list + prose | No | Low |
| Beard — Beard on Pasta | American classic | Classic list | No | Very low |
| Weiss — Classic German Baking | Home baking | Dual-unit list (vol/g) | No | Low |
| Jaffrey — Indian Cooking | Indian home cooking | Classic list + headnote | No | Very low |

**Conclusion:** The baker's percentage table format is exclusively present in books targeting professional or technically-oriented bakers. The trigger `"BAKER'S %" in chunk.upper()` is a precise, non-brittle signal that will fire only when needed and never false-positive on any of the 6 other book types examined.

---

## Candidate Strategies for Table Normalisation

| Strategy | Pros | Cons |
|----------|------|------|
| LLM pre-normalisation (always) | Works for any layout, no pattern matching | Doubles API calls per segment |
| LLM pre-normalisation (triggered) | Cheaper, only runs when tables detected | Trigger detection could miss variants |
| HTML-level table parsing | Catches structure before text stripping | Brittle if tables use non-`<table>` markup (divs, etc.) |
| Prompt engineering only | No extra API calls | May not be reliable enough for badly fragmented text |

**Decision:** Defer until more books analysed.

---
