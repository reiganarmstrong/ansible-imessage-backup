from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "create_heic_previews.py"
SPEC = importlib.util.spec_from_file_location("create_heic_previews", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CreateHeicPreviewsTests(unittest.TestCase):
    def test_merges_multi_pass_reports_and_keeps_current_missing_state(self) -> None:
        previous = {
            "created_count": 2,
            "patched_html_count": 3,
            "quicklook_fallback_count": 1,
            "missing_heic_reference_count": 1,
            "missing_heic_references": ["old.html -> missing.heic"],
            "results": [{"source": "old.heic", "status": "created"}],
        }
        current = {
            "created_count": 1,
            "preview_count": 1,
            "patched_html_count": 1,
            "quicklook_fallback_count": 0,
            "missing_heic_reference_count": 0,
            "missing_heic_references": [],
            "results": [{"source": "new.heic", "status": "created"}],
        }

        merged = MODULE.merge_preview_reports(current, [previous])

        self.assertEqual(merged["created_count"], 3)
        self.assertEqual(merged["preview_count"], 2)
        self.assertEqual(merged["patched_html_count"], 4)
        self.assertEqual(merged["missing_heic_reference_count"], 0)
        self.assertEqual(
            [result["source"] for result in merged["results"]],
            ["new.heic", "old.heic"],
        )

    def test_discovers_existing_and_missing_heic_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            export_path = Path(temporary_directory)
            attachments = export_path / "attachments"
            attachments.mkdir()
            (attachments / "present.HEIC").write_bytes(b"synthetic")
            conversation = export_path / "Conversation.html"
            conversation.write_text(
                """
                <html><body>
                  <img src="attachments/present.HEIC">
                  <img src="attachments/missing.heic">
                  <img src="attachments/already.jpeg">
                </body></html>
                """,
                encoding="utf-8",
            )

            references, missing = MODULE.discover_heic_references(export_path)

            self.assertEqual(len(references), 1)
            self.assertEqual(len(missing), 1)
            self.assertIn("attachments/missing.heic", missing[0])

    def test_patches_only_image_source_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            export_path = Path(temporary_directory)
            source = export_path / "photo.HEIC"
            preview = export_path / "photo.jpeg"
            source.write_bytes(b"source")
            preview.write_bytes(b"preview")
            conversation = export_path / "Conversation.html"
            conversation.write_text(
                '<html><body><img src="photo.HEIC"></body></html>',
                encoding="utf-8",
            )

            patched_count = MODULE.patch_html_references(
                {source: [(conversation, "photo.HEIC")]},
                {source: preview},
            )

            self.assertEqual(patched_count, 1)
            self.assertIn(
                'src="photo.jpeg"',
                conversation.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
