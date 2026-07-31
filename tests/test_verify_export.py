from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_export.py"


class VerifyExportTests(unittest.TestCase):
    def run_verifier(
        self, root: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(root), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_valid_export_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "attachments").mkdir()
            (root / "attachments" / "photo.jpg").write_bytes(b"image")
            (root / "chat.html").write_text(
                '<html><body><img src="attachments/photo.jpg"></body></html>',
                encoding="utf-8",
            )

            result = self.run_verifier(root)
            report = json.loads(result.stdout)

            self.assertEqual(result.returncode, 0)
            self.assertTrue(report["passed"])
            self.assertEqual(report["broken_reference_count"], 0)

    def test_broken_reference_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "chat.html").write_text(
                '<html><body><img src="missing.jpg"></body></html>',
                encoding="utf-8",
            )

            result = self.run_verifier(root)
            report = json.loads(result.stdout)

            self.assertEqual(result.returncode, 1)
            self.assertFalse(report["passed"])
            self.assertEqual(report["broken_reference_count"], 1)

    def test_incomplete_html_and_empty_files_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "chat.html").write_text("<html><body>", encoding="utf-8")
            (root / "empty.dat").touch()

            result = self.run_verifier(root)
            report = json.loads(result.stdout)

            self.assertEqual(result.returncode, 1)
            self.assertEqual(report["incomplete_html_count"], 1)
            self.assertEqual(report["empty_file_count"], 1)

    def test_optional_custom_stylesheet_may_be_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "chat.html").write_text(
                '<html><head><link rel="stylesheet" href="style.css"></head></html>',
                encoding="utf-8",
            )

            result = self.run_verifier(root)
            report = json.loads(result.stdout)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(report["broken_reference_count"], 0)

    def test_known_broken_reference_can_be_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "chat.html").write_text(
                '<html><body><img src="missing.jpg"></body></html>',
                encoding="utf-8",
            )

            result = self.run_verifier(root, "--allow-broken-references")
            report = json.loads(result.stdout)

            self.assertEqual(result.returncode, 0)
            self.assertTrue(report["passed"])
            self.assertEqual(report["broken_reference_count"], 1)

    def test_failed_source_preservation_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "chat.html").write_text(
                "<html><body></body></html>",
                encoding="utf-8",
            )
            (root / "source-preservation.json").write_text(
                json.dumps(
                    {
                        "passed": False,
                        "summary": {
                            "locally_readable_rows_unpreserved": 1,
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_verifier(root)
            report = json.loads(result.stdout)

            self.assertEqual(result.returncode, 1)
            self.assertFalse(report["passed"])
            self.assertTrue(report["source_preservation_present"])
            self.assertFalse(report["source_preservation_passed"])


if __name__ == "__main__":
    unittest.main()
