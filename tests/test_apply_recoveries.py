from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "apply_recoveries.py"
SPEC = importlib.util.spec_from_file_location("apply_recoveries", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ApplyRecoveriesTests(unittest.TestCase):
    def test_recovered_video_replaces_matching_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            export_path = Path(temporary_directory)
            recovered_path = export_path / "Recovered Attachments"
            recovered_path.mkdir()
            (recovered_path / "recovered.mov").write_bytes(b"video")
            conversation = export_path / "Conversation.html"
            conversation.write_text(
                """
                <html><body>
                <div class="message">
                  <a href="sms://open?message-guid=TEST-GUID">Date</a>
                  <span class="attachment_error">Unable to locate attachment: original.mov</span>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )

            result = MODULE.apply_recovery(
                export_path,
                recovered_path,
                {
                    "message_guid": "TEST-GUID",
                    "missing_filename": "original.mov",
                    "recovered_file": "recovered.mov",
                },
            )
            updated = conversation.read_text(encoding="utf-8")

            self.assertEqual(result["status"], "patched")
            self.assertNotIn("attachment_error", updated)
            self.assertIn("<video controls>", updated)
            self.assertIn("Recovered%20Attachments/recovered.mov", updated)

    def test_wrong_message_guid_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            export_path = Path(temporary_directory)
            recovered_path = export_path / "Recovered Attachments"
            recovered_path.mkdir()
            (recovered_path / "recovered.jpg").write_bytes(b"image")
            (export_path / "Conversation.html").write_text(
                '<html><body><div class="message"></div></body></html>',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "No exported conversation"):
                MODULE.apply_recovery(
                    export_path,
                    recovered_path,
                    {
                        "message_guid": "MISSING-GUID",
                        "missing_filename": "original.jpg",
                        "recovered_file": "recovered.jpg",
                    },
                )


if __name__ == "__main__":
    unittest.main()
