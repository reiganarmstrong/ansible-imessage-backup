from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "select_latest_remote_verified_archive.py"
)
SPEC = importlib.util.spec_from_file_location(
    "select_latest_remote_verified_archive",
    SCRIPT,
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SelectLatestRemoteVerifiedArchiveTests(unittest.TestCase):
    def write_marker(
        self,
        root: Path,
        archive_name: str,
        remote_root: str,
        passed: bool = True,
    ) -> None:
        marker_root = (
            root
            / ".imessage-archive-state"
            / "remote-verifications"
        )
        marker_root.mkdir(parents=True, exist_ok=True)
        (marker_root / f"{archive_name}.json").write_text(
            json.dumps(
                {
                    "passed": passed,
                    "archive_name": archive_name,
                    "remote_destination": f"{remote_root}/{archive_name}",
                }
            ),
            encoding="utf-8",
        )

    def test_selects_newest_marker_for_exact_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.write_marker(
                root,
                "imessage-backup-20240101T000000",
                "nextcloud:Backups/iMessage",
            )
            self.write_marker(
                root,
                "imessage-backup-20240201T000000",
                "other:Backups/iMessage",
            )
            self.write_marker(
                root,
                "imessage-backup-20240301T000000",
                "nextcloud:Backups/iMessage",
            )

            selected = MODULE.select_latest(
                root,
                "imessage-backup",
                ".imessage-archive-state",
                "nextcloud:Backups/iMessage/",
            )

            self.assertEqual(
                selected,
                "imessage-backup-20240301T000000",
            )

    def test_returns_none_without_matching_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.write_marker(
                root,
                "imessage-backup-20240101T000000",
                "other:Backups/iMessage",
            )
            self.assertIsNone(
                MODULE.select_latest(
                    root,
                    "imessage-backup",
                    ".imessage-archive-state",
                    "nextcloud:Backups/iMessage",
                )
            )
