from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "verify_source_preservation.py"
)
SPEC = importlib.util.spec_from_file_location("verify_source_preservation", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class VerifySourcePreservationTests(unittest.TestCase):
    def build_archive(self, root: Path) -> tuple[Path, Path]:
        export = root / "export"
        preservation = export / "Source Preservation"
        archive_file = preservation / "attachments" / "01" / "1-photo.jpg"
        archive_file.parent.mkdir(parents=True)
        archive_file.write_bytes(b"preserved-image")
        source_file = root / "photo.jpg"
        source_file.write_bytes(b"preserved-image")

        database = sqlite3.connect(preservation / "chat.db")
        database.execute("CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY)")
        database.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY)")
        database.execute("INSERT INTO attachment VALUES (1)")
        database.execute("INSERT INTO message VALUES (1)")
        database.commit()
        database.close()

        import hashlib

        digest = hashlib.sha256(b"preserved-image").hexdigest()
        (preservation / "attachment-manifest.json").write_text(
            json.dumps(
                [
                    {
                        "row_id": 1,
                        "source_present": True,
                        "separately_recovered": False,
                        "source_filename": str(source_file),
                        "archive_path": (
                            "Source Preservation/attachments/01/1-photo.jpg"
                        ),
                        "sha256": digest,
                    }
                ]
            ),
            encoding="utf-8",
        )
        (export / "source-preservation.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "preservation_directory": "Source Preservation",
                    "summary": {
                        "locally_readable_rows": 1,
                        "locally_readable_rows_unpreserved": 0,
                        "separately_recovered_rows": 0,
                    },
                }
            ),
            encoding="utf-8",
        )
        return export, archive_file

    def test_verified_archive_and_live_source_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            export, _ = self.build_archive(Path(temporary_directory))

            report, passed = MODULE.verify(export, verify_live_source=True)

            self.assertTrue(passed)
            self.assertTrue(report["passed"])
            self.assertEqual(
                report["locally_readable_rows_with_verified_archive_bytes"],
                1,
            )
            self.assertEqual(report["snapshot_quick_check"], "ok")

    def test_tampered_archive_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            export, archive_file = self.build_archive(Path(temporary_directory))
            archive_file.write_bytes(b"tampered")

            report, passed = MODULE.verify(export, verify_live_source=True)

            self.assertFalse(passed)
            self.assertEqual(report["archive_digest_mismatch_count"], 1)

    def test_tampered_hidden_counterpart_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            export, _ = self.build_archive(Path(temporary_directory))
            hidden = (
                export
                / "Recovered Attachments"
                / "iOS Backup"
                / "Hidden Counterparts"
            )
            hidden.mkdir(parents=True)
            payload = hidden / "payload.pluginPayloadAttachment"
            payload.write_bytes(b"payload")
            import hashlib

            digest = hashlib.sha256(b"payload").hexdigest()
            (hidden / "hidden-counterparts.json").write_text(
                json.dumps(
                    {
                        "counterparts": [
                            {
                                "archive_path": str(payload.relative_to(export)),
                                "sha256": digest,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            payload.write_bytes(b"tampered")

            report, passed = MODULE.verify(export, verify_live_source=True)

            self.assertFalse(passed)
            self.assertFalse(
                report["checks"]["hidden_counterparts_verified"]
            )
            self.assertEqual(
                report["hidden_counterpart_digest_mismatch_count"],
                1,
            )


if __name__ == "__main__":
    unittest.main()
