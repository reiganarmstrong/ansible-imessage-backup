from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "build_pruning_report.py"
SPEC = importlib.util.spec_from_file_location("build_pruning_report", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class BuildPruningReportTests(unittest.TestCase):
    def test_single_verified_complete_copy_is_enough_in_single_copy_mode(
        self,
    ) -> None:
        catalog = {
            "archives": [
                {
                    "name": "backup-1",
                    "created_at": "2024-01-08T00:00:00Z",
                    "maximum_message_timestamp": "2024-01-08T00:00:00Z",
                    "remote_verified": True,
                }
            ],
            "conversations": [{"guid": "CHAT", "name": "Test Chat"}],
        }
        messages = {
            "message_fields": [
                "guid",
                "timestamp",
                "conversation_indexes",
                "copies",
            ],
            "copy_fields": ["archive_index", "attachment_complete"],
            "messages": [
                ["ELIGIBLE", "2024-01-06T00:00:00Z", [0], [[0, True]]],
                ["MISSING", "2024-01-06T00:00:01Z", [0], [[0, False]]],
                ["TOO-NEW", "2024-01-08T00:00:00Z", [0], [[0, True]]],
            ],
        }

        report = MODULE.build_pruning_report(
            catalog,
            messages,
            minimum_verified_copies=1,
            safety_hours=1,
        )

        self.assertEqual(report["status"], "ready_for_manual_review")
        self.assertEqual(report["eligible_message_guids"], ["ELIGIBLE"])
        self.assertEqual(
            report["blocked_reasons"],
            {"insufficient_attachment_complete_remote_copies": 1},
        )

    def test_requires_two_complete_remote_copies(self) -> None:
        catalog = {
            "archives": [
                {
                    "name": "backup-1",
                    "created_at": "2024-01-08T00:00:00Z",
                    "maximum_message_timestamp": "2024-01-08T00:00:00Z",
                    "remote_verified": True,
                },
                {
                    "name": "backup-2",
                    "created_at": "2024-01-15T00:00:00Z",
                    "maximum_message_timestamp": "2024-01-15T00:00:00Z",
                    "remote_verified": True,
                },
            ]
        }
        messages = {
            "message_fields": [
                "guid",
                "timestamp",
                "conversation_indexes",
                "copies",
            ],
            "copy_fields": [
                "archive_index",
                "attachment_complete",
            ],
            "messages": [
                [
                    "ELIGIBLE",
                    "2024-01-06T00:00:00Z",
                    [0],
                    [[0, True], [1, True]],
                ],
                [
                    "MISSING-ASSET",
                    "2024-01-06T00:00:01Z",
                    [0],
                    [[0, False], [1, True]],
                ],
                [
                    "TOO-NEW",
                    "2024-01-08T00:00:01Z",
                    [0],
                    [[0, True], [1, True]],
                ],
            ]
        }
        catalog["conversations"] = [{"guid": "CHAT", "name": "Test Chat"}]

        report = MODULE.build_pruning_report(
            catalog,
            messages,
            minimum_verified_copies=2,
            safety_hours=0,
        )

        self.assertEqual(report["eligible_message_guids"], ["ELIGIBLE"])
        self.assertEqual(report["eligible_message_count"], 1)
        self.assertEqual(report["blocked_message_count"], 1)
        self.assertIsNone(report["safe_contiguous_cutoff"])
        self.assertEqual(
            report["blocked_reasons"],
            {"insufficient_attachment_complete_remote_copies": 1},
        )

    def test_reports_insufficient_verified_archives(self) -> None:
        report = MODULE.build_pruning_report(
            {
                "archives": [
                    {
                        "name": "backup-1",
                        "created_at": "2024-01-08T00:00:00Z",
                        "maximum_message_timestamp": "2024-01-08T00:00:00Z",
                        "remote_verified": True,
                    }
                ]
            },
            {
                "message_fields": [],
                "copy_fields": [],
                "messages": [],
            },
            minimum_verified_copies=2,
            safety_hours=24,
        )

        self.assertEqual(
            report["status"],
            "insufficient_remote_verified_archives",
        )
        self.assertEqual(report["eligible_message_count"], 0)

    def test_ignores_messages_absent_from_newest_archive(self) -> None:
        catalog = {
            "archives": [
                {
                    "name": f"backup-{index}",
                    "created_at": f"2024-01-{index:02d}T00:00:00Z",
                    "maximum_message_timestamp": (
                        f"2024-01-{index:02d}T00:00:00Z"
                    ),
                    "remote_verified": True,
                }
                for index in (1, 8, 15)
            ],
            "conversations": [{"guid": "CHAT", "name": "Test Chat"}],
        }
        messages = {
            "message_fields": [
                "guid",
                "timestamp",
                "conversation_indexes",
                "copies",
            ],
            "copy_fields": ["archive_index", "attachment_complete"],
            "messages": [
                [
                    "ALREADY-ABSENT",
                    "2024-01-01T00:00:00Z",
                    [0],
                    [[0, True], [1, True]],
                ]
            ],
        }

        report = MODULE.build_pruning_report(
            catalog,
            messages,
            minimum_verified_copies=2,
            safety_hours=0,
        )

        self.assertEqual(report["target_archive"], "backup-15")
        self.assertEqual(report["eligible_message_count"], 0)
        self.assertEqual(report["blocked_message_count"], 0)


if __name__ == "__main__":
    unittest.main()
