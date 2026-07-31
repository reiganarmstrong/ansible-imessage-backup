from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "check_restore_capacity.py"
)
SPEC = importlib.util.spec_from_file_location(
    "check_restore_capacity",
    SCRIPT,
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CheckRestoreCapacityTests(unittest.TestCase):
    def test_requires_larger_of_byte_and_percentage_reserves(self) -> None:
        report = MODULE.evaluate_capacity(
            filesystem_total_bytes=500,
            filesystem_free_bytes=100,
            restore_bytes=30,
            minimum_free_bytes_after=20,
            minimum_free_percent_after=20,
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["required_reserve_bytes"], 100)
        self.assertEqual(report["projected_free_bytes_after"], 70)
        self.assertEqual(report["additional_bytes_required"], 30)

    def test_passes_when_projected_free_space_meets_reserve(self) -> None:
        report = MODULE.evaluate_capacity(
            filesystem_total_bytes=500,
            filesystem_free_bytes=200,
            restore_bytes=30,
            minimum_free_bytes_after=20,
            minimum_free_percent_after=10,
        )
        self.assertTrue(report["passed"])
        self.assertEqual(report["required_reserve_bytes"], 50)
        self.assertEqual(report["additional_bytes_required"], 0)
