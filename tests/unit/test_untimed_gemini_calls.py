"""The three Gemini call sites that bypassed ``_call_with_retry`` must be bounded.

Task 7c gave ``_call_with_retry`` an HTTP timeout, but three call sites never
went through it and so inherited no bound at all:

* ``gemini.get_embeddings`` — reached from the EMBED stage of *every* run
  (``core/stages/embed.py``) and from ``adapters/api.py``.  This is the one on
  the main path: the 61-minute hang that motivated the timeout could still
  happen here.
* ``gemini.verify_connectivity`` — a preflight probe that swallows every
  exception, so a hang there is both unbounded and silent.
* ``categories.categorise_recipe`` — reached from the ``--recategorize`` CLI
  via ``recategorize.py``.

No real API calls: the client is a stub that records what it was called with.

``HttpOptions.timeout`` is expressed in MILLISECONDS, while
``config.HTTP_TIMEOUT_SECS`` is 180 SECONDS, so the value reaching the SDK must
be 180000.  Asserting mere presence would not catch a seconds/milliseconds
mix-up, which would give every real call an unusable ~0.18s bound — so these
tests assert the exact magnitude.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from recipeparser.config import HTTP_TIMEOUT_SECS


def _timeout_of(config: object) -> int:
    """The timeout carried by a config, whether it is a dict or a typed object.

    ``generate_content`` is given a plain dict; ``embed_content`` is given a
    typed ``EmbedContentConfig``.  Both expose ``http_options.timeout``, just
    through different access syntax.
    """
    if isinstance(config, dict):
        http_options = config.get("http_options")
    else:
        http_options = getattr(config, "http_options", None)
    assert http_options is not None, f"no http_options on {config!r}"
    if isinstance(http_options, dict):
        return http_options["timeout"]
    return http_options.timeout


class TestGetEmbeddings:
    def test_the_embedding_call_carries_the_http_timeout(self):
        from recipeparser.gemini import get_embeddings

        client = MagicMock()
        client.models.embed_content.return_value = SimpleNamespace(
            embeddings=[SimpleNamespace(values=[0.0] * 1536)]
        )

        get_embeddings("some recipe text", client)

        assert client.models.embed_content.call_count == 1
        _, kwargs = client.models.embed_content.call_args
        assert _timeout_of(kwargs["config"]) == HTTP_TIMEOUT_SECS * 1000

    def test_the_embedding_call_keeps_its_output_dimensionality(self):
        """The timeout must be added to the existing config, not replace it —
        a 1536-dimension vector is what the assemble stage expects."""
        from recipeparser.gemini import get_embeddings

        client = MagicMock()
        client.models.embed_content.return_value = SimpleNamespace(
            embeddings=[SimpleNamespace(values=[0.0] * 1536)]
        )

        get_embeddings("some recipe text", client)

        _, kwargs = client.models.embed_content.call_args
        assert kwargs["config"].output_dimensionality == 1536


class TestVerifyConnectivity:
    def test_the_connectivity_probe_carries_the_http_timeout(self):
        from recipeparser.gemini import verify_connectivity

        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text="OK")

        assert verify_connectivity(client) is True

        _, kwargs = client.models.generate_content.call_args
        assert _timeout_of(kwargs["config"]) == HTTP_TIMEOUT_SECS * 1000

    def test_the_connectivity_probe_still_fails_fast_without_retrying(self):
        """A preflight probe must not burn the five-retry ladder on a dead key:
        the whole point is a quick verdict before real work starts."""
        from recipeparser.gemini import verify_connectivity

        client = MagicMock()
        client.models.generate_content.side_effect = RuntimeError("503 unavailable")

        assert verify_connectivity(client) is False
        assert client.models.generate_content.call_count == 1


class TestCategoriseRecipe:
    @staticmethod
    def _recipe():
        return SimpleNamespace(name="Boiled Custard", ingredients=["milk", "eggs"], notes="")

    def test_the_category_call_carries_the_http_timeout(self):
        from recipeparser.categories import categorise_recipe

        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text='["Dessert"]')

        result = categorise_recipe(self._recipe(), [], ["Dessert", "Bread"], client)

        assert result == ["Dessert"]
        _, kwargs = client.models.generate_content.call_args
        assert _timeout_of(kwargs["config"]) == HTTP_TIMEOUT_SECS * 1000

    def test_a_transient_server_error_is_retried_rather_than_falling_back(self):
        """Routing through _call_with_retry is what buys this: a bulk
        --recategorize run should survive one 503 rather than silently
        dropping that recipe into the EPUB Imports bucket."""
        from google.genai import errors as genai_errors

        from recipeparser.categories import categorise_recipe

        transient = genai_errors.ServerError.__new__(genai_errors.ServerError)
        transient.code = 503
        transient.status = "UNAVAILABLE"
        transient.message = "unavailable"
        transient.args = ("unavailable",)

        client = MagicMock()
        client.models.generate_content.side_effect = [
            transient,
            SimpleNamespace(text='["Bread"]'),
        ]

        result = categorise_recipe(self._recipe(), [], ["Dessert", "Bread"], client)

        assert result == ["Bread"]
        assert client.models.generate_content.call_count == 2
