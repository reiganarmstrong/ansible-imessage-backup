from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "select_superseded_remote_archives.py"
)
REPOSITORY = Path(__file__).parents[1]
ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")
FAKE_RCLONE = REPOSITORY / "tests" / "fixtures" / "fake_rclone.py"
SPEC = importlib.util.spec_from_file_location(
    "select_superseded_remote_archives", SCRIPT
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SelectSupersededRemoteArchivesTests(unittest.TestCase):
    def create_archive(
        self,
        root: Path,
        name: str,
        messages: list[str],
        attachments: list[tuple[str, str]],
        remote_root: str = "nextcloud:root",
    ) -> None:
        state = root / ".state"
        marker_root = state / "remote-verifications"
        cache = state / "catalog-cache" / name
        preservation = cache / "Source Preservation"
        marker_root.mkdir(parents=True, exist_ok=True)
        preservation.mkdir(parents=True, exist_ok=True)
        remote_marker = {
            "passed": True,
            "archive_name": name,
            "remote_destination": f"{remote_root}/{name}",
            "verified_at": f"{name[-15:-7]}T{name[-6:]}Z",
            "file_count": 10,
            "total_bytes": 100,
        }
        (marker_root / f"{name}.json").write_text(
            json.dumps(remote_marker), encoding="utf-8"
        )
        (cache / ".catalog-cache-verification.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "archive_name": name,
                    "remote_destination": remote_marker["remote_destination"],
                    "remote_verified_at": remote_marker["verified_at"],
                    "remote_file_count": 10,
                    "remote_total_bytes": 100,
                }
            ),
            encoding="utf-8",
        )
        with sqlite3.connect(preservation / "chat.db") as database:
            database.execute(
                "CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT)"
            )
            database.executemany(
                "INSERT INTO message(guid) VALUES (?)",
                [(message,) for message in messages],
            )
        (preservation / "attachment-manifest.json").write_text(
            json.dumps(
                [
                    {"attachment_guid": guid, "sha256": digest}
                    for guid, digest in attachments
                ]
            ),
            encoding="utf-8",
        )

    def select(self, root: Path) -> dict[str, object]:
        return MODULE.select_superseded(
            root,
            "imessage-backup",
            ".state",
            "catalog-cache",
            "nextcloud:root",
        )

    def test_selects_strictly_subsumed_older_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            older = "imessage-backup-20240101T000000"
            newer = "imessage-backup-20240201T000000"
            self.create_archive(root, older, ["A"], [("ATTACH-A", "HASH-A")])
            self.create_archive(
                root,
                newer,
                ["A", "B"],
                [("ATTACH-A", "HASH-A"), ("ATTACH-B", "HASH-B")],
            )

            report = self.select(root)

            self.assertEqual(report["archive_names"], [older])
            self.assertEqual(report["candidate_count"], 1)

    def test_retains_archive_with_unique_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            older = "imessage-backup-20240101T000000"
            newer = "imessage-backup-20240201T000000"
            self.create_archive(root, older, ["OLD-ONLY"], [])
            self.create_archive(root, newer, ["NEW"], [])

            report = self.select(root)

            self.assertEqual(report["archive_names"], [])
            self.assertEqual(
                report["blocked_archives"][0]["reasons"],
                ["contains_unique_messages"],
            )

    def test_retains_archive_with_unique_preserved_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            older = "imessage-backup-20240101T000000"
            newer = "imessage-backup-20240201T000000"
            self.create_archive(root, older, ["A"], [("ATTACH", "OLD-HASH")])
            self.create_archive(root, newer, ["A"], [])

            report = self.select(root)

            self.assertEqual(report["archive_names"], [])
            self.assertEqual(
                report["blocked_archives"][0]["reasons"],
                ["contains_unique_preserved_attachments"],
            )

    def test_one_archive_is_never_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            name = "imessage-backup-20240101T000000"
            self.create_archive(root, name, ["A"], [])

            report = self.select(root)

            self.assertEqual(report["archive_names"], [])
            self.assertEqual(report["candidate_count"], 0)
            self.assertEqual(
                report["blocked_archives"][0]["reasons"],
                ["newest_archive_retained"],
            )

    @unittest.skipUnless(ANSIBLE_PLAYBOOK, "ansible-playbook is not installed")
    def test_playbook_deletes_only_selected_remote_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            older = "imessage-backup-20240101T000000"
            newer = "imessage-backup-20240201T000000"
            fake_remote = f"fake:{remote}"
            self.create_archive(
                local,
                older,
                ["A"],
                [("ATTACH", "HASH")],
                fake_remote,
            )
            self.create_archive(
                local,
                newer,
                ["A", "B"],
                [("ATTACH", "HASH")],
                fake_remote,
            )
            remote_markers = remote / ".state" / "remote-verifications"
            remote_markers.mkdir(parents=True)
            for name in (older, newer):
                archive = remote / name
                archive.mkdir()
                (archive / "payload").write_text(name, encoding="utf-8")
                shutil.copy2(
                    local / ".state" / "remote-verifications" / f"{name}.json",
                    remote_markers / f"{name}.json",
                )

            assert ANSIBLE_PLAYBOOK
            result = subprocess.run(
                [
                    ANSIBLE_PLAYBOOK,
                    str(REPOSITORY / "retire-superseded-remote-archives.yml"),
                    "--inventory",
                    str(REPOSITORY / "inventory.ini"),
                    "--extra-vars",
                    json.dumps(
                        {
                            "imessage_backup_root": str(local),
                            "imessage_local_vars_path": str(root / "missing.yml"),
                            "imessage_lifecycle_state_directory_name": ".state",
                            "imessage_nextcloud_enabled": True,
                            "imessage_nextcloud_remote": fake_remote,
                            "imessage_nextcloud_rclone_binary": str(FAKE_RCLONE),
                            "imessage_remote_superseded_retirement_enabled": True,
                        }
                    ),
                ],
                cwd=REPOSITORY,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertFalse((remote / older).exists())
            self.assertFalse((remote_markers / f"{older}.json").exists())
            self.assertFalse(
                (local / ".state" / "catalog-cache" / older).exists()
            )
            self.assertTrue((remote / newer / "payload").is_file())
            self.assertTrue((remote_markers / f"{newer}.json").is_file())
            report = json.loads(
                (
                    local
                    / ".state"
                    / "remote-superseded-retirement.json"
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(report["automatic_deletion_performed"])
            self.assertEqual(report["archive_names"], [older])


if __name__ == "__main__":
    unittest.main()
