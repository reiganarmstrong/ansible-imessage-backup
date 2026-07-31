#!/usr/bin/env python3
"""Independently verify the Messages source-preservation layer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path


class DigestCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def sha256(self, path: Path) -> str:
        key = str(path)
        if key in self.values:
            return self.values[key]
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        value = digest.hexdigest()
        self.values[key] = value
        return value


def verify(
    export_path: Path,
    verify_live_source: bool,
) -> tuple[dict[str, object], bool]:
    report_path = export_path / "source-preservation.json"
    if not report_path.is_file():
        return {"passed": False, "error": "source-preservation.json is missing"}, False

    preservation_report = json.loads(report_path.read_text(encoding="utf-8"))
    directory_name = preservation_report.get("preservation_directory")
    if not isinstance(directory_name, str) or not directory_name:
        return {"passed": False, "error": "preservation directory is invalid"}, False

    preservation_path = export_path / directory_name
    manifest_path = preservation_path / "attachment-manifest.json"
    snapshot_path = preservation_path / "chat.db"
    if not manifest_path.is_file() or not snapshot_path.is_file():
        return {
            "passed": False,
            "error": "attachment manifest or database snapshot is missing",
        }, False

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        return {"passed": False, "error": "attachment manifest is not a list"}, False

    digest_cache = DigestCache()
    missing_archive_files: list[str] = []
    archive_digest_mismatches: list[str] = []
    live_source_missing: list[str] = []
    live_source_digest_mismatches: list[str] = []
    readable_rows = 0
    separately_recovered_rows = 0
    preserved_rows = 0
    preserved_readable_rows = 0

    for row in manifest:
        row_id = row.get("row_id")
        source_present = bool(row.get("source_present"))
        separately_recovered = bool(row.get("separately_recovered"))
        if not source_present and not separately_recovered:
            continue
        if source_present:
            readable_rows += 1
        if separately_recovered:
            separately_recovered_rows += 1
        relative_archive = row.get("archive_path")
        expected_digest = row.get("sha256")
        if not isinstance(relative_archive, str) or not isinstance(
            expected_digest, str
        ):
            missing_archive_files.append(str(row_id))
            continue
        archived = export_path / relative_archive
        if not archived.is_file() or archived.is_symlink():
            missing_archive_files.append(f"{row_id}: {relative_archive}")
            continue
        if digest_cache.sha256(archived) != expected_digest:
            archive_digest_mismatches.append(f"{row_id}: {relative_archive}")
            continue
        preserved_rows += 1
        if source_present:
            preserved_readable_rows += 1

        if verify_live_source and source_present:
            source_name = row.get("source_filename")
            source = (
                Path(os.path.expanduser(source_name))
                if isinstance(source_name, str) and source_name
                else None
            )
            if source is None or not source.is_file():
                live_source_missing.append(str(row_id))
            elif digest_cache.sha256(source) != expected_digest:
                live_source_digest_mismatches.append(
                    f"{row_id}: {source_name}"
                )

    hidden_manifest_path = (
        export_path
        / "Recovered Attachments"
        / "iOS Backup"
        / "Hidden Counterparts"
        / "hidden-counterparts.json"
    )
    hidden_counterpart_count = 0
    hidden_counterpart_missing_files: list[str] = []
    hidden_counterpart_digest_mismatches: list[str] = []
    if hidden_manifest_path.is_file():
        hidden_manifest = json.loads(
            hidden_manifest_path.read_text(encoding="utf-8")
        )
        counterparts = hidden_manifest.get("counterparts", [])
        if not isinstance(counterparts, list):
            hidden_counterpart_missing_files.append(
                "hidden-counterparts.json is invalid"
            )
            counterparts = []
        hidden_counterpart_count = len(counterparts)
        resolved_export = export_path.resolve()
        for counterpart in counterparts:
            relative_path = counterpart.get("archive_path")
            expected_digest = counterpart.get("sha256")
            if not isinstance(relative_path, str) or not isinstance(
                expected_digest,
                str,
            ):
                hidden_counterpart_missing_files.append(str(relative_path))
                continue
            candidate = (resolved_export / relative_path).resolve()
            try:
                candidate.relative_to(resolved_export)
            except ValueError:
                hidden_counterpart_missing_files.append(relative_path)
                continue
            if not candidate.is_file() or candidate.is_symlink():
                hidden_counterpart_missing_files.append(relative_path)
            elif digest_cache.sha256(candidate) != expected_digest:
                hidden_counterpart_digest_mismatches.append(relative_path)

    connection = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        snapshot_attachment_rows = connection.execute(
            "SELECT count(*) FROM attachment"
        ).fetchone()[0]
        snapshot_message_rows = connection.execute(
            "SELECT count(*) FROM message"
        ).fetchone()[0]
    finally:
        connection.close()

    summary = preservation_report.get("summary", {})
    summary_readable = (
        summary.get("locally_readable_rows") if isinstance(summary, dict) else None
    )
    summary_unpreserved = (
        summary.get("locally_readable_rows_unpreserved")
        if isinstance(summary, dict)
        else None
    )
    summary_recovered = (
        summary.get("separately_recovered_rows")
        if isinstance(summary, dict)
        else None
    )
    checks = {
        "preservation_report_passed": preservation_report.get("passed") is True,
        "manifest_rows_match_snapshot": len(manifest) == snapshot_attachment_rows,
        "manifest_readable_rows_match_report": readable_rows == summary_readable,
        "manifest_recovered_rows_match_report": (
            separately_recovered_rows == summary_recovered
        ),
        "report_has_zero_readable_rows_unpreserved": summary_unpreserved == 0,
        "all_readable_or_recovered_archive_files_verified": (
            preserved_rows == readable_rows + separately_recovered_rows
            and not missing_archive_files
            and not archive_digest_mismatches
        ),
        "snapshot_quick_check_ok": quick_check == "ok",
        "live_sources_still_match": (
            not verify_live_source
            or (not live_source_missing and not live_source_digest_mismatches)
        ),
        "hidden_counterparts_verified": (
            not hidden_counterpart_missing_files
            and not hidden_counterpart_digest_mismatches
        ),
    }
    passed = all(checks.values())
    report: dict[str, object] = {
        "export_path": str(export_path),
        "verify_live_source": verify_live_source,
        "attachment_manifest_rows": len(manifest),
        "locally_readable_rows": readable_rows,
        "locally_readable_rows_with_verified_archive_bytes": preserved_readable_rows,
        "separately_recovered_rows": separately_recovered_rows,
        "readable_or_recovered_rows_with_verified_archive_bytes": preserved_rows,
        "snapshot_attachment_rows": snapshot_attachment_rows,
        "snapshot_message_rows": snapshot_message_rows,
        "snapshot_quick_check": quick_check,
        "missing_archive_file_count": len(missing_archive_files),
        "missing_archive_files": missing_archive_files,
        "archive_digest_mismatch_count": len(archive_digest_mismatches),
        "archive_digest_mismatches": archive_digest_mismatches,
        "live_source_missing_count": len(live_source_missing),
        "live_source_missing_rows": live_source_missing,
        "live_source_digest_mismatch_count": len(live_source_digest_mismatches),
        "live_source_digest_mismatches": live_source_digest_mismatches,
        "hidden_counterpart_count": hidden_counterpart_count,
        "hidden_counterpart_missing_file_count": len(
            hidden_counterpart_missing_files
        ),
        "hidden_counterpart_missing_files": hidden_counterpart_missing_files,
        "hidden_counterpart_digest_mismatch_count": len(
            hidden_counterpart_digest_mismatches
        ),
        "hidden_counterpart_digest_mismatches": (
            hidden_counterpart_digest_mismatches
        ),
        "checks": checks,
        "passed": passed,
    }
    return report, passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_path", type=Path)
    parser.add_argument(
        "--verify-live-source",
        action="store_true",
        help="Also re-hash every source file that remains available.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        help="Optionally save the JSON report to this path.",
    )
    args = parser.parse_args()
    export_path = args.export_path.expanduser().resolve()
    if not export_path.is_dir():
        parser.error(f"{export_path} is not a directory")

    report, passed = verify(export_path, args.verify_live_source)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report_path:
        report_destination = args.report_path.expanduser().resolve()
        report_destination.write_text(rendered, encoding="utf-8")
        os.chmod(report_destination, 0o600)
    sys.stdout.write(rendered)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
