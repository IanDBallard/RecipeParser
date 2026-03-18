# recipeparser/api.py  ← SHIM — will be deleted in Phase 7
# Canonical location: recipeparser/adapters/api.py
#
# IMPORTANT: We use the sys.modules alias trick so that
#   patch("recipeparser.api._get_client")
# and
#   patch("recipeparser.adapters.api._get_client")
# are identical operations on the same module object.
# A simple "from ... import" shim does NOT work for patch() targets because
# the endpoint code calls its own local binding, not the shim's copy.
import sys
import recipeparser.adapters.api as _canonical

sys.modules[__name__] = _canonical
