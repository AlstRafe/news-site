"""Create a clean portable application archive using Python's standard library."""
from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path

APP_NAME = "NewsHtmlReader"
PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIRECTORY = PROJECT_ROOT.parent
EXCLUDED_DIRECTORIES = {".git", ".venv", "__pycache__", ".pytest_cache", "instance", "tests", "preset",
                        "node_modules", ".vscode", ".ruff_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".sqlite", ".sqlite3", ".db", ".log", ".zip", ".key", ".pem", ".pfx", ".p12"}
#: The formatter's and linter's settings, for working on the code only.
EXCLUDED_FILES = {".editorconfig", ".prettierrc.json", ".prettierignore", "eslint.config.mjs", "package.json",
                  "package-lock.json", "ruff.toml", ".git-blame-ignore-revs"}


def should_include(file_path: Path, relative_path: Path) -> bool:
    if file_path.is_symlink():
        return False
    if EXCLUDED_DIRECTORIES.intersection(relative_path.parts):
        return False
    if relative_path.parts and relative_path.parts[0] == "articles":
        return relative_path.as_posix() == "articles/README.txt"
    if relative_path.as_posix() == "start_config.bat" or relative_path.as_posix() in EXCLUDED_FILES:
        return False
    if any(part.lower() == ".env" or part.lower().startswith(".env.") for part in relative_path.parts):
        return False
    if file_path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    return True


def create_archive() -> Path:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    archive_path = OUTPUT_DIRECTORY / f"{APP_NAME}-portable-{timestamp}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file_path in PROJECT_ROOT.rglob("*"):
            if not file_path.is_file():
                continue
            relative_path = file_path.relative_to(PROJECT_ROOT)
            if should_include(file_path, relative_path):
                archive.write(file_path, (Path(APP_NAME) / relative_path).as_posix())
    return archive_path


if __name__ == "__main__":
    print(f"Portable ZIP created: {create_archive()}")
