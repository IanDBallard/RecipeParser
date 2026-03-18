# recipeparser/pdf.py  ← SHIM — will be deleted in Phase 7
# Backward-compat re-export. Import from recipeparser.io.readers.pdf directly.
from recipeparser.io.readers.pdf import (  # noqa: F401
    extract_text_from_pdf,
    load_pdf,
)
