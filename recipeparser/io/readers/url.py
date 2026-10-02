"""
url.py — Reader for web URLs via the r.jina.ai proxy, with a direct fallback.

Fetches the Markdown-rendered content of a URL using the Jina AI reader
service (``https://r.jina.ai/<url>``), which strips navigation, ads, and
boilerplate and returns clean article text suitable for recipe extraction.

When Jina refuses or fails (an HTTP error, a timeout, a blank page, a
bot-protection challenge), the page is fetched directly with a browser
user-agent and read from its schema.org Recipe JSON-LD, or failing that from
its visible text. Jina answers 451 for whole domains it will not read —
seriouseats.com, 2026-10-02 — and those sites serve the page itself to an
ordinary GET.

This reader is intentionally thin — all AI work happens downstream in the
pipeline stages.

Usage::

    reader = UrlReader()
    chunks = reader.read("https://www.seriouseats.com/some-recipe")
    # → [Chunk(text="...", input_type=InputType.URL, source_url="https://...",
    #          citation=Citation("web", "seriouseats.com", "seriouseats.com", None))]
"""

from __future__ import annotations

import html as html_mod
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests  # type: ignore[import-untyped]
from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from recipeparser.core.citation import web_citation
from recipeparser.core.models import Chunk, InputType
from recipeparser.io.readers import RecipeReader

log = logging.getLogger(__name__)

_JINA_PREFIX = "https://r.jina.ai/"
_REQUEST_TIMEOUT = 30  # seconds
_MAX_REDIRECTS = 5
# Statuses that mean "this site will not serve an automated reader": 402 is
# pay-per-crawl (seriouseats.com answered it to the server, 2026-10-02, while
# serving a browser), 451 is what Jina answers for a domain it will not read.
_REFUSING_STATUSES = frozenset({401, 402, 403, 451})
_PASTE_INSTEAD = "Open it in your browser, copy the recipe and paste its text in place of the link."
_MAX_HTML_CHARS = 3_000_000

PAGE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
_PAGE_HEADERS = {
    "User-Agent": PAGE_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _is_private_address(addr: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast


def is_unsafe_fetch_target(url: str) -> bool:
    """True when ``url`` must not be GET-ed server-side.

    A recipe URL is user-submitted and fetched server-side, so it is exactly
    the shape of an SSRF vector: refuse anything that is not a plain http(s)
    request to a public host, before the GET. No DNS resolution is performed
    here — a hostname that only resolves to a private address is not caught,
    but a bare IP literal (the common probe, e.g. the cloud metadata address)
    and the obvious hostnames are. ``_resolves_to_public`` adds the DNS check
    for the reader's direct fetch, whose whole body becomes recipe text.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return True
    host = (parsed.hostname or "").lower()
    if not host:
        return True
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return _is_private_address(addr)


def _resolves_to_public(host: str) -> bool:
    """True when every address ``host`` resolves to is public; False if it does not resolve.

    Checked before each hop of the direct fetch. requests resolves the name
    again when it connects, so a rebinding DNS server can still win that race;
    this stops the plain case of a public-looking name pointed at 10.x or
    169.254.169.254.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    addrs = {str(info[4][0]) for info in infos}
    try:
        return bool(addrs) and not any(_is_private_address(ipaddress.ip_address(a.split("%")[0])) for a in addrs)
    except ValueError:
        return False


@dataclass(frozen=True)
class PageMeta:
    """What a page says about itself in its <head>: the hero image and the description."""

    image_url: Optional[str]
    description: Optional[str]


_CHALLENGE_TITLE_RE = re.compile(r"^Title:\s*Just a moment", re.IGNORECASE)


def looks_like_bot_challenge(text: str) -> bool:
    """True for a Jina-rendered bot-protection interstitial, not real content.

    Cloudflare-style JS challenges (and clones, e.g. BigScoots' "Security
    Verification") title themselves "Just a moment..." and always print a
    Ray ID for support tickets. A recipe page combining both by coincidence
    is vanishingly unlikely, so together they're a safe fingerprint.
    """
    return bool(_CHALLENGE_TITLE_RE.match(text[:200])) and "Ray ID" in text


_HTML_CHALLENGE_TITLE_RE = re.compile(
    r"<title[^>]*>\s*(?:Just a moment|Attention Required|Access denied|Security Verification)",
    re.IGNORECASE,
)


def looks_like_html_challenge(html: str) -> bool:
    """True for a bot-protection interstitial fetched directly, before Jina's rendering."""
    head = html[:20_000]
    return bool(_HTML_CHALLENGE_TITLE_RE.search(head)) or "/cdn-cgi/challenge-platform/" in head


def _iter_json_ld_nodes(value: Any) -> Iterator[Dict[str, Any]]:
    """Every dict in a JSON-LD document: top-level lists, ``@graph`` and nesting alike."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _iter_json_ld_nodes(child)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_json_ld_nodes(item)


def _is_recipe_node(node: Dict[str, Any]) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and k.split("/")[-1].lower() == "recipe" for k in kinds)


def find_json_ld_recipe(html: str) -> Optional[Dict[str, Any]]:
    """The first schema.org Recipe a page declares in a JSON-LD script, or None.

    A script that is not valid JSON is skipped, not raised: some sites ship one
    broken block beside the good one.
    """
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.IGNORECASE)}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw, strict=False)
        except ValueError:
            continue
        for node in _iter_json_ld_nodes(data):
            if _is_recipe_node(node):
                return node
    return None


def _clean(value: Any) -> str:
    """A JSON-LD string as plain text: entities decoded, stray tags dropped, whitespace collapsed."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        return ""
    text = html_mod.unescape(value)
    if "<" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def _strings(value: Any) -> List[str]:
    """A JSON-LD value that may be one string or a list of them, as a list of clean strings."""
    items = value if isinstance(value, list) else [value]
    return [c for c in (_clean(v.get("name") if isinstance(v, dict) else v) for v in items) if c]


_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<d>\d+(?:\.\d+)?)D)?(?:T(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<m>\d+(?:\.\d+)?)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)


def _duration(value: Any) -> str:
    """An ISO-8601 duration ("PT1H30M") as words ("1 hr 30 min"); anything else as given."""
    text = _clean(value)
    m = _ISO_DURATION_RE.match(text)
    if not text or not m or not any(m.groupdict().values()):
        return text
    total_min = (
        float(m["d"] or 0) * 1440 + float(m["h"] or 0) * 60 + float(m["m"] or 0) + float(m["s"] or 0) / 60
    )
    if total_min <= 0:
        return ""
    hours, minutes = divmod(round(total_min), 60)
    parts = ([f"{hours} hr"] if hours else []) + ([f"{minutes} min"] if minutes else [])
    return " ".join(parts) or "1 min"


def _instruction_lines(value: Any) -> List[str]:
    """recipeInstructions in any of its shapes: a string, strings, HowToSteps, HowToSections."""
    if isinstance(value, str):
        return [line for line in (_clean(p) for p in re.split(r"\n+", html_mod.unescape(value))) if line]
    if isinstance(value, dict):
        if "itemListElement" in value:
            heading = _clean(value.get("name"))
            steps = _instruction_lines(value["itemListElement"])
            return ([f"{heading}:"] if heading else []) + steps
        text = _clean(value.get("text") or value.get("name"))
        return [text] if text else []
    if isinstance(value, list):
        lines: List[str] = []
        for item in value:
            lines.extend(_instruction_lines(item))
        return lines
    return []


def recipe_json_ld_to_text(recipe: Dict[str, Any]) -> str:
    """A schema.org Recipe as plain text laid out like a recipe page, for the extract stage."""
    out: List[str] = []
    name = _clean(recipe.get("name") or recipe.get("headline"))
    if name:
        out += [f"# {name}", ""]
    description = _clean(recipe.get("description"))
    if description:
        out += [description, ""]
    facts = [
        ("Yield", ", ".join(_strings(recipe.get("recipeYield")))),
        ("Prep time", _duration(recipe.get("prepTime"))),
        ("Cook time", _duration(recipe.get("cookTime"))),
        ("Total time", _duration(recipe.get("totalTime"))),
    ]
    fact_lines = [f"{label}: {value}" for label, value in facts if value]
    if fact_lines:
        out += fact_lines + [""]
    ingredients = _strings(recipe.get("recipeIngredient") or recipe.get("ingredients"))
    if ingredients:
        out += ["Ingredients", ""] + [f"- {i}" for i in ingredients] + [""]
    steps = _instruction_lines(recipe.get("recipeInstructions"))
    if steps:
        out += ["Directions", ""]
        n = 0
        for step in steps:
            if step.endswith(":"):
                out.append(step)
            else:
                n += 1
                out.append(f"{n}. {step}")
        out.append("")
    return "\n".join(out).strip()


_NON_CONTENT_TAGS = (
    "script", "style", "noscript", "template", "svg", "iframe", "nav", "header", "footer", "aside", "form",
)


def page_text_from_html(html: str) -> str:
    """The text a reader would get from a page: its Recipe JSON-LD, else its visible article text."""
    recipe = find_json_ld_recipe(html)
    if recipe is not None:
        text = recipe_json_ld_to_text(recipe)
        if text:
            return text
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_NON_CONTENT_TAGS):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    return str(root.get_text("\n", strip=True))


_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_IMAGE_KEYS = ("og:image", "og:image:secure_url", "og:image:url", "twitter:image", "twitter:image:src")
_DESCRIPTION_KEYS = ("description", "og:description", "twitter:description")


def _meta_tags(html: str) -> Dict[str, str]:
    """property/name → content for every <meta> tag, first occurrence winning."""
    found: Dict[str, str] = {}
    for tag in _META_TAG_RE.findall(html):
        attrs = {}
        for m in _ATTR_RE.finditer(tag):
            attrs[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = html_mod.unescape(attrs.get("content") or "").strip()
        if key and content and key not in found:
            found[key] = content
    return found


def _is_absolute_http(url: str) -> bool:
    """
    True for an http(s) address that parses and names a host. A prefix test let "http://[bad/…"
    (which ``urlparse`` refuses) and "https://" through (Fix Roadmap F-110).
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname)


def page_meta_from_html(html: str) -> PageMeta:
    """
    The hero image and description a page declares in its own <meta> tags.

    The scraper's markdown carries neither reliably — on 2026-09-12 an NYT page
    came back with no og:image line and, as its only image, the Edamam
    "Powered by" logo, while the page's own head named the real photograph.
    An image that is not an absolute http(s) URL naming a host is treated as none.
    """
    tags = _meta_tags(html)
    image = next((tags[k] for k in _IMAGE_KEYS if k in tags), None)
    if image is not None and not _is_absolute_http(image):
        image = None
    description = next((tags[k] for k in _DESCRIPTION_KEYS if k in tags), None)
    return PageMeta(image_url=image, description=description)


_BADGE_WORDS = frozenset(
    {"logo", "badge", "icon", "sprite", "powered", "pixel", "spacer", "placeholder"}
)


def looks_like_badge(url: str, alt: str = "") -> bool:
    """
    True for an image that is a site's furniture rather than a photograph.

    Judged from the URL's path (with a Next.js ``/_next/image?url=…`` wrapper
    unwrapped) and the alt text: a whole badge-word token in either, an SVG,
    or anything the wrapper serves from ``/assets/``. Badge words are matched
    as whole tokens after splitting on non-alphanumerics, so "iconic-lasagna"
    and "silicone-mold-cookies" are not mistaken for site furniture the way a
    plain substring match would. ``parse_qs`` already percent-decodes the
    wrapper's ``url=`` value; unquoting the whole URL before parsing it (as
    opposed to just the path) would let an inner URL's own encoded
    ``?``/``&``/``=`` characters split into the wrong query parameters.

    An address that does not parse ("http://[bad/…", which ``urlparse``
    refuses with ValueError) is not a photograph either: True, never a raise.
    Both callers in the URL ingest run outside any guard, so a malformed
    og:image or markdown image failed the whole job (Fix Roadmap F-013).
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return True
    inner = parse_qs(parsed.query).get("url", [""])[0].lower()
    path = unquote(parsed.path).lower()
    if path.endswith(".svg") or inner.endswith(".svg"):
        return True
    if inner.startswith("/assets/") or "/assets/" in inner:
        return True
    haystack = " ".join((path, inner, alt.lower()))
    tokens = set(re.split(r"[^a-z0-9]+", haystack))
    return bool(tokens & _BADGE_WORDS)


class UrlReader(RecipeReader):
    """
    Fetches a URL via the r.jina.ai proxy and returns a single Chunk.

    The Jina reader converts the target page to clean Markdown, removing
    navigation, ads, and other non-content elements. When Jina fails, the page
    is fetched directly (``_read_directly``). The resulting text is returned as
    a single Chunk with ``InputType.URL``.

    Args:
        timeout: HTTP request timeout in seconds (default: 30).
    """

    def __init__(self, timeout: int = _REQUEST_TIMEOUT) -> None:
        self.timeout = timeout

    def read(self, source: str) -> List[Chunk]:
        """
        Fetch ``source`` via r.jina.ai, or directly when Jina fails, and return a single-element list.

        Args:
            source: The target URL to fetch (e.g. ``https://example.com/recipe``).

        Returns:
            A list containing exactly one Chunk with:
            - ``text``: The Markdown content returned by r.jina.ai, or the
              page's own Recipe JSON-LD (else visible text) from the direct fetch.
            - ``input_type``: ``InputType.URL``
            - ``source_url``: The original (non-proxied) URL.
            - ``citation``: a ``web_citation`` of the URL (host-derived key and title)

        Raises:
            UrlFetchError: Jina failed — a non-2xx status, a timeout, any other
                network failure, a page with no text, or a bot-protection
                challenge page (Jina itself got challenged and rendered that
                instead of the article) — and the direct fetch failed too. When
                the site itself refused the direct fetch (``SiteRefusedError``),
                the message says so and suggests pasting the recipe's text, since
                Jina's own status (a 451, say) names Jina's policy, not the
                site's. Any other direct failure is logged and the message is
                Jina's failure, as before the fallback existed. The message is a
                predicate about the URL: the caller prefixes the URL itself.
        """
        from recipeparser.exceptions import SiteRefusedError, UrlFetchError

        try:
            text = self._read_via_jina(source)
        except UrlFetchError as jina_error:
            # Best effort: whatever goes wrong in the fallback — a refusal, or a bug in
            # parsing some site's HTML — the job reports a sentence, never a traceback's.
            try:
                text = self._read_directly(source)
            except SiteRefusedError as refusal:
                log.warning(
                    "UrlReader: %s refused both Jina (%s) and the direct fetch (%s)", source, jina_error, refusal
                )
                raise refusal from jina_error
            except Exception as direct_error:
                log.warning("UrlReader: direct fetch of %s failed too: %r", source, direct_error)
                raise jina_error from jina_error.__cause__
            log.warning(
                "UrlReader: Jina failed for %s (%s) — read %d chars from the page directly.",
                source, jina_error, len(text),
            )

        return [
            Chunk(
                text=text,
                input_type=InputType.URL,
                source_url=source,
                citation=web_citation(source),
            )
        ]

    def _read_via_jina(self, source: str) -> str:
        """The page as Jina renders it, or a UrlFetchError saying why not."""
        from recipeparser.exceptions import UrlFetchError

        jina_url = f"{_JINA_PREFIX}{source}"
        log.info("UrlReader: fetching %s via %s", source, jina_url)

        try:
            response = requests.get(jina_url, timeout=self.timeout)
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", "?")
            # Jina's body names its reason (a 451 for a domain it will not read); the status alone does not.
            reason = getattr(exc.response, "text", "")
            if not isinstance(reason, str):
                reason = ""
            log.warning("UrlReader: Jina answered HTTP %s for %s: %.500s", status, source, reason)
            raise UrlFetchError(f"could not be fetched (HTTP {status}).") from exc
        except requests.Timeout as exc:
            raise UrlFetchError("did not respond in time.") from exc
        except requests.RequestException as exc:
            raise UrlFetchError(f"could not be fetched: {exc}") from exc

        text = response.text
        if not text.strip():
            raise UrlFetchError("contains no readable text.")
        if looks_like_bot_challenge(text):
            raise UrlFetchError("is blocked by the site's bot-protection challenge page.")
        log.info("UrlReader: received %d chars for %s", len(text), source)
        return text

    def _read_directly(self, source: str) -> str:
        """The page fetched with a browser user-agent, read from its Recipe JSON-LD or visible text.

        Redirects are followed by hand, at most ``_MAX_REDIRECTS``, so every hop
        passes the same private-address checks as the first.
        """
        from recipeparser.exceptions import SiteRefusedError, UrlFetchError

        url = source
        for _ in range(_MAX_REDIRECTS + 1):
            try:
                unsafe = is_unsafe_fetch_target(url)
            except ValueError:
                unsafe = True
            if unsafe or not _resolves_to_public(urlparse(url).hostname or ""):
                raise UrlFetchError("is not a public web address.")
            try:
                response = requests.get(url, headers=_PAGE_HEADERS, timeout=self.timeout, allow_redirects=False)
            except requests.Timeout as exc:
                raise UrlFetchError("did not respond in time.") from exc
            except requests.RequestException as exc:
                raise UrlFetchError(f"could not be fetched: {exc}") from exc
            location = response.headers.get("location") if response.status_code in (301, 302, 303, 307, 308) else None
            if not location:
                break
            url = urljoin(url, location)
        else:
            raise UrlFetchError("redirected too many times.")

        if response.status_code in _REFUSING_STATUSES:
            raise SiteRefusedError(f"refuses automated readers (HTTP {response.status_code}). {_PASTE_INSTEAD}")
        if not 200 <= response.status_code < 300:
            raise UrlFetchError(f"could not be fetched (HTTP {response.status_code}).")
        content_type = response.headers.get("content-type") or ""
        if not isinstance(content_type, str) or "html" not in content_type.lower():
            raise UrlFetchError("is not a web page.")
        html = response.text[:_MAX_HTML_CHARS]
        if looks_like_html_challenge(html):
            raise SiteRefusedError(f"is blocked by the site's bot-protection challenge page. {_PASTE_INSTEAD}")
        text = page_text_from_html(html)
        if not text.strip():
            raise UrlFetchError("contains no readable text.")
        return text
