#!/usr/bin/env python3
"""Select full local archives safe to retire after verified publication."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from select_remote_catalog_archives import (
    load_json,
    valid_cache_marker,
    valid_remote_marker,
)


def has_restore_attestation(
    state_root: Path,
    remote_root: str,
) -> bool:
    expected_prefix = f"{remote_root.rstrip('/')}/"
    verification_root = state_root / "restore-verifications"
    for path in verification_root.glob("*.json"):
        report = load_json(path)
        if (
            isinstance(report, dict)
            and report.get("passed") is True
            and str(report.get("remote_source", "")).startswith(
                expected_prefix
            )
        ):
            return True
    return False


def select_retirable(
    backup_root: Path,
    backup_prefix: str,
    state_directory_name: str,
    cache_directory_name: str,
    remote_root: str,
    keep_count: int,
    require_restore_attestation: bool,
    protected_paths: list[Path] | None = None,
) -> dict[str, object]:
    protected_paths = [
        path.expanduser().resolve()
        for path in (protected_paths or [])
    ]
    resolved_backup_root = backup_root.resolve()
    state_root = backup_root / state_directory_name
    marker_root = state_root / "remote-verifications"
    cache_root = state_root / cache_directory_name
    restore_proven = has_restore_attestation(state_root, remote_root)
    candidates: list[Path] = []
    blocked = Counter()
    for archive_path in sorted(backup_root.glob(f"{backup_prefix}-*")):
        if not archive_path.is_dir():
            continue
        if archive_path.is_symlink():
            blocked["symlink_archive_refused"] += 1
            continue
        resolved_archive_path = archive_path.resolve()
        if resolved_archive_path.parent != resolved_backup_root:
            blocked["archive_outside_backup_root_refused"] += 1
            continue
        if any(
            protected_path == resolved_archive_path
            or protected_path.is_relative_to(resolved_archive_path)
            for protected_path in protected_paths
        ):
            blocked["contains_configured_recovery_input"] += 1
            continue
        verification = load_json(archive_path / "verification.json")
        if (
            not isinstance(verification, dict)
            or verification.get("passed") is not True
        ):
            blocked["local_verification_missing_or_failed"] += 1
            continue
        archive_name = archive_path.name
        remote_marker = load_json(marker_root / f"{archive_name}.json")
        if not valid_remote_marker(
            remote_marker,
            archive_name,
            remote_root,
        ):
            blocked["remote_verification_missing_or_invalid"] += 1
            continue
        assert isinstance(remote_marker, dict)
        cache_marker = load_json(
            cache_root
            / archive_name
            / ".catalog-cache-verification.json"
        )
        if not valid_cache_marker(
            cache_marker,
            remote_marker,
            archive_name,
        ):
            blocked["catalog_cache_missing_or_invalid"] += 1
            continue
        if require_restore_attestation and not restore_proven:
            blocked["successful_restore_test_required"] += 1
            continue
        candidates.append(resolved_archive_path)

    selected = candidates[:-keep_count] if keep_count else candidates
    return {
        "candidate_count": len(candidates),
        "count": len(selected),
        "archive_paths": [str(path) for path in selected],
        "kept_local_archive_count": len(candidates) - len(selected),
        "restore_attestation_present": restore_proven,
        "blocked_reasons": dict(sorted(blocked.items())),
    }


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
    parser.add_argument("--keep-count", type=int, default=0)
    parser.add_argument(
        "--require-restore-attestation",
        action="store_true",
    )
    parser.add_argument(
        "--protected-path",
        action="append",
        default=[],
        type=Path,
        help="Never retire an archive containing this configured input.",
    )
    args = parser.parse_args()
    if args.keep_count < 0:
        parser.error("--keep-count cannot be negative")

    report = select_retirable(
        args.backup_root.expanduser().resolve(),
        args.backup_prefix,
        args.state_directory_name,
        args.cache_directory_name,
        args.remote_root,
        args.keep_count,
        args.require_restore_attestation,
        args.protected_path,
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
