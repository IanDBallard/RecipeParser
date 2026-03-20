# tests/transient/ — Shadow-execution tests (Phase 6 §11.2)
#
# These tests run the new RecipePipeline path in parallel with the legacy
# engine and assert that outputs are structurally equivalent.  They require
# live Gemini API credentials and are therefore excluded from the standard
# unit-test suite (marked with @pytest.mark.transient).
#
# Run with:
#   pytest tests/transient/ -v -m transient
