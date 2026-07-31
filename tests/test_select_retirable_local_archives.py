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
    / "select_retirable_local_archives.py"
)
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location(
    "select_retirable_local_archives",
    SCRIPT,
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SelectRetirableLocalArchivesTests(unittest.TestCase):
    def create_ready_archive(
        self,
        root: Path,
        name: str,
        *,
        with_restore: bool,
    ) -> None:
        state = root / ".state"
        archive = root / name
        archive.mkdir()
        (archive / "verification.json").write_text(
            '{"passed": true}\n',
            encoding="utf-8",
        )
        remote_marker = {
            "passed": True,
            "archive_name": name,
            "remote_destination": f"nextcloud:root/{name}",
            "verified_at": "2024-01-02T00:00:00Z",
            "file_count": 10,
            "total_bytes": 100,
        }
        remote_root = state / "remote-verifications"
        remote_root.mkdir(parents=True, exist_ok=True)
        (remote_root / f"{name}.json").write_text(
            json.dumps(remote_marker),
            encoding="utf-8",
        )
        cache = state / "catalog-cache" / name
        cache.mkdir(parents=True)
        (cache / ".catalog-cache-verification.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "archive_name": name,
                    "remote_destination": remote_marker[
                        "remote_destination"
                    ],
                    "remote_verified_at": remote_marker["verified_at"],
                    "remote_file_count": remote_marker["file_count"],
                    "remote_total_bytes": remote_marker["total_bytes"],
                }
            ),
            encoding="utf-8",
        )
        if with_restore:
            restore_root = state / "restore-verifications"
            restore_root.mkdir(parents=True, exist_ok=True)
            (restore_root / "restore.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "remote_source": f"nextcloud:root/{name}",
                    }
                ),
                encoding="utf-8",
            )

    def test_requires_successful_restore_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            name = "imessage-backup-20240101T000000"
            self.create_ready_archive(root, name, with_restore=False)

            report = MODULE.select_retirable(
                root,
                "imessage-backup",
                ".state",
                "catalog-cache",
                "nextcloud:root",
                0,
                True,
            )

            self.assertEqual(report["count"], 0)
            self.assertEqual(
                report["blocked_reasons"],
                {"successful_restore_test_required": 1},
            )

    def test_selects_verified_cached_archive_after_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            name = "imessage-backup-20240101T000000"
            self.create_ready_archive(root, name, with_restore=True)

            report = MODULE.select_retirable(
                root,
                "imessage-backup",
                ".state",
                "catalog-cache",
                "nextcloud:root",
                0,
                True,
            )

            self.assertEqual(report["count"], 1)
            self.assertEqual(
                report["archive_paths"],
                [str((root / name).resolve())],
            )
            self.assertTrue(report["restore_attestation_present"])

    def test_keep_count_preserves_newest_ready_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            older = "imessage-backup-20240101T000000"
            newer = "imessage-backup-20240201T000000"
            self.create_ready_archive(root, older, with_restore=True)
            self.create_ready_archive(root, newer, with_restore=False)

            report = MODULE.select_retirable(
                root,
                "imessage-backup",
                ".state",
                "catalog-cache",
                "nextcloud:root",
                1,
                True,
            )

            self.assertEqual(
                report["archive_paths"],
                [str((root / older).resolve())],
            )
            self.assertEqual(report["kept_local_archive_count"], 1)

    def test_refuses_archive_containing_configured_recovery_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            name = "imessage-backup-20240101T000000"
            self.create_ready_archive(root, name, with_restore=True)
            recovery_input = root / name / "Recovered Attachments"
            recovery_input.mkdir()

            report = MODULE.select_retirable(
                root,
                "imessage-backup",
                ".state",
                "catalog-cache",
                "nextcloud:root",
                0,
                True,
                [recovery_input],
            )

            self.assertEqual(report["count"], 0)
            self.assertEqual(
                report["blocked_reasons"],
                {"contains_configured_recovery_input": 1},
            )

    def test_refuses_symlink_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outside = root / "outside"
            outside.mkdir()
            symlink = root / "imessage-backup-20240101T000000"
            symlink.symlink_to(outside, target_is_directory=True)

            report = MODULE.select_retirable(
                root,
                "imessage-backup",
                ".state",
                "catalog-cache",
                "nextcloud:root",
                0,
                False,
            )

            self.assertEqual(report["count"], 0)
            self.assertEqual(
                report["blocked_reasons"],
                {"symlink_archive_refused": 1},
            )
