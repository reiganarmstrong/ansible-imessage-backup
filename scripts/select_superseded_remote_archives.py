#!/usr/bin/env python3
"""Select older verified archives whose useful content exists later."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sqlite3


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


def read_message_guids(database_path: Path) -> set[str] | None:
    try:
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as db:
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                return None
            return {
                row[0]
                for row in db.execute(
                    "SELECT guid FROM message WHERE guid IS NOT NULL"
                )
                if isinstance(row[0], str) and row[0]
            }
    except (sqlite3.Error, OSError):
        return None


def read_attachment_digests(manifest_path: Path) -> set[tuple[str, str]] | None:
    manifest = load_json(manifest_path)
    if not isinstance(manifest, list):
        return None
    result: set[tuple[str, str]] = set()
    for row in manifest:
        if not isinstance(row, dict):
            return None
        attachment_guid = row.get("attachment_guid")
        digest = row.get("sha256")
        if digest is None:
            continue
        if not isinstance(attachment_guid, str) or not isinstance(digest, str):
            return None
        result.add((attachment_guid, digest))
    return result


def select_superseded(
    backup_root: Path,
    backup_prefix: str,
    state_directory_name: str,
    cache_directory_name: str,
    remote_root: str,
) -> dict[str, object]:
    state_root = backup_root / state_directory_name
    marker_root = state_root / "remote-verifications"
    cache_root = state_root / cache_directory_name
    archive_pattern = re.compile(
        rf"^{re.escape(backup_prefix)}-\d{{8}}T\d{{6}}$"
    )
    archives: list[dict[str, object]] = []
    ignored_reasons: Counter[str] = Counter()

    for marker_path in sorted(marker_root.glob("*.json")):
        archive_name = marker_path.stem
        if not archive_pattern.fullmatch(archive_name):
            ignored_reasons["invalid_archive_name"] += 1
            continue
        remote_marker = load_json(marker_path)
        if not valid_remote_marker(remote_marker, archive_name, remote_root):
            ignored_reasons["invalid_remote_marker"] += 1
            continue
        assert isinstance(remote_marker, dict)
        cache_path = cache_root / archive_name
        cache_marker = load_json(
            cache_path / ".catalog-cache-verification.json"
        )
        if not valid_cache_marker(cache_marker, remote_marker, archive_name):
            ignored_reasons["invalid_catalog_cache_marker"] += 1
            continue
        message_guids = read_message_guids(
            cache_path / "Source Preservation" / "chat.db"
        )
        attachment_digests = read_attachment_digests(
            cache_path / "Source Preservation" / "attachment-manifest.json"
        )
        if message_guids is None:
            ignored_reasons["invalid_catalog_database"] += 1
            continue
        if attachment_digests is None:
            ignored_reasons["invalid_attachment_manifest"] += 1
            continue
        archives.append(
            {
                "name": archive_name,
                "messages": message_guids,
                "attachments": attachment_digests,
            }
        )

    archives.sort(key=lambda archive: str(archive["name"]))
    candidates: list[str] = []
    blocked: list[dict[str, object]] = []
    for index, archive in enumerate(archives):
        archive_name = str(archive["name"])
        if index == len(archives) - 1:
            blocked.append(
                {"archive_name": archive_name, "reasons": ["newest_archive_retained"]}
            )
            continue
        later_messages: set[str] = set()
        later_attachments: set[tuple[str, str]] = set()
        for later in archives[index + 1 :]:
            later_messages.update(later["messages"])
            later_attachments.update(later["attachments"])
        missing_messages = archive["messages"] - later_messages
        missing_attachments = archive["attachments"] - later_attachments
        reasons: list[str] = []
        if missing_messages:
            reasons.append("contains_unique_messages")
        if missing_attachments:
            reasons.append("contains_unique_preserved_attachments")
        if reasons:
            blocked.append(
                {
                    "archive_name": archive_name,
                    "reasons": reasons,
                    "unique_message_count": len(missing_messages),
                    "unique_preserved_attachment_count": len(missing_attachments),
                }
            )
        else:
            candidates.append(archive_name)

    return {
        "schema_version": 1,
        "automatic_deletion_performed": False,
        "verified_archive_count": len(archives),
        "candidate_count": len(candidates),
        "archive_names": candidates,
        "blocked_archives": blocked,
        "ignored_reasons": dict(sorted(ignored_reasons.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup_root", type=Path)
    parser.add_argument("--backup-prefix", default="imessage-backup")
    parser.add_argument(
        "--state-directory-name", default=".imessage-archive-state"
    )
    parser.add_argument("--cache-directory-name", default="catalog-cache")
    parser.add_argument("--remote-root", required=True)
    args = parser.parse_args()
    report = select_superseded(
        args.backup_root.expanduser().resolve(),
        args.backup_prefix,
        args.state_directory_name,
        args.cache_directory_name,
        args.remote_root,
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
