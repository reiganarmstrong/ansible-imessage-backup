from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).parents[1]
SCRIPT = REPOSITORY / "scripts" / "validate_catalog_cache.py"
FIXTURE = REPOSITORY / "tests" / "fixtures" / "verified-archive"
SPEC = importlib.util.spec_from_file_location("validate_catalog_cache", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ValidateCatalogCacheTests(unittest.TestCase):
    def create_cache(self, root: Path) -> Path:
        cache = root / "cache"
        preservation = cache / "Source Preservation"
        preservation.mkdir(parents=True)
        shutil.copy2(
            FIXTURE / "verification.json",
            cache / "verification.json",
        )
        shutil.copy2(
            FIXTURE / "conversation.html",
            cache / "conversation.html",
        )
        (preservation / "attachment-manifest.json").write_text(
            "[]\n",
            encoding="utf-8",
        )
        connection = sqlite3.connect(preservation / "chat.db")
        connection.execute(
            "CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT)"
        )
        connection.execute("INSERT INTO message VALUES (1, 'GUID')")
        connection.commit()
        connection.close()
        return cache

    def test_valid_compact_archive_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache = self.create_cache(Path(temporary_directory))

            report = MODULE.validate(cache)

            self.assertTrue(report["passed"])
            self.assertEqual(report["database_quick_check"], "ok")
            self.assertEqual(report["html_file_count"], 1)

    def test_missing_html_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache = self.create_cache(Path(temporary_directory))
            for html_path in cache.glob("*.html"):
                html_path.unlink()

            with self.assertRaises(ValueError):
                MODULE.validate(cache)
