from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "select_latest_verified_archive.py"
)
SPEC = importlib.util.spec_from_file_location(
    "select_latest_verified_archive",
    SCRIPT,
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def create_archive(root: Path, name: str, passed: bool | None) -> Path:
    archive = root / name
    archive.mkdir()
    if passed is not None:
        (archive / "verification.json").write_text(
            json.dumps({"passed": passed}),
            encoding="utf-8",
        )
    return archive


class SelectLatestVerifiedArchiveTests(unittest.TestCase):
    def test_selects_newest_passing_timestamped_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            expected = create_archive(
                root,
                "imessage-backup-20240108T000000",
                True,
            )
            create_archive(
                root,
                "imessage-backup-20240115T000000",
                False,
            )
            create_archive(
                root,
                "imessage-backup-20240122T000000",
                None,
            )
            create_archive(
                root,
                "imessage-backup-20240101T000000",
                True,
            )

            selected = MODULE.select_latest(root, "imessage-backup")

            self.assertEqual(selected, expected)

    def test_returns_none_without_a_verified_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_archive(
                root,
                "imessage-backup-20240101T000000",
                False,
            )

            self.assertIsNone(MODULE.select_latest(root, "imessage-backup"))


if __name__ == "__main__":
    unittest.main()
