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


class EpubExtractionError(RecipeParserError):
    """Raised when the EPUB file cannot be opened or parsed."""


class PdfExtractionError(RecipeParserError):
    """Raised when the PDF cannot be opened, parsed, or fails pre-flight (e.g. no text layer, password-protected)."""


class ImageExtractionError(RecipeParserError):
    """Raised when a photo cannot be opened as an image."""


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
