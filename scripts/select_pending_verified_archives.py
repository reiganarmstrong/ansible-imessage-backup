#!/usr/bin/env python3
"""Select locally verified archives not verified at the configured remote."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def normalized_remote(value: str) -> str:
    return value.rstrip("/")


def pending_archives(
    backup_root: Path,
    backup_prefix: str,
    state_directory_name: str,
    remote_root: str,
) -> list[Path]:
    expected_root = normalized_remote(remote_root)
    marker_root = backup_root / state_directory_name / "remote-verifications"
    pending: list[Path] = []
    for archive_path in sorted(backup_root.glob(f"{backup_prefix}-*")):
        if not archive_path.is_dir():
            continue
        verification = load_json(archive_path / "verification.json")
        if not isinstance(verification, dict) or verification.get("passed") is not True:
            continue
        marker = load_json(marker_root / f"{archive_path.name}.json")
        expected_destination = f"{expected_root}/{archive_path.name}"
        if (
            isinstance(marker, dict)
            and marker.get("passed") is True
            and marker.get("archive_name") == archive_path.name
            and normalized_remote(str(marker.get("remote_destination", "")))
            == expected_destination
        ):
            continue
        pending.append(archive_path.resolve())
    return pending


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup_root", type=Path)
    parser.add_argument("--backup-prefix", default="imessage-backup")
    parser.add_argument(
        "--state-directory-name",
        default=".imessage-archive-state",
    )
    parser.add_argument("--remote-root", required=True)
    args = parser.parse_args()

    selected = pending_archives(
        args.backup_root.expanduser().resolve(),
        args.backup_prefix,
        args.state_directory_name,
        args.remote_root,
    )
    print(
        json.dumps(
            {
                "count": len(selected),
                "archive_paths": [str(path) for path in selected],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
