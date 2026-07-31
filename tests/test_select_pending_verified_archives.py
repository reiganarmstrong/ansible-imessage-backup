from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "select_pending_verified_archives.py"
)
SPEC = importlib.util.spec_from_file_location(
    "select_pending_verified_archives",
    SCRIPT,
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SelectPendingVerifiedArchivesTests(unittest.TestCase):
    def create_archive(
        self,
        root: Path,
        name: str,
        passed: bool = True,
    ) -> Path:
        archive = root / name
        archive.mkdir()
        (archive / "verification.json").write_text(
            json.dumps({"passed": passed}),
            encoding="utf-8",
        )
        return archive

    def write_marker(
        self,
        root: Path,
        archive_name: str,
        remote_destination: str,
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
                    "passed": True,
                    "archive_name": archive_name,
                    "remote_destination": remote_destination,
                }
            ),
            encoding="utf-8",
        )

    def test_selects_missing_and_wrong_remote_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = self.create_archive(
                root,
                "imessage-backup-20240101T000000",
            )
            second = self.create_archive(
                root,
                "imessage-backup-20240201T000000",
            )
            third = self.create_archive(
                root,
                "imessage-backup-20240301T000000",
            )
            self.create_archive(
                root,
                "imessage-backup-20240401T000000",
                passed=False,
            )
            self.write_marker(
                root,
                first.name,
                f"nextcloud:Backups/iMessage/{first.name}",
            )
            self.write_marker(
                root,
                second.name,
                f"other:Backups/iMessage/{second.name}",
            )

            selected = MODULE.pending_archives(
                root,
                "imessage-backup",
                ".imessage-archive-state",
                "nextcloud:Backups/iMessage/",
            )

            self.assertEqual(selected, [second.resolve(), third.resolve()])

    def test_invalid_marker_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive = self.create_archive(
                root,
                "imessage-backup-20240101T000000",
            )
            self.write_marker(
                root,
                archive.name,
                f"nextcloud:Backups/iMessage/{archive.name}",
            )
            marker = (
                root
                / ".imessage-archive-state"
                / "remote-verifications"
                / f"{archive.name}.json"
            )
            marker.write_text("{invalid", encoding="utf-8")

            selected = MODULE.pending_archives(
                root,
                "imessage-backup",
                ".imessage-archive-state",
                "nextcloud:Backups/iMessage",
            )

            self.assertEqual(selected, [archive.resolve()])
