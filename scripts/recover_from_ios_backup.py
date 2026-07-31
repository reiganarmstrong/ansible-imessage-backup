#!/usr/bin/env python3
"""Recover missing macOS Messages attachments from an Apple device backup.

The utility correlates attachment records through the message GUID and transfer
name stored in both Messages databases. It then resolves the iOS attachment
through Manifest.db, verifies the physical backup file, copies it into the
export, and merges a SHA-256-backed entry into the source recovery map.

Exact recoveries must also match the Mac attachment byte count. Device variants
are the same logical attachment in the same message but have different bytes;
they are only included when explicitly requested.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path


SMS_DATABASE_PATH = "Library/SMS/sms.db"


@dataclass(frozen=True)
class MacAttachment:
    row_id: int
    message_guid: str
    transfer_name: str
    total_bytes: int
    is_sticker: bool


@dataclass(frozen=True)
class IosAttachment:
    row_id: int
    message_guid: str
    transfer_name: str
    total_bytes: int
    filename: str
    file_id: str
    domain: str
    relative_path: str
    physical_path: Path
    evidence_kind: str
    hide_attachment: bool


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def backup_file(backup_path: Path, file_id: str) -> Path:
    return backup_path / file_id[:2] / file_id


def normalize_ios_path(filename: str) -> str:
    if filename.startswith("~/"):
        return filename[2:]
    prefix = "/var/mobile/"
    if filename.startswith(prefix):
        return filename[len(prefix) :]
    return filename.lstrip("/")


def safe_name(row_id: int, transfer_name: str) -> str:
    name = re.sub(
        r"[^A-Za-z0-9._ -]+",
        "_",
        Path(transfer_name).name,
    ).strip(" .")
    return f"row-{row_id}_{name or 'attachment'}"


def locate_sms_database(
    backup_path: Path,
    manifest: sqlite3.Connection,
) -> Path:
    row = manifest.execute(
        """
        SELECT fileID
        FROM Files
        WHERE relativePath = ?
        ORDER BY CASE domain WHEN 'HomeDomain' THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (SMS_DATABASE_PATH,),
    ).fetchone()
    if row is None:
        raise FileNotFoundError(
            f"{SMS_DATABASE_PATH} is not listed in {backup_path / 'Manifest.db'}"
        )
    database = backup_file(backup_path, row["fileID"])
    if not database.is_file():
        raise FileNotFoundError(f"Backed-up sms.db is missing: {database}")
    return database


def unresolved_mac_attachments(
    manifest_path: Path,
    mac_database: sqlite3.Connection,
) -> list[MacAttachment]:
    manifest = read_json(manifest_path)
    if not isinstance(manifest, list):
        raise ValueError("Attachment manifest must be a JSON array")
    unresolved = {
        row["row_id"]
        for row in manifest
        if (
            isinstance(row, dict)
            and isinstance(row.get("row_id"), int)
            and not row.get("source_present")
            and not row.get("separately_recovered")
            and not row.get("hidden_internal_payload")
        )
    }
    if not unresolved:
        return []

    placeholders = ",".join("?" for _ in unresolved)
    rows = mac_database.execute(
        f"""
        SELECT DISTINCT a.ROWID, m.guid, a.transfer_name,
               COALESCE(a.total_bytes, 0) AS total_bytes,
               COALESCE(a.is_sticker, 0) AS is_sticker
        FROM attachment a
        JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
        JOIN message m ON m.ROWID = maj.message_id
        WHERE a.ROWID IN ({placeholders})
          AND COALESCE(a.transfer_name, '') <> ''
        ORDER BY a.ROWID, m.ROWID
        """,
        sorted(unresolved),
    )
    return [
        MacAttachment(
            row_id=row["ROWID"],
            message_guid=row["guid"],
            transfer_name=row["transfer_name"],
            total_bytes=row["total_bytes"],
            is_sticker=bool(row["is_sticker"]),
        )
        for row in rows
    ]


def backed_ios_attachments(
    backup_path: Path,
    manifest: sqlite3.Connection,
    ios_database: sqlite3.Connection,
    message_guids: set[str],
) -> list[IosAttachment]:
    if not message_guids:
        return []
    placeholders = ",".join("?" for _ in message_guids)
    rows = ios_database.execute(
        f"""
        SELECT DISTINCT a.ROWID, m.guid, a.transfer_name,
               COALESCE(a.total_bytes, 0) AS total_bytes, a.filename,
               COALESCE(a.hide_attachment, 0) AS hide_attachment
        FROM message m
        JOIN message_attachment_join maj ON maj.message_id = m.ROWID
        JOIN attachment a ON a.ROWID = maj.attachment_id
        WHERE upper(m.guid) IN ({placeholders})
          AND COALESCE(a.transfer_name, '') <> ''
          AND COALESCE(a.filename, '') <> ''
        ORDER BY a.ROWID
        """,
        sorted(guid.upper() for guid in message_guids),
    )
    result: list[IosAttachment] = []
    for row in rows:
        relative_path = normalize_ios_path(row["filename"])
        backup_row = manifest.execute(
            """
            SELECT fileID, domain, relativePath
            FROM Files
            WHERE relativePath = ?
            ORDER BY CASE domain WHEN 'MediaDomain' THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (relative_path,),
        ).fetchone()
        if backup_row is None:
            continue
        physical = backup_file(backup_path, backup_row["fileID"])
        if not physical.is_file():
            continue
        result.append(
            IosAttachment(
                row_id=row["ROWID"],
                message_guid=row["guid"],
                transfer_name=row["transfer_name"],
                total_bytes=row["total_bytes"],
                filename=row["filename"],
                file_id=backup_row["fileID"],
                domain=backup_row["domain"],
                relative_path=backup_row["relativePath"],
                physical_path=physical,
                evidence_kind="message_guid_and_transfer_name",
                hide_attachment=bool(row["hide_attachment"]),
            )
        )
    return result


def backed_sticker_cache_candidates(
    backup_path: Path,
    manifest: sqlite3.Connection,
    mac_attachments: list[MacAttachment],
) -> list[IosAttachment]:
    """Find device sticker-cache files carrying a missing sticker's unique name."""
    sticker_names = {
        attachment.transfer_name.casefold(): attachment
        for attachment in mac_attachments
        if attachment.is_sticker
    }
    if not sticker_names:
        return []
    rows = manifest.execute(
        """
        SELECT fileID, domain, relativePath
        FROM Files
        WHERE relativePath LIKE 'Library/SMS/StickerCache/%'
        ORDER BY relativePath
        """
    )
    result: list[IosAttachment] = []
    for row in rows:
        matched = sticker_names.get(Path(row["relativePath"]).name.casefold())
        if matched is None:
            continue
        physical = backup_file(backup_path, row["fileID"])
        if not physical.is_file() or physical.stat().st_size == 0:
            continue
        result.append(
            IosAttachment(
                row_id=-1,
                message_guid=matched.message_guid,
                transfer_name=matched.transfer_name,
                total_bytes=physical.stat().st_size,
                filename=row["relativePath"],
                file_id=row["fileID"],
                domain=row["domain"],
                relative_path=row["relativePath"],
                physical_path=physical,
                evidence_kind="sticker_cache_transfer_name",
                hide_attachment=False,
            )
        )
    return result


def unresolved_hidden_message_rows(
    manifest_path: Path,
    mac_database: sqlite3.Connection,
) -> dict[str, list[int]]:
    manifest = read_json(manifest_path)
    if not isinstance(manifest, list):
        raise ValueError("Attachment manifest must be a JSON array")
    hidden_rows = {
        row["row_id"]
        for row in manifest
        if (
            isinstance(row, dict)
            and isinstance(row.get("row_id"), int)
            and not row.get("source_present")
            and not row.get("separately_recovered")
            and row.get("hidden_internal_payload")
        )
    }
    if not hidden_rows:
        return {}
    placeholders = ",".join("?" for _ in hidden_rows)
    result: dict[str, list[int]] = {}
    for row in mac_database.execute(
        f"""
        SELECT DISTINCT m.guid, maj.attachment_id
        FROM message_attachment_join maj
        JOIN message m ON m.ROWID = maj.message_id
        WHERE maj.attachment_id IN ({placeholders})
        ORDER BY m.guid, maj.attachment_id
        """,
        sorted(hidden_rows),
    ):
        result.setdefault(row["guid"], []).append(row["attachment_id"])
    return result


def correlate(
    mac_attachments: list[MacAttachment],
    ios_attachments: list[IosAttachment],
    include_device_variants: bool,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    by_identity: dict[tuple[str, str], list[IosAttachment]] = {}
    for attachment in ios_attachments:
        key = (
            attachment.message_guid.casefold(),
            attachment.transfer_name.casefold(),
        )
        by_identity.setdefault(key, []).append(attachment)

    selected: list[dict[str, object]] = []
    ambiguous: list[dict[str, object]] = []
    seen_rows: set[int] = set()
    for mac in mac_attachments:
        if mac.row_id in seen_rows:
            continue
        key = (mac.message_guid.casefold(), mac.transfer_name.casefold())
        candidates = by_identity.get(key, [])
        viable = [
            candidate
            for candidate in candidates
            if candidate.physical_path.stat().st_size > 0
        ]
        exact = [
            candidate
            for candidate in viable
            if candidate.physical_path.stat().st_size == mac.total_bytes
        ]
        if exact:
            pool = exact
        elif include_device_variants and viable:
            closest_difference = min(
                abs(candidate.physical_path.stat().st_size - mac.total_bytes)
                for candidate in viable
            )
            pool = [
                candidate
                for candidate in viable
                if (
                    abs(
                        candidate.physical_path.stat().st_size
                        - mac.total_bytes
                    )
                    == closest_difference
                )
            ]
        else:
            pool = []
        unique_files = {candidate.file_id: candidate for candidate in pool}
        if len(unique_files) != 1:
            if len(unique_files) > 1:
                ambiguous.append(
                    {
                        "attachment_row_id": mac.row_id,
                        "message_guid": mac.message_guid,
                        "transfer_name": mac.transfer_name,
                        "candidate_file_ids": sorted(unique_files),
                    }
                )
            continue
        ios = next(iter(unique_files.values()))
        selected.append(
            {
                "attachment_row_id": mac.row_id,
                "message_guid": mac.message_guid,
                "transfer_name": mac.transfer_name,
                "mac_total_bytes": mac.total_bytes,
                "ios_attachment_row_id": ios.row_id,
                "ios_total_bytes": ios.total_bytes,
                "ios_physical_bytes": ios.physical_path.stat().st_size,
                "ios_filename": ios.filename,
                "ios_file_id": ios.file_id,
                "ios_domain": ios.domain,
                "ios_relative_path": ios.relative_path,
                "evidence_kind": ios.evidence_kind,
                "physical_path": str(ios.physical_path),
                "recovery_kind": (
                    "ios_backup_byte_exact"
                    if ios.physical_path.stat().st_size == mac.total_bytes
                    else (
                        "ios_backup_sticker_cache_variant"
                        if ios.evidence_kind == "sticker_cache_transfer_name"
                        else "ios_backup_device_variant"
                    )
                ),
            }
        )
        seen_rows.add(mac.row_id)
    return selected, ambiguous


def load_source_map(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"recoveries": []}
    value = read_json(path)
    if not isinstance(value, dict) or not isinstance(value.get("recoveries"), list):
        raise ValueError("Recovery source map must contain a 'recoveries' list")
    return value


def apply_recoveries(
    export_path: Path,
    destination: Path,
    source_map_path: Path,
    matches: list[dict[str, object]],
) -> list[dict[str, object]]:
    destination.mkdir(parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    source_map = load_source_map(source_map_path)
    recoveries = source_map["recoveries"]
    assert isinstance(recoveries, list)
    existing_rows: dict[int, dict[str, object]] = {}
    for recovery in recoveries:
        if not isinstance(recovery, dict):
            continue
        for row_id in recovery.get("attachment_row_ids", []):
            existing_rows[row_id] = recovery

    applied: list[dict[str, object]] = []
    for match in matches:
        row_id = int(match["attachment_row_id"])
        source = Path(str(match["physical_path"]))
        digest = sha256(source)
        existing = existing_rows.get(row_id)
        if existing is not None:
            if existing.get("sha256") != digest:
                raise ValueError(
                    f"Attachment row {row_id} already maps to different bytes"
                )
            continue

        target = destination / safe_name(row_id, str(match["transfer_name"]))
        if target.exists() and sha256(target) != digest:
            raise FileExistsError(f"Refusing to replace different bytes: {target}")
        if not target.exists():
            shutil.copy2(source, target)
            os.chmod(target, 0o600)
        relative = str(target.relative_to(export_path))
        recovery = {
            "attachment_row_ids": [row_id],
            "archive_path": relative,
            "sha256": digest,
            "recovery_kind": match["recovery_kind"],
            "evidence": {
                "message_guid": match["message_guid"],
                "transfer_name": match["transfer_name"],
                "mac_total_bytes": match["mac_total_bytes"],
                "ios_attachment_row_id": match["ios_attachment_row_id"],
                "ios_total_bytes": match["ios_total_bytes"],
                "ios_physical_bytes": match["ios_physical_bytes"],
                "ios_file_id": match["ios_file_id"],
                "ios_domain": match["ios_domain"],
                "ios_relative_path": match["ios_relative_path"],
                "evidence_kind": match["evidence_kind"],
            },
        }
        recoveries.append(recovery)
        existing_rows[row_id] = recovery
        applied.append(recovery)

    recoveries.sort(
        key=lambda recovery: min(recovery.get("attachment_row_ids", [sys.maxsize]))
    )
    write_json(source_map_path, source_map)
    return applied


def apply_hidden_counterparts(
    export_path: Path,
    destination: Path,
    attachments: list[IosAttachment],
    mac_rows_by_message: dict[str, list[int]],
) -> list[dict[str, object]]:
    counterpart_path = destination / "Hidden Counterparts"
    counterpart_path.mkdir(parents=True, exist_ok=True)
    os.chmod(counterpart_path, 0o700)
    applied: list[dict[str, object]] = []
    seen_file_ids: set[str] = set()
    for attachment in attachments:
        if (
            not attachment.hide_attachment
            or attachment.file_id in seen_file_ids
        ):
            continue
        mac_rows = mac_rows_by_message.get(attachment.message_guid, [])
        if not mac_rows:
            continue
        seen_file_ids.add(attachment.file_id)
        name = safe_name(
            attachment.row_id,
            attachment.transfer_name,
        )
        target = counterpart_path / (
            f"message-{attachment.message_guid}_{name}"
        )
        digest = sha256(attachment.physical_path)
        if target.exists() and sha256(target) != digest:
            raise FileExistsError(f"Refusing to replace different bytes: {target}")
        if not target.exists():
            shutil.copy2(attachment.physical_path, target)
            os.chmod(target, 0o600)
        applied.append(
            {
                "archive_path": str(target.relative_to(export_path)),
                "sha256": digest,
                "source_bytes": target.stat().st_size,
                "message_guid": attachment.message_guid,
                "mac_unavailable_attachment_row_ids": mac_rows,
                "ios_attachment_row_id": attachment.row_id,
                "ios_file_id": attachment.file_id,
                "ios_domain": attachment.domain,
                "ios_relative_path": attachment.relative_path,
                "recovery_kind": "ios_hidden_message_counterpart",
            }
        )
    write_json(
        counterpart_path / "hidden-counterparts.json",
        {
            "counterparts": applied,
            "counterpart_count": len(applied),
            "messages_covered": len(
                {counterpart["message_guid"] for counterpart in applied}
            ),
            "note": (
                "These are device-specific hidden rich-preview payloads from "
                "the same messages, not byte-identical Mac attachment rows."
            ),
        },
    )
    return applied


def recover(
    export_path: Path,
    backup_path: Path,
    destination: Path,
    source_map_path: Path,
    include_device_variants: bool,
    preserve_hidden_counterparts: bool,
    apply: bool,
) -> dict[str, object]:
    preservation = export_path / "Source Preservation"
    attachment_manifest = preservation / "attachment-manifest.json"
    mac_database_path = preservation / "chat.db"
    manifest_database_path = backup_path / "Manifest.db"
    for required in (
        attachment_manifest,
        mac_database_path,
        manifest_database_path,
    ):
        if not required.is_file():
            raise FileNotFoundError(required)

    manifest = sqlite3.connect(
        f"file:{manifest_database_path}?mode=ro&immutable=1",
        uri=True,
    )
    manifest.row_factory = sqlite3.Row
    sms_database_path = locate_sms_database(backup_path, manifest)
    mac_database = sqlite3.connect(
        f"file:{mac_database_path}?mode=ro&immutable=1",
        uri=True,
    )
    mac_database.row_factory = sqlite3.Row
    ios_database = sqlite3.connect(
        f"file:{sms_database_path}?mode=ro&immutable=1",
        uri=True,
    )
    ios_database.row_factory = sqlite3.Row
    try:
        mac_attachments = unresolved_mac_attachments(
            attachment_manifest,
            mac_database,
        )
        ios_attachments = backed_ios_attachments(
            backup_path,
            manifest,
            ios_database,
            {attachment.message_guid for attachment in mac_attachments},
        )
        ios_attachments.extend(
            backed_sticker_cache_candidates(
                backup_path,
                manifest,
                mac_attachments,
            )
        )
        matches, ambiguous = correlate(
            mac_attachments,
            ios_attachments,
            include_device_variants,
        )
        hidden_rows_by_message = unresolved_hidden_message_rows(
            attachment_manifest,
            mac_database,
        )
        hidden_ios_attachments = (
            backed_ios_attachments(
                backup_path,
                manifest,
                ios_database,
                set(hidden_rows_by_message),
            )
            if preserve_hidden_counterparts
            else []
        )
    finally:
        ios_database.close()
        mac_database.close()
        manifest.close()

    applied = (
        apply_recoveries(
            export_path,
            destination,
            source_map_path,
            matches,
        )
        if apply
        else []
    )
    hidden_counterparts = (
        apply_hidden_counterparts(
            export_path,
            destination,
            hidden_ios_attachments,
            hidden_rows_by_message,
        )
        if apply and preserve_hidden_counterparts
        else []
    )
    hidden_candidates = [
        attachment
        for attachment in hidden_ios_attachments
        if (
            attachment.hide_attachment
            and attachment.message_guid in hidden_rows_by_message
        )
    ]
    source_map_value = load_source_map(source_map_path)
    all_ios_recoveries = [
        recovery
        for recovery in source_map_value["recoveries"]
        if (
            isinstance(recovery, dict)
            and str(recovery.get("recovery_kind", "")).startswith("ios_backup_")
        )
    ]
    report = {
        "applied": apply,
        "backup_path": str(backup_path),
        "export_path": str(export_path),
        "include_device_variants": include_device_variants,
        "preserve_hidden_counterparts": preserve_hidden_counterparts,
        "candidate_count": len(matches),
        "byte_exact_candidate_count": sum(
            match["recovery_kind"] == "ios_backup_byte_exact"
            for match in matches
        ),
        "device_variant_candidate_count": sum(
            match["recovery_kind"] == "ios_backup_device_variant"
            for match in matches
        ),
        "ambiguous_count": len(ambiguous),
        "applied_count": len(applied),
        "hidden_counterpart_candidate_count": len(
            {attachment.file_id for attachment in hidden_candidates}
        ),
        "hidden_counterpart_applied_count": len(hidden_counterparts),
        "hidden_counterpart_messages_covered": len(
            {attachment.message_guid for attachment in hidden_candidates}
        ),
        "total_mapped_ios_recovery_rows": sum(
            len(recovery.get("attachment_row_ids", []))
            for recovery in all_ios_recoveries
        ),
        "all_mapped_ios_recoveries": all_ios_recoveries,
        "matches": matches,
        "ambiguous": ambiguous,
        "source_map": str(source_map_path),
    }
    if apply:
        write_json(export_path / "ios-backup-recovery.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_path", type=Path)
    parser.add_argument("ios_backup_path", type=Path)
    parser.add_argument(
        "--destination-directory",
        default="Recovered Attachments/iOS Backup",
        help="Archive-relative directory for recovered files.",
    )
    parser.add_argument(
        "--source-map",
        type=Path,
        help=(
            "Recovery map to merge. Defaults to "
            "Recovered Attachments/recovery-source-map.json in the export."
        ),
    )
    parser.add_argument(
        "--include-device-variants",
        action="store_true",
        help="Include same-message/name assets whose bytes differ by device.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Copy candidates and update the recovery map.",
    )
    parser.add_argument(
        "--preserve-hidden-counterparts",
        action="store_true",
        help=(
            "Copy device-specific hidden rich-preview payloads belonging to "
            "messages whose Mac payload rows are unavailable."
        ),
    )
    args = parser.parse_args()

    export_path = args.export_path.expanduser().resolve()
    backup_path = args.ios_backup_path.expanduser().resolve()
    destination = (export_path / args.destination_directory).resolve()
    try:
        destination.relative_to(export_path)
    except ValueError:
        parser.error("Destination directory must remain inside the export")
    source_map = (
        args.source_map.expanduser().resolve()
        if args.source_map
        else export_path / "Recovered Attachments" / "recovery-source-map.json"
    )

    report = recover(
        export_path,
        backup_path,
        destination,
        source_map,
        args.include_device_variants,
        args.preserve_hidden_counterparts,
        args.apply,
    )
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
