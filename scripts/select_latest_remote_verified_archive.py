#!/usr/bin/env python3
"""Select the newest archive with a marker for the configured remote root."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re


RUN_ID_PATTERN = re.compile(r"(\d{8}T\d{6})$")


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def archive_order(name: str, marker_path: Path) -> tuple[float, str]:
    match = RUN_ID_PATTERN.search(name)
    if match:
        try:
            created = datetime.strptime(
                match.group(1),
                "%Y%m%dT%H%M%S",
            ).replace(tzinfo=timezone.utc)
            return created.timestamp(), name
        except ValueError:
            pass
    return marker_path.stat().st_mtime, name


def select_latest(
    backup_root: Path,
    backup_prefix: str,
    state_directory_name: str,
    remote_root: str,
) -> str | None:
    normalized_root = remote_root.rstrip("/")
    marker_root = backup_root / state_directory_name / "remote-verifications"
    candidates: list[tuple[str, Path]] = []
    for marker_path in marker_root.glob("*.json"):
        marker = load_json(marker_path)
        if not isinstance(marker, dict) or marker.get("passed") is not True:
            continue
        archive_name = marker.get("archive_name")
        if (
            not isinstance(archive_name, str)
            or not archive_name.startswith(f"{backup_prefix}-")
            or archive_name != Path(archive_name).name
        ):
            continue
        expected_destination = f"{normalized_root}/{archive_name}"
        if str(marker.get("remote_destination", "")).rstrip("/") != expected_destination:
            continue
        candidates.append((archive_name, marker_path))
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda candidate: archive_order(candidate[0], candidate[1]),
    )[0]


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

    selected = select_latest(
        args.backup_root.expanduser().resolve(),
        args.backup_prefix,
        args.state_directory_name,
        args.remote_root,
    )
    if selected is None:
        parser.error("No archive has a valid marker for this remote root")
    print(json.dumps({"archive_name": selected}, sort_keys=True))


if __name__ == "__main__":
    main()
