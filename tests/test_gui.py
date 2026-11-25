"""
Tests for recipeparser.gui — run-config logic used by the Parse tab.

GUI tests here are logic-only: we test the function that turns free-tier and
concurrency settings into (rpm, concurrency) for the pipeline. We do not
instantiate CustomTkinter widgets or require a display; full UI tests would
need a headless CTk or display server.
"""
import pytest

from recipeparser.adapters.gui import CategoryEditorFrame, _parse_run_config


class TestParseRunConfig:

    def test_free_tier_returns_rpm_5_concurrency_1(self):
        rpm, concurrency = _parse_run_config(True, "1")
        assert rpm == 5
        assert concurrency == 1

    def test_free_tier_ignores_concurrency_str(self):
        """When free tier is on, concurrency is always 1 regardless of dropdown."""
        rpm, concurrency = _parse_run_config(True, "10")
        assert rpm == 5
        assert concurrency == 1

    def test_paid_tier_no_rpm_concurrency_used(self):
        rpm, concurrency = _parse_run_config(False, "5")
        assert rpm is None
        assert concurrency == 5

    def test_paid_tier_concurrency_clamped_to_10(self):
        rpm, concurrency = _parse_run_config(False, "15")
        assert rpm is None
        assert concurrency == 10

    def test_paid_tier_concurrency_clamped_to_1(self):
        rpm, concurrency = _parse_run_config(False, "0")
        assert rpm is None
        assert concurrency == 1

    def test_paid_tier_concurrency_1_and_10_valid(self):
        _, c1 = _parse_run_config(False, "1")
        _, c10 = _parse_run_config(False, "10")
        assert c1 == 1
        assert c10 == 10


class TestCategoryEditorLoad:
    """An unreadable categories.yaml must never look like an empty one (a later Save would
    overwrite the user's file). The editor's methods run against a stand-in for the widget,
    so no display is needed."""

    @staticmethod
    def _editor():
        from unittest.mock import MagicMock

        editor = MagicMock()
        editor._data = {"Mains": ["Beef"]}
        editor._order = ["Mains"]
        editor._save_blocked = False
        return editor

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("categories: [unclosed\n", id="bad-yaml"),
            pytest.param("- just\n- a list\n", id="top-level-not-a-mapping"),
            pytest.param("categories: Mains\n", id="categories-not-a-list"),
        ],
    )
    def test_an_unreadable_file_is_reported_and_replaces_nothing(self, tmp_path, content):
        from unittest.mock import patch


        path = tmp_path / "categories.yaml"
        path.write_text(content, encoding="utf-8")
        editor = self._editor()
        with patch("recipeparser.adapters.gui.messagebox") as box:
            loaded = CategoryEditorFrame._load(editor, path)

        assert loaded is False
        box.showerror.assert_called_once()
        assert str(path) in box.showerror.call_args.args[1]
        assert editor._data == {"Mains": ["Beef"]} and editor._order == ["Mains"]

    def test_a_missing_file_is_the_first_run_and_loads_empty(self, tmp_path):
        from unittest.mock import patch


        editor = self._editor()
        with patch("recipeparser.adapters.gui.messagebox") as box:
            loaded = CategoryEditorFrame._load(editor, tmp_path / "missing.yaml")

        assert loaded is True
        box.showerror.assert_not_called()
        assert editor._data == {} and editor._order == []

    def test_a_readable_file_loads_its_tree(self, tmp_path):

        path = tmp_path / "categories.yaml"
        path.write_text("categories:\n  - Soups\n  - Mains:\n      - Beef\n", encoding="utf-8")
        editor = self._editor()

        assert CategoryEditorFrame._load(editor, path) is True
        assert editor._order == ["Soups", "Mains"]
        assert editor._data == {"Soups": [], "Mains": ["Beef"]}

    def test_save_is_refused_while_the_startup_file_was_unreadable(self, tmp_path):
        from unittest.mock import patch


        path = tmp_path / "categories.yaml"
        path.write_text("categories: [unclosed\n", encoding="utf-8")
        editor = self._editor()
        editor._save_blocked = True
        with patch("recipeparser.adapters.gui.messagebox") as box:
            CategoryEditorFrame._save(editor, path)

        box.showerror.assert_called_once()
        assert path.read_text(encoding="utf-8") == "categories: [unclosed\n"
