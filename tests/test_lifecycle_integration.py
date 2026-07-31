from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).parents[1]
ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")
FAKE_RCLONE = REPOSITORY / "tests" / "fixtures" / "fake_rclone.py"
VERIFIED_ARCHIVE = REPOSITORY / "tests" / "fixtures" / "verified-archive"
ARCHIVE_NAME = "imessage-backup-20240101T000000"
OLDER_ARCHIVE_NAME = "imessage-backup-20231201T000000"


@unittest.skipUnless(ANSIBLE_PLAYBOOK, "ansible-playbook is not installed")
class LifecycleIntegrationTests(unittest.TestCase):
    def add_catalog_data(self, archive: Path) -> None:
        preservation = archive / "Source Preservation"
        preservation.mkdir(parents=True)
        (preservation / "attachment-manifest.json").write_text(
            "[]\n",
            encoding="utf-8",
        )
        connection = sqlite3.connect(preservation / "chat.db")
        connection.executescript(
            """
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT NOT NULL,
                text TEXT,
                subject TEXT,
                date INTEGER,
                is_from_me INTEGER,
                handle_id INTEGER
            );
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                display_name TEXT,
                chat_identifier TEXT
            );
            CREATE TABLE chat_message_join (
                chat_id INTEGER,
                message_id INTEGER
            );
            INSERT INTO handle VALUES (1, 'fixture@example.com');
            INSERT INTO chat VALUES (
                1,
                'FIXTURE-CHAT',
                'Fixture Chat',
                'fixture'
            );
            INSERT INTO message VALUES (
                1,
                'FIXTURE-GUID',
                'Lifecycle integration fixture',
                NULL,
                725846400000000000,
                0,
                1
            );
            INSERT INTO chat_message_join VALUES (1, 1);
            """
        )
        connection.commit()
        connection.close()

    def run_playbook(
        self,
        playbook: str,
        extra_vars: dict[str, object],
    ) -> subprocess.CompletedProcess[str]:
        assert ANSIBLE_PLAYBOOK
        return subprocess.run(
            [
                ANSIBLE_PLAYBOOK,
                str(REPOSITORY / playbook),
                "--inventory",
                str(REPOSITORY / "inventory.ini"),
                "--extra-vars",
                json.dumps(extra_vars),
            ],
            cwd=REPOSITORY,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_remote_mismatch_invalidates_marker_and_metadata_recovers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            local = root / "local"
            remote = root / "remote"
            archive = local / ARCHIVE_NAME
            older_archive = local / OLDER_ARCHIVE_NAME
            local.mkdir()
            remote.mkdir()
            shutil.copytree(VERIFIED_ARCHIVE, archive)
            shutil.copytree(VERIFIED_ARCHIVE, older_archive)
            self.add_catalog_data(archive)
            self.add_catalog_data(older_archive)
            local_vars = root / "local.yml"
            nextcloud_variables = {
                "imessage_nextcloud_enabled": True,
                "imessage_nextcloud_rclone_binary": str(FAKE_RCLONE),
                "imessage_nextcloud_remote": f"fake:{remote}",
                "imessage_backup_root": str(local),
                "imessage_lifecycle_archive_path": str(archive),
                "imessage_local_vars_path": str(local_vars),
            }
            activation_variables = {
                "imessage_activation_remote": f"fake:{remote}",
                "imessage_activation_install_schedule": False,
                "imessage_nextcloud_rclone_binary": str(FAKE_RCLONE),
                "imessage_backup_root": str(local),
                "imessage_local_vars_path": str(local_vars),
            }
            marker = (
                local
                / ".imessage-archive-state"
                / "remote-verifications"
                / f"{ARCHIVE_NAME}.json"
            )
            older_marker = (
                local
                / ".imessage-archive-state"
                / "remote-verifications"
                / f"{OLDER_ARCHIVE_NAME}.json"
            )

            initial = self.run_playbook(
                "activate-nextcloud.yml",
                activation_variables,
            )
            self.assertEqual(initial.returncode, 0, initial.stderr + initial.stdout)
            self.assertTrue(marker.is_file())
            self.assertTrue(older_marker.is_file())
            self.assertTrue((remote / ARCHIVE_NAME / "conversation.html").is_file())
            self.assertTrue(
                (remote / ARCHIVE_NAME / ".synthetic-hidden").is_file()
            )
            self.assertTrue(
                (remote / OLDER_ARCHIVE_NAME / "conversation.html").is_file()
            )
            current_marker_data = json.loads(
                marker.read_text(encoding="utf-8")
            )
            current_files = [
                path for path in archive.rglob("*") if path.is_file()
            ]
            self.assertEqual(
                current_marker_data["file_count"],
                len(current_files),
            )
            self.assertEqual(
                current_marker_data["total_bytes"],
                sum(path.stat().st_size for path in current_files),
            )
            self.assertEqual(
                older_marker.read_bytes(),
                (
                    remote
                    / ".imessage-archive-state"
                    / "remote-verifications"
                    / f"{OLDER_ARCHIVE_NAME}.json"
                ).read_bytes(),
            )
            self.assertEqual(
                (local / "index.html").read_bytes(),
                (remote / "index.html").read_bytes(),
            )
            self.assertEqual(
                (local / "pruning-report.json").read_bytes(),
                (remote / "pruning-report.json").read_bytes(),
            )
            persisted = local_vars.read_text(encoding="utf-8")
            self.assertIn("imessage_nextcloud_enabled: true", persisted)
            self.assertIn(f"fake:{remote}", persisted)
            self.assertEqual(local_vars.stat().st_mode & 0o777, 0o600)

            shutil.rmtree(older_archive)
            hydration = self.run_playbook(
                "catalog-hydrate.yml",
                nextcloud_variables,
            )
            self.assertEqual(
                hydration.returncode,
                0,
                hydration.stderr + hydration.stdout,
            )
            cached_older = (
                local
                / ".imessage-archive-state"
                / "catalog-cache"
                / OLDER_ARCHIVE_NAME
            )
            self.assertTrue(
                (
                    cached_older / ".catalog-cache-verification.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    cached_older / "Source Preservation" / "chat.db"
                ).is_file()
            )
            self.assertFalse(
                (cached_older / ".synthetic-hidden").exists()
            )
            catalog_rebuild = self.run_playbook(
                "catalog.yml",
                nextcloud_variables,
            )
            self.assertEqual(
                catalog_rebuild.returncode,
                0,
                catalog_rebuild.stderr + catalog_rebuild.stdout,
            )
            pruning_rebuild = self.run_playbook(
                "pruning-report.yml",
                nextcloud_variables,
            )
            self.assertEqual(
                pruning_rebuild.returncode,
                0,
                pruning_rebuild.stderr + pruning_rebuild.stdout,
            )
            hydrated_catalog = json.loads(
                (local / "archive-catalog.json").read_text(encoding="utf-8")
            )
            self.assertEqual(hydrated_catalog["archive_count"], 2)
            self.assertEqual(
                hydrated_catalog["remote_verified_archive_count"],
                2,
            )
            self.assertEqual(
                {
                    archive["name"]: archive["catalog_source"]
                    for archive in hydrated_catalog["archives"]
                },
                {
                    ARCHIVE_NAME: "local_archive",
                    OLDER_ARCHIVE_NAME: "remote_cache",
                },
            )

            restore_root = root / "restores"
            restore = self.run_playbook(
                "restore-test.yml",
                {
                    "imessage_backup_root": str(local),
                    "imessage_local_vars_path": str(local_vars),
                    "imessage_restore_root": str(restore_root),
                    "imessage_restore_archive_name": OLDER_ARCHIVE_NAME,
                    "imessage_restore_run_id": "integration",
                    "imessage_restore_minimum_free_bytes_after": 0,
                    "imessage_restore_minimum_free_percent_after": 0,
                },
            )
            self.assertEqual(
                restore.returncode,
                0,
                restore.stderr + restore.stdout,
            )
            restored_archive = (
                restore_root
                / f"{OLDER_ARCHIVE_NAME}-restore-integration"
            )
            restore_result = json.loads(
                (restored_archive / "RESTORE-TEST.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(restore_result["passed"])
            self.assertTrue(
                restore_result["structural_verification"]["passed"]
            )
            self.assertEqual(
                (restored_archive / "conversation.html").read_bytes(),
                (remote / OLDER_ARCHIVE_NAME / "conversation.html").read_bytes(),
            )
            restore_check = (
                restore_root
                / ".restore-state"
                / f"{restored_archive.name}-remote-check.txt"
            )
            self.assertEqual(restore_check.stat().st_mode & 0o777, 0o600)
            persistent_restore_state = (
                local
                / ".imessage-archive-state"
                / "restore-verifications"
            )
            persistent_result = (
                persistent_restore_state / f"{restored_archive.name}.json"
            )
            self.assertEqual(
                persistent_result.stat().st_mode & 0o777,
                0o600,
            )
            self.assertTrue(
                json.loads(persistent_result.read_text(encoding="utf-8"))[
                    "restore_copy_retained"
                ]
            )
            self.assertEqual(
                (
                    persistent_restore_state
                    / f"{restored_archive.name}-remote-check.txt"
                ).stat().st_mode
                & 0o777,
                0o600,
            )

            transient_restore = self.run_playbook(
                "restore-test.yml",
                {
                    "imessage_backup_root": str(local),
                    "imessage_local_vars_path": str(local_vars),
                    "imessage_restore_root": str(restore_root),
                    "imessage_restore_archive_name": OLDER_ARCHIVE_NAME,
                    "imessage_restore_run_id": "transient",
                    "imessage_restore_keep_copy": False,
                    "imessage_restore_minimum_free_bytes_after": 0,
                    "imessage_restore_minimum_free_percent_after": 0,
                },
            )
            self.assertEqual(
                transient_restore.returncode,
                0,
                transient_restore.stderr + transient_restore.stdout,
            )
            transient_name = f"{OLDER_ARCHIVE_NAME}-restore-transient"
            self.assertFalse((restore_root / transient_name).exists())
            self.assertFalse(
                (
                    restore_root
                    / ".restore-state"
                    / f"{transient_name}-remote-check.txt"
                ).exists()
            )
            transient_result = json.loads(
                (
                    persistent_restore_state / f"{transient_name}.json"
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(transient_result["passed"])
            self.assertFalse(transient_result["restore_copy_retained"])
            self.assertTrue(
                (
                    persistent_restore_state
                    / f"{transient_name}-remote-check.txt"
                ).is_file()
            )

            unexpected = remote / ARCHIVE_NAME / "unexpected.json"
            unexpected.write_text("{}\n", encoding="utf-8")
            mismatch = self.run_playbook(
                "nextcloud-archive.yml",
                nextcloud_variables,
            )
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertFalse(marker.exists())

            unexpected.unlink()
            recovered = self.run_playbook(
                "nextcloud-archive.yml",
                nextcloud_variables,
            )
            self.assertEqual(
                recovered.returncode,
                0,
                recovered.stderr + recovered.stdout,
            )
            self.assertTrue(marker.is_file())
            pruning_report = json.loads(
                (local / "pruning-report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                pruning_report["status"],
                "ready_for_manual_review",
            )
            self.assertEqual(
                pruning_report["remote_verified_archive_count"],
                2,
            )

            metadata = self.run_playbook(
                "nextcloud-metadata.yml",
                nextcloud_variables,
            )
            self.assertEqual(
                metadata.returncode,
                0,
                metadata.stderr + metadata.stdout,
            )

            retirement_variables = {
                **nextcloud_variables,
                "imessage_local_retirement_enabled": True,
                "imessage_local_retirement_keep_count": 0,
                "imessage_local_retirement_require_restore_attestation": True,
            }
            hydrate_current = self.run_playbook(
                "catalog-hydrate.yml",
                retirement_variables,
            )
            self.assertEqual(
                hydrate_current.returncode,
                0,
                hydrate_current.stderr + hydrate_current.stdout,
            )
            retire_current = self.run_playbook(
                "retire-local-archives.yml",
                retirement_variables,
            )
            self.assertEqual(
                retire_current.returncode,
                0,
                retire_current.stderr + retire_current.stdout,
            )
            self.assertFalse(archive.exists())
            self.assertTrue(
                (
                    local
                    / ".imessage-archive-state"
                    / "catalog-cache"
                    / ARCHIVE_NAME
                    / ".catalog-cache-verification.json"
                ).is_file()
            )
            retirement_report = json.loads(
                (
                    local
                    / ".imessage-archive-state"
                    / "local-retirement.json"
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(
                retirement_report["automatic_deletion_performed"]
            )
            self.assertEqual(retirement_report["count"], 1)


if __name__ == "__main__":
    unittest.main()
