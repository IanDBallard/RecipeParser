"""
User-writable paths for RecipeParser.

All persisted user data (categories, .env, default output) lives under
platform-appropriate user directories to avoid permission issues when the
app is installed in system-protected locations (e.g. Program Files).
"""
import os
import sys
from pathlib import Path

_APP_NAME = "RecipeParser"


def get_app_data_dir() -> Path:
    """User-writable config directory.

    - Windows: %APPDATA%\\RecipeParser
    - macOS:   ~/Library/Application Support/RecipeParser
    - Linux:   $XDG_CONFIG_HOME/RecipeParser or ~/.config/RecipeParser
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    path = Path(base) / _APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_env_file() -> Path:
    """Path to the .env file (API key, etc.).

    Lives in the app data directory so it survives upgrades and is writable
    when the app is installed to Program Files.
    """
    return get_app_data_dir() / ".env"


def get_categories_file() -> Path:
    """Path to the user-editable categories.yaml.

    Always in app data. On first run, the bundled default is copied there
    if the user file does not exist.
    """
    return get_app_data_dir() / "categories.yaml"


def get_bundled_categories_file() -> Path:
    """Path to the bundled categories.yaml (read-only, may not be writable)."""
    return Path(__file__).parent / "categories.yaml"


def get_default_output_dir() -> Path:
    """Default output directory for recipe exports.

    Uses Documents/RecipeParser when available so users can easily find
    exports. Falls back to app data/Exports if Documents is not writable.
    """
    docs = Path.home() / "Documents"
    if docs.exists():
        out = docs / _APP_NAME
        try:
            out.mkdir(parents=True, exist_ok=True)
            return out
        except OSError:
            pass
    return get_app_data_dir() / "Exports"
