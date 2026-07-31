#!/usr/bin/env python3
"""Check that a restore leaves a conservative amount of free disk space."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil


def existing_parent(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise FileNotFoundError(path)
        candidate = parent
    return candidate


def evaluate_capacity(
    filesystem_total_bytes: int,
    filesystem_free_bytes: int,
    restore_bytes: int,
    minimum_free_bytes_after: int,
    minimum_free_percent_after: float,
) -> dict[str, object]:
    percent_reserve = int(
        filesystem_total_bytes * minimum_free_percent_after / 100
    )
    required_reserve = max(minimum_free_bytes_after, percent_reserve)
    projected_free = filesystem_free_bytes - restore_bytes
    return {
        "passed": projected_free >= required_reserve,
        "filesystem_total_bytes": filesystem_total_bytes,
        "filesystem_free_bytes_before": filesystem_free_bytes,
        "restore_bytes": restore_bytes,
        "projected_free_bytes_after": projected_free,
        "minimum_free_bytes_after": minimum_free_bytes_after,
        "minimum_free_percent_after": minimum_free_percent_after,
        "percent_based_reserve_bytes": percent_reserve,
        "required_reserve_bytes": required_reserve,
        "additional_bytes_required": max(0, required_reserve - projected_free),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("restore_root", type=Path)
    parser.add_argument("--restore-bytes", type=int, required=True)
    parser.add_argument(
        "--minimum-free-bytes-after",
        type=int,
        default=20 * 1024**3,
    )
    parser.add_argument(
        "--minimum-free-percent-after",
        type=float,
        default=10,
    )
    args = parser.parse_args()
    if args.restore_bytes < 0:
        parser.error("--restore-bytes cannot be negative")
    if args.minimum_free_bytes_after < 0:
        parser.error("--minimum-free-bytes-after cannot be negative")
    if not 0 <= args.minimum_free_percent_after <= 100:
        parser.error("--minimum-free-percent-after must be between 0 and 100")

    filesystem_path = existing_parent(args.restore_root)
    usage = shutil.disk_usage(filesystem_path)
    report = evaluate_capacity(
        usage.total,
        usage.free,
        args.restore_bytes,
        args.minimum_free_bytes_after,
        args.minimum_free_percent_after,
    )
    report["filesystem_path"] = str(filesystem_path)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
