#!/usr/bin/env python3
"""Select the newest locally verified timestamped iMessage archive."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re


RUN_ID_PATTERN = re.compile(r"(\d{8}T\d{6})$")


def archive_order(path: Path) -> tuple[float, str]:
    match = RUN_ID_PATTERN.search(path.name)
    if match:
        try:
            created = datetime.strptime(
                match.group(1),
                "%Y%m%dT%H%M%S",
            ).replace(tzinfo=timezone.utc)
            return created.timestamp(), path.name
        except ValueError:
            pass
    return path.stat().st_mtime, path.name


def is_verified_archive(path: Path) -> bool:
    verification_path = path / "verification.json"
    try:
        verification = json.loads(
            verification_path.read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    return isinstance(verification, dict) and verification.get("passed") is True


def select_latest(
    backup_root: Path,
    backup_prefix: str,
) -> Path | None:
    candidates = [
        path
        for path in backup_root.glob(f"{backup_prefix}-*")
        if path.is_dir() and is_verified_archive(path)
    ]
    return max(candidates, key=archive_order) if candidates else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup_root", type=Path)
    parser.add_argument("--backup-prefix", default="imessage-backup")
    args = parser.parse_args()
    backup_root = args.backup_root.expanduser().resolve()
    selected = select_latest(backup_root, args.backup_prefix)
    if selected is None:
        parser.error(
            f"No locally verified {args.backup_prefix}-* archive exists in "
            f"{backup_root}"
        )
    print(
        json.dumps(
            {
                "archive_name": selected.name,
                "archive_path": str(selected),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
