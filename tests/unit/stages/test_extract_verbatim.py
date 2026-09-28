"""EXTRACT copies lines verbatim and refuses a rewrite by name (verbatim-ingestion D2)."""
from unittest.mock import MagicMock, patch

from recipeparser.core.stages.extract import Extraction, extract
from recipeparser.models import RecipeExtraction, RecipeList
from tests.unit.test_numbers import NYT_PAGE, REWRITTEN, VERBATIM

_EXTRACT = "recipeparser.core.stages.extract.extract_recipes"
_PLAIN = "recipeparser.core.stages.extract.extract_recipe_from_text"


def _carbonara(lines):
    return RecipeList(recipes=[RecipeExtraction(name="Spaghetti Carbonara", ingredients=lines, directions=["Cook."])])


def test_a_verbatim_extraction_is_kept_on_the_first_call():
    with patch(_EXTRACT, return_value=_carbonara(VERBATIM)) as fn:
        result = extract(NYT_PAGE, client=MagicMock())
    assert isinstance(result, Extraction)
    assert [r.name for r in result.recipes] == ["Spaghetti Carbonara"]
    assert result.rewritten == []
    assert fn.call_count == 1


def test_a_rewrite_is_extracted_once_more_and_the_verbatim_retry_kept():
    with patch(_EXTRACT, side_effect=[_carbonara(REWRITTEN), _carbonara(VERBATIM)]) as fn:
        result = extract(NYT_PAGE, client=MagicMock())
    assert result.recipes[0].ingredients == VERBATIM
    assert result.rewritten == []
    assert fn.call_count == 2


def test_a_rewrite_twice_is_dropped_by_name():
    with patch(_EXTRACT, side_effect=[_carbonara(REWRITTEN), _carbonara(REWRITTEN)]) as fn:
        result = extract(NYT_PAGE, client=MagicMock())
    assert result.recipes == []
    assert result.rewritten == ["Spaghetti Carbonara"]
    assert fn.call_count == 2


def test_only_the_rewritten_recipe_in_a_chunk_is_dropped():
    page = NYT_PAGE + "\nShortbread\n250g butter\n"
    shortbread = RecipeExtraction(name="Shortbread", ingredients=["250g butter"], directions=["Bake."])
    both = RecipeList(recipes=[_carbonara(REWRITTEN).recipes[0], shortbread])
    with patch(_EXTRACT, side_effect=[both, both]):
        result = extract(page, client=MagicMock())
    assert [r.name for r in result.recipes] == ["Shortbread"]
    assert result.rewritten == ["Spaghetti Carbonara"]


def test_a_number_the_table_normaliser_wrote_counts_as_the_writers():
    table = "Flour 100%\nWater 70%"
    normalised = "500g flour\n350g water"
    loaf = RecipeList(recipes=[RecipeExtraction(name="Loaf", ingredients=["500g flour", "350g water"], directions=["Mix."])])
    with patch("recipeparser.core.stages.extract.needs_table_normalisation", return_value=True), \
         patch("recipeparser.core.stages.extract.normalise_baker_table", return_value=normalised), \
         patch(_EXTRACT, return_value=loaf) as fn:
        result = extract(table, client=MagicMock())
    assert [r.name for r in result.recipes] == ["Loaf"]
    assert fn.call_count == 1


def test_plain_text_mode_retries_through_the_plain_text_prompt():
    with patch(_PLAIN, side_effect=[_carbonara(REWRITTEN), _carbonara(VERBATIM)]) as fn:
        result = extract(NYT_PAGE, client=MagicMock(), plain_text_mode=True)
    assert result.recipes[0].ingredients == VERBATIM
    assert fn.call_count == 2


# The retry replaces only the recipes that failed, and every failed name not recovered is named (D2).
_A_OK = RecipeExtraction(name="A", ingredients=["2 eggs"], directions=["Cook."])
_A_BAD = RecipeExtraction(name="A", ingredients=["99 eggs"], directions=["Cook."])
_B_OK = RecipeExtraction(name="B", ingredients=["3 cups flour"], directions=["Bake."])
_B_BAD = RecipeExtraction(name="B", ingredients=["340g flour"], directions=["Bake."])
_AB_PAGE = "A\n2 eggs\nB\n3 cups flour\n"


def _run_ab(first, retry):
    with patch(_EXTRACT, side_effect=[RecipeList(recipes=first), RecipeList(recipes=retry)]) as fn:
        result = extract(_AB_PAGE, client=MagicMock())
    assert fn.call_count == 2
    return result


def test_the_retry_recovers_only_the_recipe_that_failed():
    result = _run_ab([_A_OK, _B_BAD], [_B_OK])
    assert [r.name for r in result.recipes] == ["A", "B"]
    assert result.recipes[1].ingredients == ["3 cups flour"]
    assert result.rewritten == []


def test_a_clean_first_attempt_survives_a_retry_that_rewrites_it():
    result = _run_ab([_A_OK, _B_BAD], [_A_BAD, _B_BAD])
    assert result.recipes == [_A_OK]
    assert result.rewritten == ["B"]


def test_a_retry_that_omits_the_failed_recipe_names_it():
    result = _run_ab([_A_OK, _B_BAD], [])
    assert result.recipes == [_A_OK]
    assert result.rewritten == ["B"]


def test_a_recipe_only_the_retry_found_is_ignored():
    stray = RecipeExtraction(name="C", ingredients=["2 eggs"], directions=["Mix."])
    result = _run_ab([_A_OK, _B_BAD], [_B_OK, stray])
    assert [r.name for r in result.recipes] == ["A", "B"]


def test_a_retry_that_raises_keeps_the_clean_recipes_and_names_the_failed_ones():
    # A retry that fails outright (an unparseable reply, a rate limit past the call's own retries)
    # must not cost the first attempt's clean recipes.
    from recipeparser.exceptions import ExtractionParseError

    with patch(_EXTRACT, side_effect=[RecipeList(recipes=[_A_OK, _B_BAD]), ExtractionParseError("truncated")]) as fn:
        result = extract(_AB_PAGE, client=MagicMock())
    assert fn.call_count == 2
    assert result.recipes == [_A_OK]
    assert result.rewritten == ["B"]


# Fix Roadmap F-012 (RecipeParser#59's final review, ruling 7): the pipeline took one limiter slot before
# extract(), and the number-guard retry and the baker's-table call made further Gemini calls on it.
class _Calls:
    """A limiter and the Gemini stand-ins writing to one log, so the order of slot and call shows."""

    def __init__(self):
        self.log = []
        self.limiter = MagicMock()
        self.limiter.wait_then_record_start.side_effect = lambda: self.log.append("slot")

    def returning(self, name, *values):
        replies = iter(values)

        def call(*a, **kw):
            self.log.append(name)
            return next(replies)
        return call


def test_the_number_guard_retry_takes_its_own_limiter_slot():
    calls = _Calls()
    with patch(_EXTRACT, side_effect=calls.returning("extract", _carbonara(REWRITTEN), _carbonara(VERBATIM))):
        extract(NYT_PAGE, client=MagicMock(), limiter=calls.limiter)
    assert calls.log == ["slot", "extract", "slot", "extract"]


def test_the_bakers_table_call_takes_its_own_limiter_slot():
    calls = _Calls()
    loaf = RecipeList(recipes=[RecipeExtraction(name="Loaf", ingredients=["500g flour"], directions=["Mix."])])
    with patch("recipeparser.core.stages.extract.needs_table_normalisation", return_value=True), \
         patch("recipeparser.core.stages.extract.normalise_baker_table",
               side_effect=calls.returning("table", "500g flour")), \
         patch(_EXTRACT, side_effect=calls.returning("extract", loaf)):
        extract("Flour 100%", client=MagicMock(), limiter=calls.limiter)
    assert calls.log == ["slot", "table", "slot", "extract"]


def test_with_no_limiter_extract_still_runs():
    # The goldens and the CLI's direct callers pass none.
    with patch(_EXTRACT, return_value=_carbonara(VERBATIM)):
        assert extract(NYT_PAGE, client=MagicMock()).recipes
