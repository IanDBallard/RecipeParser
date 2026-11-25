# Gemini model and SDK plan

**Status:** proposed, 2026-11-25. **Owner:** the repository owner.
**Scope:** which Gemini models RecipeParser calls, through which SDK and API, and what has
to change before the current generation model is retired (earliest May 7, 2027).

Facts about Google's platform below were read from Google's own pages on 2026-11-25
(sources at the end). Model availability and prices change; re-check them before acting.

## 1. Where we are (master `4971ffb`)

### Models

| Setting (`recipeparser/config.py`) | Default | Used for |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Every generation call: extraction, REFINE, mentions, temperatures, pans, yields, servings estimate, TAG (`categorize_batch`), baker-table normalising, vision OCR, shopping classify |
| `GEMINI_FOOD_CHECK_MODEL` | `GEMINI_MODEL` | The picture food check (`ask_is_food`) |
| `GEMINI_IMAGE_MODEL` | `gemini-3.1-flash-image` | AI recipe pictures (`core/picture_gen.py`) |
| `GEMINI_EMBEDDING_MODEL` | `models/gemini-embedding-001` | EMBED and `/embed` (1536 dimensions) |
| `GEMINI_DECISION_MODEL` | `gemini-decision-1` | `POST /shopping/aisles` (since 9.39.0) |

One model drives every generation call. `config.py` records why it stays on the
flash-lite tier: tagging scored 147/160 there against 96/96 on `gemini-3.8-flash`
(ruled 2026-10-03), and `gemini-3.5-flash-lite` "returned no usable refinement in 42 of
42 runs". Those figures were measured on 2026-10-03, three days before sampling
parameters were stripped (#100, 2026-10-06), so they describe the old sampling.

### SDK and API

- **google-genai 1.68.0** installed (`pyproject.toml` allows `>=1.38.0`, `requirements.txt`
  `>=1.60.0`). The latest release is 3.6.1.
- Every generation call goes through `client.models.generate_content` via
  `gemini._call_with_retry`, stateless, mostly with JSON structured output, thinking
  level `minimal` (`GEMINI_THINKING_LEVEL`).
- **The Decisions API is already in use,** for `/shopping/aisles`, through a hand-written
  httpx client (`shopping_aisles.DecisionsClient`, shaped like the SDK's
  `client.decisions.create`). The SDK's own client needs google-genai >= 3.2, which
  requires pydantic >= 2.12.5 (we pin 2.11.9) and moves onto httpx2, so every
  `except httpx.TimeoutException` in the routes would stop matching (`config.py`,
  `GEMINI_DECISIONS_URL`).
- `_finalize_config` strips `temperature`, `top_p`, `top_k`, `thinking_budget`. Four
  callers still pass `temperature: 0` and the docs still say TAG samples at 0.0; the
  removal is in progress (workstream 1).

## 2. What changed on Google's side

- **Sampling parameters.** Gemini 4 and Gemini 3.6+ ignore `temperature`, `top_p` and
  `top_k`, send a deprecation warning header, and will return 400 in a future release.
  For earlier Gemini 3 models (ours), Google says keep them at their defaults. So no call
  here can be made deterministic by temperature; determinism comes from structured
  output, closed answer sets and our own validation.
- **Retirement.** `gemini-3.1-flash-lite`: earliest shutdown **May 7, 2027**, named
  replacement `gemini-3.5-flash-lite`. `gemini-embedding-001`: earliest shutdown
  **May 14, 2028**, replacement `gemini-embedding-2` (multimodal).
- **New models.** Gemini 4: `gemini-4-hydrogen`, `-carbon`, `-neon`, `-argon`. There is
  no Gemini 4 Flash or Flash-Lite. Latest 3.x: `gemini-3.8-flash` and
  `gemini-3.6-flash`, `gemini-3.5-flash-lite`.
- **Prices** (standard tier, per 1M tokens, input / output):

  | Model | Price | Note |
  |---|---|---|
  | `gemini-3.1-flash-lite` | $0.25 / $1.50 | ours today |
  | `gemini-3.5-flash-lite` | $0.30 / $2.50 | named replacement |
  | `gemini-3.8-flash` | $0.75 / $3.75 | $1.50 / $7.50 from Jan 1, 2027 |
  | `gemini-4-hydrogen` | $1.50 / $7.50 | cheapest Gemini 4 |
  | `gemini-4-carbon`, `-argon` | $2.00 / $10.00 | $4.00 / $20.00 from Dec 1, 2026 |
  | `gemini-4-neon` | $3.00 / $15.00 | |
  | `gemini-decision-1` | $0.10 / $0 | output tokens are free |

- **Decisions API** (`gemini-decision-1`): up to 1,000 yes/no, choice or rubric-score
  questions over one shared input (up to 256K tokens), answered as probabilities with a
  0–1 confidence each, not text. `v1beta`, AI Studio only; Vertex "coming soon".
- **Interactions API** is generally available and recommended for new projects;
  `generateContent` is "legacy but fully supported". Interactions is built for stateful
  multi-turn work. Batch, explicit caching and Python automatic function calling remain
  `generateContent`-only.

## 3. Principles

1. **Measure before moving.** No model, SDK or API change lands without a before/after
   number on the goldens and the tagging sample, at today's sampling.
2. **The right tool per call.** Closed-set questions (tag, yes/no, pick one) go to the
   Decisions API; text that must be written (extract, REFINE, mentions, OCR) stays on a
   generation model. One `GEMINI_MODEL` for everything is retired.
3. **Pay for accuracy only where it shows.** A costlier generation model is used per
   task, where the measurement shows it is worth it, never across the board.
4. **No silent parameters.** A request setting the API ignores is refused in our code,
   not dropped (workstream 1).

## 4. Workstreams

### W1 — Stop claiming temperature 0 (in progress)

Remove `temperature`/`top_p`/`top_k`/`thinking_budget` from every caller, make
`_finalize_config` raise when one is passed, correct ARCHITECTURE.md and the `config.py`
notes. **Done when** merged.

### W2 — A measurement baseline at today's sampling (first, small)

Re-run the tagging sample (the 14-recipe set behind the `config.py` figures, and
`tests/goldens/test_tagging_golden.py`) and the stage goldens live against
`gemini-3.1-flash-lite` with default sampling. Record per-axis accuracy, refine failure
rate, tokens and cost per recipe in a dated results file. Make the script reusable with a
`--model` flag, so every later workstream is one command. **Done when** the numbers are
committed and `config.py`'s figures cite them.

### W3 — Decisions API for the closed-set calls (high value)

Candidates, in order:

| Call | Today | As a Decisions request |
|---|---|---|
| TAG (`categorize_batch`) | one generation call per axis per five recipes | one `choice` question per recipe per axis, candidates = the axis's categories plus "none of these"; all axes of a batch in one request; a winner under a confidence floor leaves the axis empty |
| Picture food check (`ask_is_food`) | one generation call per picture | one `yes/no` question per picture |
| Recategorise (`RecatWorker`) | same path as TAG | same as TAG |

The "none of these" candidate and the confidence floor aim straight at the failure the
flash-lite tier cannot be prompted out of: reaching for the nearest ingredient when an
axis should be empty. Steps:

1. Move `DecisionsClient` out of `shopping_aisles.py` into a shared adapter, with the
   aisles endpoint as its first user, unchanged.
2. A side-by-side run of TAG on W2's sample (generation vs Decisions), with the floor
   chosen from the measured confidences, not guessed.
3. Switch TAG (and recategorise) behind a setting if Decisions is at least as accurate;
   then the food check.

**Done when** TAG's accuracy and cost per recipe are measured on both paths and the
better one is the default.

### W4 — google-genai 1.68 → 3.x (its own PR, before W5)

1. Raise pydantic to >= 2.12.5 and run the suite (pydantic validates every model here).
2. Upgrade google-genai; move every `except httpx.…` at a Gemini call site to what the SDK
   raises under httpx2 (or to the SDK's own error types). Grep for them first and list
   them in the PR.
3. Replace the hand-written `DecisionsClient` with `client.decisions.create`.
4. Read the 2.0 and 3.0 release notes for removed or renamed config fields; the goldens'
   recorded request shapes (`prompt_sha256`) will show any change in what we send.

**Done when** the suite and goldens pass on 3.x and no hand-written Gemini HTTP client is
left.

### W5 — A successor to `gemini-3.1-flash-lite` (before May 7, 2027)

1. Split `GEMINI_MODEL` into per-task settings (extraction and REFINE, mentions and
   tokens, OCR, shopping classify), each defaulting to today's model, so tasks can move
   separately.
2. Using W2's script, measure `gemini-3.5-flash-lite` again (the 42-of-42 failure was at
   the old sampling and thinking level; try `minimal` and `low`), then `gemini-3.6-flash`,
   `gemini-3.8-flash` and `gemini-4-hydrogen`, per task.
3. Pick per task on accuracy and cost per recipe; write the ruling and its numbers into
   `config.py` next to each setting, replacing the 2026-10-03 note.

**Done when** no task depends on a model with a shutdown date inside the next six months.
**Deadline:** decided by February 2027, deployed by April 2027.

### W6 — Embeddings to `gemini-embedding-2` (by early 2028)

Check whether it can still give 1536 dimensions (the column and Cayenne's search depend on
it); plan a re-embed backfill over every recipe with both columns live during the switch,
and measure search quality on the AI-search goldens before and after. **Deadline:**
deployed by February 2028.

### Not doing: the Interactions API

Every call here is a stateless one-shot request with structured output, the case
`generateContent` serves and Interactions adds nothing to, and Batch and explicit caching
exist only on `generateContent`. Revisit if `generateContent` gets a shutdown date or a
multi-turn feature (the AI search agent runs on Anthropic, not Gemini) needs it.

## 5. Order and dates

| When | What |
|---|---|
| Now | W1 (in progress), W2 |
| December 2026 | W3 TAG side-by-side; W4 pydantic step |
| January 2027 | W4 SDK upgrade; W3 switch if it wins |
| February 2027 | W5 measurements and ruling |
| April 2027 | W5 deployed (shutdown earliest May 7) |
| By February 2028 | W6 |

Also check `gemini-3.1-flash-image`'s shutdown date on the deprecations page; it was not
reviewed here.

## 6. Decisions for the owner

1. **Budget:** may a task move to a pricier tier (3.8 Flash or Gemini 4 Hydrogen) where
   W5 shows a clear accuracy win, or does flash-lite pricing stay the ceiling?
2. **Decisions API maturity:** it is `v1beta` and AI Studio only. Acceptable for the
   import path (TAG, food check), as it already is for aisles?
3. **Order of W3 and W4:** do W3 on the existing httpx client now (faster), or wait for
   W4 and use the SDK's client (one path from the start)?

## Sources

- Troubleshooting (sampling parameters): https://aistudio.google.com/docs/troubleshooting
- Models: https://aistudio.google.com/docs/models
- Deprecations (shutdown dates): https://aistudio.google.com/docs/deprecations
- Pricing: https://aistudio.google.com/docs/pricing
- Interactions API overview: https://aistudio.google.com/docs/interactions-overview
- Decisions API launch (2026-11-11): https://blog.google/innovation-and-ai/technology/developers-tools/gemini-decisions-api/
