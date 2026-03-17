#!/usr/bin/env python3
"""
live_api_test.py — Live integration test harness for the RecipeParser FastAPI.

Requires the Docker container running at http://localhost:8000 with DISABLE_AUTH=1.
Dependencies: httpx, rich  (pip install httpx rich)

Usage:
    python tests/live_api_test.py                  # run all tests
    python tests/live_api_test.py --text-only       # only POST /ingest
    python tests/live_api_test.py --url-only        # only POST /ingest/url
    python tests/live_api_test.py --embed-only      # only POST /embed
    python tests/live_api_test.py --base http://192.168.2.46:8000

API contract (new):
    All /ingest* endpoints return 202 Accepted with { job_id, recipe_id }.
    The API writes the recipe directly to Supabase; the client receives it via PowerSync.
"""

import argparse
import sys
import time
from typing import Any

import httpx
from rich.console import Console
from rich.table import Table

# supabase_writer provides verify/delete — reuse the same logic as the API itself
from recipeparser.supabase_writer import (
    delete_recipe_from_supabase,
    verify_recipe_in_supabase,
)

console = Console()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PANCAKE_TEXT = """
Classic Buttermilk Pancakes
Servings: 4

Ingredients:
- 1.5 cups all-purpose flour
- 2 tbsp sugar
- 1 tsp baking powder
- 0.5 tsp baking soda
- 0.25 tsp salt
- 1.25 cups buttermilk
- 1 large egg
- 2 tbsp melted butter

Directions:
1. Whisk together flour, sugar, baking powder, baking soda, and salt in a large bowl.
2. In a separate bowl, whisk buttermilk, egg, and melted butter.
3. Pour wet ingredients into dry ingredients and stir until just combined (lumps are fine).
4. Heat a non-stick skillet over medium heat and lightly grease with butter.
5. Pour 1/4 cup batter per pancake. Cook until bubbles form on surface, about 2 minutes.
6. Flip and cook until golden brown, about 1 more minute.
7. Serve immediately with maple syrup.
"""

COOKIE_URL = "https://www.simplyrecipes.com/recipes/homemade_pizza/"
EMBED_QUERY = "chocolate chip cookies with brown butter"

# ---------------------------------------------------------------------------
# Storage cleanup — delete the image uploaded by /ingest/url (best-effort)
# ---------------------------------------------------------------------------

def _cleanup_storage_image(image_url: str) -> None:
    """
    Delete a file from Supabase Storage using the service key.

    The API uploads to:
      {SUPABASE_URL}/storage/v1/object/public/recipe-images/{user_id}/{recipe_id}.ext

    The DELETE endpoint is:
      DELETE {SUPABASE_URL}/storage/v1/object/recipe-images/{path}

    This is best-effort — failure is logged but does not affect test results.
    """
    import os
    from dotenv import load_dotenv
    load_dotenv()

    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_KEY", "")

    if not supabase_url or not service_key:
        console.print("  [yellow]⚠ Cleanup skipped: SUPABASE_URL/SUPABASE_SERVICE_KEY not set[/yellow]")
        return

    # Extract the storage path from the public URL.
    # Public URL format: {supabase_url}/storage/v1/object/public/recipe-images/{path}
    marker = "/storage/v1/object/public/recipe-images/"
    idx = image_url.find(marker)
    if idx == -1:
        console.print(f"  [yellow]⚠ Cleanup skipped: could not parse storage path from {image_url!r}[/yellow]")
        return

    storage_path = image_url[idx + len(marker):]
    delete_url = f"{supabase_url}/storage/v1/object/recipe-images/{storage_path}"

    try:
        resp = httpx.delete(
            delete_url,
            headers={"Authorization": f"Bearer {service_key}"},
            timeout=15,
        )
        if resp.status_code in (200, 204):
            console.print(f"  [dim]🗑  Storage cleanup OK: recipe-images/{storage_path}[/dim]")
        else:
            console.print(f"  [yellow]⚠ Storage cleanup returned {resp.status_code}: {resp.text[:120]}[/yellow]")
    except Exception as exc:
        console.print(f"  [yellow]⚠ Storage cleanup failed: {exc}[/yellow]")


def _validate_job_response(data: dict[str, Any]) -> list[str]:
    """Validate a JobResponse payload { job_id, recipe_id }. Returns list of errors."""
    errors = []
    for field in ("job_id", "recipe_id"):
        val = data.get(field)
        if not isinstance(val, str) or not val:
            errors.append(f"'{field}' missing or empty in JobResponse")
    return errors


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.errors: list[str] = []
        self.elapsed: float = 0.0
        self.detail: str = ""

    def fail(self, *msgs: str) -> "TestResult":
        self.errors.extend(msgs)
        return self

    def ok(self, detail: str = "") -> "TestResult":
        self.passed = True
        self.detail = detail
        return self


def test_health(base: str) -> TestResult:
    r = TestResult("GET /docs → 200")
    t0 = time.perf_counter()
    try:
        resp = httpx.get(f"{base}/docs", timeout=10)
        r.elapsed = time.perf_counter() - t0
        if resp.status_code == 200:
            r.ok("OpenAPI docs reachable")
        else:
            r.fail(f"status {resp.status_code}")
    except Exception as exc:
        r.elapsed = time.perf_counter() - t0
        r.fail(str(exc))
    return r


def test_ingest_text(base: str) -> TestResult:
    r = TestResult("POST /ingest → 202 + DB write (pancake text)")
    t0 = time.perf_counter()
    recipe_id: str | None = None
    try:
        resp = httpx.post(
            f"{base}/ingest",
            json={"text": PANCAKE_TEXT},
            timeout=120,
        )
        r.elapsed = time.perf_counter() - t0
        if resp.status_code != 202:
            return r.fail(f"expected 202, got {resp.status_code}", resp.text[:300])
        data = resp.json()
        errs = _validate_job_response(data)
        if errs:
            return r.fail(*errs)
        recipe_id = data["recipe_id"]
        job_id = data["job_id"]

        # --- Verify the API wrote the recipe to Supabase ---
        # We don't know the title or ingredient count ahead of time, so we do a
        # minimal existence + embedding check via verify_recipe_in_supabase.
        # Pass expected_ing_count=0 to skip the count assertion (any count is fine).
        db_errs = verify_recipe_in_supabase(recipe_id, expected_title="", expected_ing_count=-1)
        # Filter out title/count errors — we only care that the row exists with an embedding
        db_errs = [e for e in db_errs if "not found" in e or "embedding" in e]
        if db_errs:
            return r.fail(*[f"DB: {e}" for e in db_errs])

        r.ok(f"job_id={job_id}, recipe_id={recipe_id}, DB write confirmed")
    except Exception as exc:
        r.elapsed = time.perf_counter() - t0
        r.fail(str(exc))
    finally:
        if recipe_id:
            delete_recipe_from_supabase(recipe_id)
            console.print(f"  [dim]🗑  DB cleanup OK: recipes/{recipe_id}[/dim]")
    return r


def test_ingest_url(base: str) -> TestResult:
    r = TestResult(f"POST /ingest/url → 202 + DB write ({COOKIE_URL[:40]}…)")
    t0 = time.perf_counter()
    recipe_id: str | None = None
    try:
        resp = httpx.post(
            f"{base}/ingest/url",
            json={"url": COOKIE_URL},
            timeout=180,
        )
        r.elapsed = time.perf_counter() - t0
        if resp.status_code != 202:
            return r.fail(f"expected 202, got {resp.status_code}", resp.text[:300])
        data = resp.json()
        errs = _validate_job_response(data)
        if errs:
            return r.fail(*errs)
        recipe_id = data["recipe_id"]
        job_id = data["job_id"]

        # --- Verify the API wrote the recipe to Supabase ---
        db_errs = verify_recipe_in_supabase(recipe_id, expected_title="", expected_ing_count=-1)
        # Filter out title/count errors — we only care that the row exists with an embedding
        db_errs = [e for e in db_errs if "not found" in e or "embedding" in e]
        if db_errs:
            return r.fail(*[f"DB: {e}" for e in db_errs])

        r.ok(f"job_id={job_id}, recipe_id={recipe_id}, DB write confirmed")
    except Exception as exc:
        r.elapsed = time.perf_counter() - t0
        r.fail(str(exc))
    finally:
        if recipe_id:
            delete_recipe_from_supabase(recipe_id)
            console.print(f"  [dim]🗑  DB cleanup OK: recipes/{recipe_id}[/dim]")
    return r


def test_embed(base: str) -> TestResult:
    r = TestResult(f"POST /embed ({EMBED_QUERY!r})")
    t0 = time.perf_counter()
    try:
        resp = httpx.post(
            f"{base}/embed",
            json={"text": EMBED_QUERY},
            timeout=60,
        )
        r.elapsed = time.perf_counter() - t0
        if resp.status_code != 200:
            return r.fail(f"status {resp.status_code}", resp.text[:300])
        data = resp.json()
        emb = data.get("embedding")
        if not isinstance(emb, list):
            return r.fail("'embedding' not a list")
        if len(emb) != 1536:
            return r.fail(f"embedding length {len(emb)}, expected 1536")
        r.ok(f"1536-dim vector, first val={emb[0]:.6f}")
    except Exception as exc:
        r.elapsed = time.perf_counter() - t0
        r.fail(str(exc))
    return r


def test_error_empty_text(base: str) -> TestResult:
    r = TestResult("POST /ingest empty text → 422")
    t0 = time.perf_counter()
    try:
        resp = httpx.post(f"{base}/ingest", json={"text": ""}, timeout=15)
        r.elapsed = time.perf_counter() - t0
        if resp.status_code in (400, 422):
            r.ok(f"correctly rejected with {resp.status_code}")
        else:
            r.fail(f"expected 400/422, got {resp.status_code}")
    except Exception as exc:
        r.elapsed = time.perf_counter() - t0
        r.fail(str(exc))
    return r


def test_error_empty_url(base: str) -> TestResult:
    r = TestResult("POST /ingest/url empty url → 422")
    t0 = time.perf_counter()
    try:
        resp = httpx.post(f"{base}/ingest/url", json={"url": ""}, timeout=15)
        r.elapsed = time.perf_counter() - t0
        if resp.status_code in (400, 422):
            r.ok(f"correctly rejected with {resp.status_code}")
        else:
            r.fail(f"expected 400/422, got {resp.status_code}")
    except Exception as exc:
        r.elapsed = time.perf_counter() - t0
        r.fail(str(exc))
    return r


# ---------------------------------------------------------------------------
# Runner + reporting
# ---------------------------------------------------------------------------

def run_suite(base: str, text_only: bool, url_only: bool, embed_only: bool) -> list[TestResult]:
    results: list[TestResult] = []

    if not (text_only or url_only or embed_only):
        # Full suite
        console.rule("[bold cyan]RecipeParser Live API Tests[/bold cyan]")
        console.print(f"  Base URL : [yellow]{base}[/yellow]")
        console.print()

        results.append(_run("Health check", test_health, base))
        results.append(_run("Text ingest", test_ingest_text, base))
        results.append(_run("URL ingest", test_ingest_url, base))
        results.append(_run("PDF ingest", test_ingest_pdf, base))
        results.append(_run("EPUB ingest", test_ingest_epub, base))
        results.append(_run("Paprika legacy (Flow A)", test_ingest_paprika_legacy, base))
        results.append(_run("Paprika cayenne (Flow B)", test_ingest_paprika_cayenne, base))
        results.append(_run("Embed", test_embed, base))
        results.append(_run("Error: empty text", test_error_empty_text, base))
        results.append(_run("Error: empty url", test_error_empty_url, base))
    elif text_only:
        results.append(_run("Health check", test_health, base))
        results.append(_run("Text ingest", test_ingest_text, base))
        results.append(_run("Error: empty text", test_error_empty_text, base))
    elif url_only:
        results.append(_run("Health check", test_health, base))
        results.append(_run("URL ingest", test_ingest_url, base))
        results.append(_run("Error: empty url", test_error_empty_url, base))
    elif embed_only:
        results.append(_run("Health check", test_health, base))
        results.append(_run("Embed", test_embed, base))

    return results


def _run(label: str, fn, base: str) -> TestResult:
    console.print(f"  [dim]▶ {label}…[/dim]", end="")
    result = fn(base)
    if result.passed:
        console.print(f"\r  [green]✓[/green] {result.name} [dim]({result.elapsed:.1f}s)[/dim]")
        if result.detail:
            console.print(f"    [dim]{result.detail}[/dim]")
    else:
        console.print(f"\r  [red]✗[/red] {result.name} [dim]({result.elapsed:.1f}s)[/dim]")
        for e in result.errors:
            console.print(f"    [red]  → {e}[/red]")
    return result


def print_summary(results: list[TestResult]) -> None:
    console.print()
    table = Table(title="Test Summary", show_header=True, header_style="bold magenta")
    table.add_column("Test", style="cyan", no_wrap=True)
    table.add_column("Result", justify="center")
    table.add_column("Time", justify="right")
    table.add_column("Detail / Error")

    for r in results:
        status = "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]"
        detail = r.detail if r.passed else "; ".join(r.errors)
        table.add_row(r.name, status, f"{r.elapsed:.1f}s", detail)

    console.print(table)

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    color = "green" if passed == total else "red"
    console.print(f"\n  [{color}]{passed}/{total} tests passed[/{color}]\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Live API test harness for RecipeParser")
    parser.add_argument("--base", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--text-only", action="store_true")
    parser.add_argument("--url-only", action="store_true")
    parser.add_argument("--embed-only", action="store_true")
    args = parser.parse_args()

    results = run_suite(args.base, args.text_only, args.url_only, args.embed_only)
    print_summary(results)

    return 0 if all(r.passed for r in results) else 1


# ---------------------------------------------------------------------------
# New test cases: PDF, EPUB, Paprika Legacy (Flow A), Paprika Cayenne (Flow B)
# ---------------------------------------------------------------------------

def test_ingest_pdf(base: str) -> TestResult:
    r = TestResult("POST /ingest/pdf -> 202 + DB write (generated PDF)")
    t0 = time.perf_counter()
    recipe_id: str | None = None
    import io
    import tempfile
    import os
    try:
        import fitz  # PyMuPDF
    except ImportError:
        r.fail("PyMuPDF (fitz) not installed — run: pip install pymupdf")
        return r
    try:
        # Build a minimal PDF in memory containing a simple recipe
        pdf_text = (
            "Lemon Garlic Roast Chicken\n"
            "Servings: 4\n\n"
            "Ingredients:\n"
            "- 1 whole chicken (about 4 lbs)\n"
            "- 4 cloves garlic, minced\n"
            "- 2 tbsp olive oil\n"
            "- 1 lemon, zested and juiced\n"
            "- 1 tsp dried rosemary\n"
            "- 1 tsp salt\n"
            "- 0.5 tsp black pepper\n\n"
            "Directions:\n"
            "1. Preheat oven to 425F.\n"
            "2. Mix garlic, olive oil, lemon zest, lemon juice, rosemary, salt, and pepper.\n"
            "3. Rub mixture all over the chicken.\n"
            "4. Roast for 60-75 minutes until juices run clear.\n"
            "5. Rest 10 minutes before carving.\n"
        )
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), pdf_text, fontsize=11)
        pdf_bytes = doc.tobytes()
        doc.close()

        resp = httpx.post(
            f"{base}/ingest/pdf",
            files={"file": ("test_recipe.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
            timeout=180,
        )
        r.elapsed = time.perf_counter() - t0
        if resp.status_code != 202:
            return r.fail(f"expected 202, got {resp.status_code}", resp.text[:300])
        data = resp.json()
        errs = _validate_job_response(data)
        if errs:
            return r.fail(*errs)
        recipe_id = data["recipe_id"]
        job_id = data["job_id"]

        db_errs = verify_recipe_in_supabase(recipe_id, expected_title="", expected_ing_count=-1)
        db_errs = [e for e in db_errs if "not found" in e or "embedding" in e]
        if db_errs:
            return r.fail(*[f"DB: {e}" for e in db_errs])

        r.ok(f"job_id={job_id}, recipe_id={recipe_id}, DB write confirmed")
    except Exception as exc:
        r.elapsed = time.perf_counter() - t0
        r.fail(str(exc))
    finally:
        if recipe_id:
            delete_recipe_from_supabase(recipe_id)
            console.print(f"  [dim]Cleanup OK: recipes/{recipe_id}[/dim]")
    return r


def test_ingest_epub(base: str) -> TestResult:
    r = TestResult("POST /ingest/epub -> 202 + DB write (generated EPUB)")
    t0 = time.perf_counter()
    recipe_id: str | None = None
    import io
    import tempfile
    import os
    try:
        from ebooklib import epub as ebooklib_epub
    except ImportError:
        r.fail("ebooklib not installed — run: pip install ebooklib")
        return r
    try:
        # Build a valid EPUB using ebooklib (same library the API uses to read it)
        book = ebooklib_epub.EpubBook()
        book.set_identifier("test-beef-stew-001")
        book.set_title("Classic Beef Stew")
        book.set_language("en")
        book.add_author("Test Kitchen")

        chapter_html = (
            "<html><body>"
            "<h1>Classic Beef Stew</h1>"
            "<p>Servings: 6</p>"
            "<h2>Ingredients</h2><ul>"
            "<li>2 lbs beef chuck, cut into 1-inch cubes</li>"
            "<li>3 medium carrots, sliced</li>"
            "<li>3 medium potatoes, cubed</li>"
            "<li>1 large onion, diced</li>"
            "<li>3 cloves garlic, minced</li>"
            "<li>2 cups beef broth</li>"
            "<li>1 tbsp tomato paste</li>"
            "<li>2 tbsp olive oil</li>"
            "<li>1 tsp salt</li>"
            "<li>0.5 tsp black pepper</li>"
            "</ul>"
            "<h2>Directions</h2><ol>"
            "<li>Heat oil in a large pot over medium-high heat.</li>"
            "<li>Brown beef in batches, about 3 minutes per side.</li>"
            "<li>Add onion and garlic; cook 2 minutes.</li>"
            "<li>Stir in tomato paste, broth, salt, and pepper.</li>"
            "<li>Add carrots and potatoes. Bring to a boil.</li>"
            "<li>Reduce heat, cover, and simmer 90 minutes until beef is tender.</li>"
            "</ol>"
            "</body></html>"
        )
        ch1 = ebooklib_epub.EpubHtml(
            title="Classic Beef Stew",
            file_name="chapter1.xhtml",
            lang="en",
        )
        ch1.set_content(chapter_html)
        book.add_item(ch1)
        book.add_item(ebooklib_epub.EpubNcx())
        book.add_item(ebooklib_epub.EpubNav())
        book.spine = ["nav", ch1]

        # Write to a temp file (ebooklib requires a file path, not BytesIO)
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            ebooklib_epub.write_epub(tmp_path, book)
            with open(tmp_path, "rb") as f:
                epub_bytes = f.read()
        finally:
            os.unlink(tmp_path)

        resp = httpx.post(
            f"{base}/ingest/epub",
            files={"file": ("test_recipe.epub", io.BytesIO(epub_bytes), "application/epub+zip")},
            timeout=180,
        )
        r.elapsed = time.perf_counter() - t0
        if resp.status_code != 202:
            return r.fail(f"expected 202, got {resp.status_code}", resp.text[:300])
        data = resp.json()
        errs = _validate_job_response(data)
        if errs:
            return r.fail(*errs)
        recipe_id = data["recipe_id"]
        job_id = data["job_id"]

        db_errs = verify_recipe_in_supabase(recipe_id, expected_title="", expected_ing_count=-1)
        db_errs = [e for e in db_errs if "not found" in e or "embedding" in e]
        if db_errs:
            return r.fail(*[f"DB: {e}" for e in db_errs])

        r.ok(f"job_id={job_id}, recipe_id={recipe_id}, DB write confirmed")
    except Exception as exc:
        r.elapsed = time.perf_counter() - t0
        r.fail(str(exc))
    finally:
        if recipe_id:
            delete_recipe_from_supabase(recipe_id)
            console.print(f"  [dim]Cleanup OK: recipes/{recipe_id}[/dim]")
    return r


def test_ingest_paprika_legacy(base: str) -> TestResult:
    r = TestResult("POST /ingest (paprika legacy Flow A) -> 202 + DB write")
    t0 = time.perf_counter()
    recipe_id: str | None = None
    import gzip
    import json
    import io
    import zipfile
    import os
    try:
        # Locate the legacy fixture shipped with the Cayenne app
        here = os.path.dirname(__file__)
        fixture_path = os.path.normpath(
            os.path.join(
                here,
                "..",
                "..",
                "..",
                "Cayenne",
                "cayenne-app",
                "src",
                "services",
                "__tests__",
                "fixtures",
                "legacy.paprikarecipes",
            )
        )
        if not os.path.exists(fixture_path):
            r.fail(f"Fixture not found: {fixture_path}")
            return r

        # .paprikarecipes = ZIP of .paprikarecipe files
        # Each .paprikarecipe = gzipped JSON with fields: name, ingredients, directions
        with zipfile.ZipFile(fixture_path, "r") as outer:
            names = [n for n in outer.namelist() if n.endswith(".paprikarecipe")]
            if not names:
                r.fail("No .paprikarecipe entries found in legacy fixture")
                return r
            raw_gz = outer.read(names[0])

        recipe_json = json.loads(gzip.decompress(raw_gz).decode("utf-8"))
        recipe_name = recipe_json.get("name", "Unknown")
        ingredients_raw = recipe_json.get("ingredients", "")
        directions_raw = recipe_json.get("directions", "")
        text = f"{recipe_name}\n\nIngredients:\n{ingredients_raw}\n\nDirections:\n{directions_raw}"

        resp = httpx.post(
            f"{base}/ingest",
            json={"text": text},
            timeout=180,
        )
        r.elapsed = time.perf_counter() - t0
        if resp.status_code != 202:
            return r.fail(f"expected 202, got {resp.status_code}", resp.text[:300])
        data = resp.json()
        errs = _validate_job_response(data)
        if errs:
            return r.fail(*errs)
        recipe_id = data["recipe_id"]
        job_id = data["job_id"]

        db_errs = verify_recipe_in_supabase(recipe_id, expected_title="", expected_ing_count=-1)
        db_errs = [e for e in db_errs if "not found" in e or "embedding" in e]
        if db_errs:
            return r.fail(*[f"DB: {e}" for e in db_errs])

        r.ok(f"job_id={job_id}, recipe_id={recipe_id}, recipe={recipe_name!r}, DB write confirmed")
    except Exception as exc:
        r.elapsed = time.perf_counter() - t0
        r.fail(str(exc))
    finally:
        if recipe_id:
            delete_recipe_from_supabase(recipe_id)
            console.print(f"  [dim]Cleanup OK: recipes/{recipe_id}[/dim]")
    return r


def test_ingest_paprika_cayenne(base: str) -> TestResult:
    r = TestResult("POST /ingest (paprika cayenne Flow B) -> 202 + DB write")
    t0 = time.perf_counter()
    recipe_id: str | None = None
    import gzip
    import json
    import io
    import zipfile
    import os
    try:
        # Locate the Cayenne fixture shipped with the Cayenne app
        here = os.path.dirname(__file__)
        fixture_path = os.path.normpath(
            os.path.join(
                here,
                "..",
                "..",
                "..",
                "Cayenne",
                "cayenne-app",
                "src",
                "services",
                "__tests__",
                "fixtures",
                "cayenne.paprikarecipes",
            )
        )
        if not os.path.exists(fixture_path):
            r.fail(f"Fixture not found: {fixture_path}")
            return r

        # .paprikarecipes = ZIP of .paprikarecipe files
        # Each .paprikarecipe = gzipped JSON; Cayenne recipes include _cayenne_meta
        with zipfile.ZipFile(fixture_path, "r") as outer:
            names = [n for n in outer.namelist() if n.endswith(".paprikarecipe")]
            if not names:
                r.fail("No .paprikarecipe entries found in cayenne fixture")
                return r
            raw_gz = outer.read(names[0])

        recipe_json = json.loads(gzip.decompress(raw_gz).decode("utf-8"))
        recipe_name = recipe_json.get("name", "Unknown")
        ingredients_raw = recipe_json.get("ingredients", "")
        directions_raw = recipe_json.get("directions", "")
        text = f"{recipe_name}\n\nIngredients:\n{ingredients_raw}\n\nDirections:\n{directions_raw}"

        resp = httpx.post(
            f"{base}/ingest",
            json={"text": text},
            timeout=180,
        )
        r.elapsed = time.perf_counter() - t0
        if resp.status_code != 202:
            return r.fail(f"expected 202, got {resp.status_code}", resp.text[:300])
        data = resp.json()
        errs = _validate_job_response(data)
        if errs:
            return r.fail(*errs)
        recipe_id = data["recipe_id"]
        job_id = data["job_id"]

        db_errs = verify_recipe_in_supabase(recipe_id, expected_title="", expected_ing_count=-1)
        db_errs = [e for e in db_errs if "not found" in e or "embedding" in e]
        if db_errs:
            return r.fail(*[f"DB: {e}" for e in db_errs])

        r.ok(f"job_id={job_id}, recipe_id={recipe_id}, recipe={recipe_name!r}, DB write confirmed")
    except Exception as exc:
        r.elapsed = time.perf_counter() - t0
        r.fail(str(exc))
    finally:
        if recipe_id:
            delete_recipe_from_supabase(recipe_id)
            console.print(f"  [dim]Cleanup OK: recipes/{recipe_id}[/dim]")
    return r


if __name__ == "__main__":
    sys.exit(main())
