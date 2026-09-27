"""The tier measurement's arithmetic, with no network in sight."""
from __future__ import annotations

import math

import numpy as np

from scripts.measure_similarity_tiers import (
    band_table,
    normalise_title,
    pages_with_neighbour,
    top_different_title_pairs,
    unit_matrix,
)


def _at(c: float, axis: int = 1, dim: int = 4) -> list[float]:
    """A unit vector at cosine c to the first axis."""
    v = [0.0] * dim
    v[0] = c
    v[axis] = math.sqrt(1 - c * c)
    return v


def _sims(vectors: list[list[float]]) -> np.ndarray:
    m = unit_matrix(vectors)
    return m @ m.T


class TestNormaliseTitle:
    def test_folds_case_and_punctuation(self):
        assert normalise_title("Corned Beef Plate-Pie!") == normalise_title("corned beef plate pie")

    def test_survives_a_missing_title(self):
        assert normalise_title(None) == ""


class TestUnitMatrix:
    def test_rows_are_unit_length_and_a_zero_row_stays_zero(self):
        m = unit_matrix([[3.0, 4.0], [0.0, 0.0]])
        assert np.allclose(m[0], [0.6, 0.8])
        assert np.allclose(m[1], [0.0, 0.0])


class TestBandTable:
    def test_counts_each_pair_once_by_band_and_title(self):
        # a/b same title at cosine 1; a/c and b/c different titles at 0.92; d unrelated.
        sims = _sims([_at(1), _at(1), _at(0.92), _at(0, axis=2)])
        titles = ["pie", "pie", "plate pie", "posset"]
        table = {(lo, hi): (same, diff) for lo, hi, same, diff in band_table(sims, titles)}
        assert table[(0.99, 1.01)] == (1, 0)
        assert table[(0.91, 0.93)] == (0, 2)
        assert sum(s + d for s, d in table.values()) == 3  # the three pairs above 0.80


class TestPagesWithNeighbour:
    def test_counts_pages_and_the_most_neighbours_any_page_has(self):
        sims = _sims([_at(1), _at(1), _at(0.92), _at(0, axis=2)])
        assert pages_with_neighbour(sims, 0.95) == (2, 1)
        assert pages_with_neighbour(sims, 0.89) == (3, 2)

    def test_never_counts_a_recipe_as_its_own_neighbour(self):
        assert pages_with_neighbour(_sims([_at(1), _at(0, axis=2)]), 0.5) == (0, 0)


class TestTopDifferentTitlePairs:
    def test_skips_same_title_pairs_and_orders_by_score(self):
        sims = _sims([_at(1), _at(1), _at(0.92), _at(0.9, axis=3)])
        titles = ["pie", "pie", "plate pie", "tart"]
        pairs = top_different_title_pairs(sims, titles, 10)
        assert all(titles[i] != titles[j] for _, i, j in pairs)
        scores = [s for s, _, _ in pairs]
        assert scores == sorted(scores, reverse=True)
        assert (0, 1) not in [(i, j) for _, i, j in pairs]
