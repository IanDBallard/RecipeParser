"""
Custom exceptions for the recipeparser package.

Library consumers can catch these programmatically; the CLI catches them and
prints a friendly message without a traceback.
"""


class RecipeParserError(Exception):
    """Base class for all recipeparser errors."""


class ConfigurationError(RecipeParserError):
    """Raised when required configuration (e.g. API key) is missing or invalid."""


class GeminiConnectionError(RecipeParserError):
    """Raised when the Gemini API is unreachable or returns an auth error."""


class UnreadableInputError(RecipeParserError):
    """A reader could not turn its input into text, before any model call.

    The message is a predicate about the input — "is password-protected.",
    "contains no readable text." — and never names the server's temp path, so
    the API can prefix the user's own filename or URL and show the result as
    the job's error message.
    """


class EpubExtractionError(UnreadableInputError):
    """Raised when the EPUB file cannot be opened or parsed."""


class PdfExtractionError(UnreadableInputError):
    """Raised when the PDF cannot be opened, parsed, or fails pre-flight (e.g. no text layer, password-protected)."""


class ImageExtractionError(UnreadableInputError):
    """Raised when a photo cannot be opened as an image."""


class PaprikaExtractionError(UnreadableInputError):
    """Raised when a .paprikarecipes archive cannot be opened or holds no recipe."""


class UrlFetchError(UnreadableInputError):
    """Raised when a page cannot be fetched, fetched but holds no text, or
    fetched but is a bot-protection challenge page rather than the article.
    """


class SiteRefusedError(UrlFetchError):
    """A page whose site refuses automated readers: a 401, 402, 403 or 451, or a
    bot-protection challenge page, answered to the reader's direct fetch or
    rendered by Jina in place of the article.

    No retry from the server will get past it, so the message tells the person
    what does: their own browser can read the page, and the import box takes
    the recipe's text.

    ``host`` is the site that refused, lower-cased without a leading www., so
    the job row carries a typed reason (``site_blocked``) that the client names
    the site from, rather than parsing this sentence (Cayenne Fix Roadmap F-480).
    """

    def __init__(self, message: str, host: str = "") -> None:
        super().__init__(message)
        self.host = host


class ExportError(RecipeParserError):
    """Raised when the Paprika export bundle cannot be written."""


class RateLimitPauseError(RecipeParserError):
    """Raised when consecutive 429 responses exceed RATE_LIMIT_PAUSE_THRESHOLD.

    The pipeline controller catches this and transitions to PAUSED state,
    scheduling an auto-resume after RATE_LIMIT_AUTO_RESUME_SECS.
    """


class CheckpointError(RecipeParserError):
    """Raised when a checkpoint file cannot be read or written."""


class PipelineTransitionError(RecipeParserError):
    """Raised when an invalid FSM transition is attempted on PipelineController."""


class RecategorizationError(RecipeParserError):
    """Raised when the recategorize operation fails (bad file, unreadable archive, etc.)."""


class ExtractionParseError(RecipeParserError):
    """Raised when Gemini's extraction reply could not be parsed on any attempt.

    Distinct from a chunk that genuinely contains no recipe: that is an empty
    result, not an error. This means the reply itself was unusable — usually
    truncated JSON — and the chunk's recipes are lost unless something upstream
    counts it. The message carries the reply's finish reason and first line,
    which is what separates truncation from malformed content.
    """


class JobCancelledError(RecipeParserError):
    """Raised at a job checkpoint once its owner has cancelled it (Cayenne F-426).

    Not a failure: the job ends with status 'cancelled' and keeps the recipes it
    already wrote. Raised from ``recipeparser.core.job_signal`` between stages,
    between chunks and between Gemini retries, so every ``except Exception`` on
    the way out must let it through rather than recording it as a skip or an
    unreadable file.
    """


class AIServiceTimeoutError(RuntimeError):
    """The AI service kept timing out (504 DEADLINE_EXCEEDED, or our own per-call
    timeout) until the retry budget ran out.

    The message is the sentence the cook sees on the job row. A RuntimeError so
    the readers that already let ``extract_text_via_vision``'s RuntimeError
    through pass this one through unchanged too.
    """
