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


class ExportError(RecipeParserError):
    """Raised when the Paprika export bundle cannot be written."""
