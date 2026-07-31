#!/usr/bin/env python3
"""Select verified remote-only archives that need a compact catalog cache."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def valid_remote_marker(
    marker: object,
    archive_name: str,
    remote_root: str,
) -> bool:
    return (
        isinstance(marker, dict)
        and marker.get("passed") is True
        and marker.get("archive_name") == archive_name
        and str(marker.get("remote_destination", "")).rstrip("/")
        == f"{remote_root.rstrip('/')}/{archive_name}"
    )


def valid_cache_marker(
    marker: object,
    remote_marker: dict[str, object],
    archive_name: str,
) -> bool:
    return (
        isinstance(marker, dict)
        and marker.get("passed") is True
        and marker.get("archive_name") == archive_name
        and marker.get("remote_destination")
        == remote_marker.get("remote_destination")
        and marker.get("remote_verified_at")
        == remote_marker.get("verified_at")
        and marker.get("remote_file_count")
        == remote_marker.get("file_count")
        and marker.get("remote_total_bytes")
        == remote_marker.get("total_bytes")
    )


def archives_requiring_cache(
    backup_root: Path,
    backup_prefix: str,
    state_directory_name: str,
    cache_directory_name: str,
    remote_root: str,
    include_local_archives: bool = False,
) -> list[str]:
    state_root = backup_root / state_directory_name
    marker_root = state_root / "remote-verifications"
    cache_root = state_root / cache_directory_name
    selected: list[str] = []
    for marker_path in sorted(marker_root.glob("*.json")):
        archive_name = marker_path.stem
        if (
            not archive_name.startswith(f"{backup_prefix}-")
            or archive_name != Path(archive_name).name
        ):
            continue
        marker = load_json(marker_path)
        if not valid_remote_marker(marker, archive_name, remote_root):
            continue
        assert isinstance(marker, dict)
        if (
            (backup_root / archive_name).is_dir()
            and not include_local_archives
        ):
            continue
        cache_marker = load_json(
            cache_root / archive_name / ".catalog-cache-verification.json"
        )
        if valid_cache_marker(cache_marker, marker, archive_name):
            continue
        selected.append(archive_name)
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup_root", type=Path)
    parser.add_argument("--backup-prefix", default="imessage-backup")
    parser.add_argument(
        "--state-directory-name",
        default=".imessage-archive-state",
    )
    parser.add_argument(
        "--cache-directory-name",
        default="catalog-cache",
    )
    parser.add_argument("--remote-root", required=True)
    parser.add_argument(
        "--include-local-archives",
        action="store_true",
        help="Also cache verified archives that still have a full local copy.",
    )
    args = parser.parse_args()

    selected = archives_requiring_cache(
        args.backup_root.expanduser().resolve(),
        args.backup_prefix,
        args.state_directory_name,
        args.cache_directory_name,
        args.remote_root,
        args.include_local_archives,
    )
    print(
        json.dumps(
            {
                "count": len(selected),
                "archive_names": selected,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
