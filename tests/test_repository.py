"""Prevent common private artifacts from entering the public Git index."""

import shutil
import subprocess
import unittest
from pathlib import Path, PurePosixPath


def is_private_path(value: str) -> bool:
    """Classify filenames without reading or reporting financial contents."""
    path = PurePosixPath(value.lower())
    name = path.name
    database_suffixes = (".db", ".sqlite", ".sqlite3")
    sidecars = tuple(
        suffix + ending
        for suffix in database_suffixes
        for ending in ("-wal", "-shm", "-journal")
    )
    private_directories = {".local", ".venv", "venv", "env", "__pycache__"}
    return (
        bool(private_directories.intersection(path.parts))
        or name.endswith((".pdf", ".csv", ".xlsx", ".xls", ".json", ".xml"))
        or name.endswith(database_suffixes + sidecars)
        or name == "secrets.toml"
        or (name.startswith(".env") and value != ".env.example")
    )


class RepositoryPrivacyTests(unittest.TestCase):
    """Use only Git metadata and synthetic names to guard public commits."""

    def test_private_filename_detection(self) -> None:
        """Cover financial formats, SQLite sidecars, and credentials."""
        for value in (
            "statement.PDF", "flex.xml", "holdings.csv", "accounts.json",
            "book.xlsx", "book.xls", "ledger.db", "ledger.sqlite3-wal",
            "ledger.sqlite-shm", "ledger.db-journal", ".env.production",
            ".streamlit/secrets.toml", ".local/export.txt", ".venv/state.txt",
        ):
            self.assertTrue(is_private_path(value))
        for value in (".env.example", "requirements.txt", "core/database.py"):
            self.assertFalse(is_private_path(value))

    def test_no_tracked_private_artifacts(self) -> None:
        """Reject sensitive filename classes without echoing their paths to CI."""
        root = Path(__file__).resolve().parents[1]
        if shutil.which("git") is None or not (root / ".git").exists():
            self.skipTest("Git checkout required for the privacy gate.")
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, check=False,
            capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, "Cannot inspect the Git index.")
        paths = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        count = sum(is_private_path(path) for path in paths if path)
        self.assertEqual(count, 0, "Private artifact filename(s) found in the Git index.")


if __name__ == "__main__":
    unittest.main()