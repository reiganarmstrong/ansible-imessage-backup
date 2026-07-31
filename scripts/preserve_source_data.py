#!/usr/bin/env python3
"""Preserve and index every locally readable record from Messages.

imessage-exporter intentionally omits some database records and internal
attachments. This script adds a lossless preservation layer to an existing
export without modifying the live Messages database:

* creates a consistent SQLite snapshot;
* maps every attachment row to an identical archived file;
* copies locally readable bytes that the exporter omitted;
* reconnects omitted visible attachments to exported message HTML; and
* creates browser-readable indexes for omitted and unavailable records.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


GUID_PATTERN = re.compile(r"message-guid=([^&\"\s>]+)")
REFERENCE_PATTERN = re.compile(
    r"""(?:href|poster|src)=["']([^"']+)["']""", re.IGNORECASE
)
ERROR_PATTERN = re.compile(
    r'<span class="attachment_error">Unable to locate attachment: '
    r"(.*?)</span>"
)
PRESERVATION_MESSAGE_PATTERN = re.compile(
    r'\n<div class="message source-preservation-message">\n'
    r".*?\n  </div>\n</div>\n",
    re.DOTALL,
)
REPEATED_MEDIA_SUFFIXES = re.compile(
    r"(?i)(?:\.(?:avif|gif|heic|heif|jpe?g|mov|mp4|png|tiff?|webp))+$"
)
IGNORED_REFERENCE_SCHEMES = {
    "blob",
    "data",
    "http",
    "https",
    "javascript",
    "mailto",
    "sms",
    "tel",
}


@dataclass(frozen=True)
class MessageLink:
    """A Messages attachment-to-message relationship."""

    guid: str
    item_type: int
    date: int
    group_title: str
    chat_names: str


class DigestCache:
    """Calculate each file digest at most once."""

    def __init__(self) -> None:
        self._digests: dict[str, str] = {}

    def sha256(self, path: Path) -> str:
        key = str(path)
        cached = self._digests.get(key)
        if cached is not None:
            return cached

        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        result = digest.hexdigest()
        self._digests[key] = result
        return result


def snapshot_database(source: Path, destination: Path) -> None:
    """Create a transactionally consistent SQLite backup."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    if temporary.exists():
        temporary.unlink()

    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(destination_connection)
        destination_connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)


def resolve_source_path(filename: str | None) -> Path | None:
    """Resolve a Messages filename while retaining absent-path information."""
    if not filename:
        return None
    return Path(os.path.expanduser(filename))


def safe_filename(row_id: int, filename: str | None, transfer_name: str | None) -> str:
    """Build a private, deterministic archive filename."""
    selected = Path(transfer_name or filename or "attachment").name
    selected = re.sub(r"[^A-Za-z0-9._ -]+", "_", selected).strip(" .")
    if not selected:
        selected = "attachment"
    return f"{row_id}-{selected}"


def media_type(path: Path, mime_type: str | None) -> str | None:
    """Return a useful media type for browser markup."""
    if mime_type:
        return mime_type
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    try:
        header = path.read_bytes()[:16]
    except OSError:
        return None
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heic"
        return "video/mp4"
    return None


def attachment_markup(
    relative_path: str,
    display_name: str,
    detected_media_type: str | None,
    row_id: int,
) -> str:
    """Build conservative HTML for a source-preserved attachment."""
    encoded_path = html.escape(quote(relative_path, safe="/"), quote=True)
    escaped_name = html.escape(display_name)
    marker = f"source-preservation:{row_id}"
    if detected_media_type and detected_media_type.startswith("image/"):
        media = (
            f'<a href="{encoded_path}"><img src="{encoded_path}" '
            f'alt="{escaped_name}"></a>'
        )
    elif detected_media_type and detected_media_type.startswith("video/"):
        media = (
            '<video controls>'
            f'<source src="{encoded_path}" type="{detected_media_type}">'
            f'<source src="{encoded_path}"></video>'
        )
    elif detected_media_type and detected_media_type.startswith("audio/"):
        media = (
            '<audio controls>'
            f'<source src="{encoded_path}" type="{detected_media_type}">'
            f'<source src="{encoded_path}"></audio>'
        )
    else:
        media = f'<a href="{encoded_path}" download>{escaped_name}</a>'
    return (
        f"<!-- {marker} -->"
        '<div class="attachment source-preservation">'
        f"{media}</div>"
    )


def normalized_media_stem(filename: str) -> str:
    """Normalize names such as image.PNG.jpeg and image.jpeg to one stem."""
    return REPEATED_MEDIA_SUFFIXES.sub("", Path(filename).name).casefold()


def local_reference(html_file: Path, reference: str) -> Path | None:
    """Resolve a local archive reference."""
    parsed = urlsplit(reference)
    if parsed.scheme.lower() in IGNORED_REFERENCE_SCHEMES or parsed.netloc:
        return None
    if parsed.scheme or not parsed.path:
        return None
    candidate = Path(unquote(parsed.path))
    return candidate if candidate.is_absolute() else html_file.parent / candidate


class HtmlArchive:
    """Index and safely amend imessage-exporter conversation HTML."""

    def __init__(self, export_path: Path, digest_cache: DigestCache) -> None:
        self.export_path = export_path
        self.digest_cache = digest_cache
        self.documents: dict[Path, str] = {}
        self.guid_files: dict[str, Path] = {}
        self.message_reference_digests: dict[str, set[str]] = {}
        self.message_reference_paths: dict[str, list[Path]] = {}
        for html_file in sorted(export_path.glob("*.html")):
            document = html_file.read_text(encoding="utf-8", errors="replace")
            document = PRESERVATION_MESSAGE_PATTERN.sub("", document)
            self.documents[html_file] = document
            for match in GUID_PATTERN.finditer(document):
                self.guid_files[html.unescape(match.group(1))] = html_file

    @property
    def exported_guids(self) -> set[str]:
        return set(self.guid_files)

    @staticmethod
    def _message_bounds(document: str, guid: str) -> tuple[int, int, int] | None:
        marker = f"sms://open?message-guid={guid}"
        guid_position = document.find(marker)
        if guid_position < 0:
            return None
        message_start = document.rfind('<div class="message">', 0, guid_position)
        if message_start < 0:
            return None
        next_message = document.find('<div class="message">', guid_position)
        message_end = len(document) if next_message < 0 else next_message
        return message_start, guid_position, message_end

    def message_references_content(
        self,
        guid: str,
        source_digest: str,
        archived_source: Path,
    ) -> bool:
        """Check whether a message links to the bytes or a same-row preview."""
        cached = self.message_reference_digests.get(guid)
        if cached is not None:
            if source_digest in cached:
                return True
            return self._references_same_archive_row(guid, archived_source)
        html_file = self.guid_files.get(guid)
        if html_file is None:
            return False
        document = self.documents[html_file]
        bounds = self._message_bounds(document, guid)
        if bounds is None:
            return False
        message = document[bounds[0] : bounds[2]]
        digests: set[str] = set()
        reference_paths: list[Path] = []
        for reference in REFERENCE_PATTERN.findall(message):
            target = local_reference(html_file, html.unescape(reference))
            if target and target.is_file():
                reference_paths.append(target)
                digests.add(self.digest_cache.sha256(target))
                # HEIC/video preview generation may leave the HTML pointing at
                # a converted sibling while retaining the source file beside
                # it. Include those same-row siblings in the content check.
                for sibling in target.parent.glob(f"{target.stem}.*"):
                    if sibling.is_file() and sibling != target:
                        reference_paths.append(sibling)
                        digests.add(self.digest_cache.sha256(sibling))
        self.message_reference_digests[guid] = digests
        self.message_reference_paths[guid] = reference_paths
        return (
            source_digest in digests
            or self._references_same_archive_row(guid, archived_source)
        )

    def _references_same_archive_row(self, guid: str, archived_source: Path) -> bool:
        """Recognize converted previews that share an exported row basename."""
        try:
            relative = archived_source.relative_to(self.export_path)
        except ValueError:
            return False
        if not relative.parts:
            return False
        for reference in self.message_reference_paths.get(guid, []):
            if (
                reference.parent == archived_source.parent
                and reference.stem == archived_source.stem
            ):
                return True
        return False

    def note_message_content(self, guid: str, source_digest: str) -> None:
        """Record content added to a message during this run."""
        self.message_reference_digests.setdefault(guid, set()).add(source_digest)

    def connect_attachment(
        self,
        guid: str,
        row_id: int,
        archive_path: Path,
        display_name: str,
        detected_media_type: str | None,
    ) -> str:
        """Repair an attachment error or append an adjacent preserved asset."""
        html_file = self.guid_files.get(guid)
        if html_file is None:
            return "message_not_exported"
        document = self.documents[html_file]
        if f"source-preservation:{row_id}" in document:
            return "already_patched"

        bounds = self._message_bounds(document, guid)
        if bounds is None:
            return "message_container_not_found"
        message_start, _, message_end = bounds
        message = document[message_start:message_end]
        relative_path = os.path.relpath(archive_path, html_file.parent)
        markup = attachment_markup(
            relative_path,
            display_name,
            detected_media_type,
            row_id,
        )

        source_stem = normalized_media_stem(display_name)
        encoded_path = html.escape(quote(relative_path, safe="/"), quote=True)
        repaired_references = 0

        def repair_missing_reference(match: re.Match[str]) -> str:
            nonlocal repaired_references
            reference = html.unescape(match.group(1))
            target = local_reference(html_file, reference)
            reference_name = Path(
                unquote(urlsplit(reference).path)
            ).name
            if (
                target is None
                or target.exists()
                or normalized_media_stem(reference_name) != source_stem
            ):
                return match.group(0)
            repaired_references += 1
            return match.group(0).replace(match.group(1), encoded_path)

        repaired_message = REFERENCE_PATTERN.sub(
            repair_missing_reference,
            message,
        )
        if repaired_references:
            updated = (
                document[:message_start]
                + repaired_message
                + document[message_end:]
            )
            self.documents[html_file] = updated
            return "repaired_broken_reference"

        for error_match in ERROR_PATTERN.finditer(message):
            missing_name = html.unescape(re.sub("<.*?>", "", error_match.group(1)))
            if normalized_media_stem(missing_name) != source_stem:
                continue
            absolute_start = message_start + error_match.start()
            absolute_end = message_start + error_match.end()
            updated = document[:absolute_start] + markup + document[absolute_end:]
            self.documents[html_file] = updated
            return "repaired_error"

        supplemental = (
            "\n"
            '<div class="message source-preservation-message">\n'
            '  <div class="received">\n'
            "    <p><span class=\"sender\">Preserved source attachment for "
            "the preceding message</span></p>\n"
            f"    {markup}\n"
            "  </div>\n"
            "</div>\n"
        )
        insertion_position = message_end
        if message_end == len(document):
            body_end = document.lower().rfind("</body>")
            html_end = document.lower().rfind("</html>")
            if body_end > message_start:
                insertion_position = body_end
            elif html_end > message_start:
                insertion_position = html_end
        updated = (
            document[:insertion_position]
            + supplemental
            + document[insertion_position:]
        )
        self.documents[html_file] = updated
        return "appended_to_conversation"

    def save(self) -> int:
        """Write only changed in-memory documents."""
        changed = 0
        for path, document in self.documents.items():
            current = path.read_text(encoding="utf-8", errors="replace")
            if current == document:
                continue
            path.write_text(document, encoding="utf-8")
            changed += 1
        return changed


def build_archive_index(
    export_path: Path,
    preservation_path: Path,
) -> dict[int, list[Path]]:
    """Index candidate payload files by size for lazy content matching."""
    by_size: dict[int, list[Path]] = collections.defaultdict(list)
    ignored_names = {
        "attachment-manifest.json",
        "chat.db",
        "index.html",
        "messages-not-rendered.html",
        "source-preservation.json",
        "unavailable-attachments.html",
    }
    for path in export_path.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if path.parent == preservation_path and path.name in ignored_names:
            continue
        if path.suffix.lower() in {".html", ".json", ".md", ".txt", ".db"}:
            continue
        by_size[path.stat().st_size].append(path)
    return by_size


def relative_archive_path(export_path: Path, path: Path) -> str:
    return str(path.relative_to(export_path))


def find_identical_file(
    source: Path,
    source_digest: str,
    candidates_by_size: dict[int, list[Path]],
    digest_cache: DigestCache,
) -> Path | None:
    """Find identical bytes already stored in the archive."""
    for candidate in candidates_by_size.get(source.stat().st_size, []):
        if digest_cache.sha256(candidate) == source_digest:
            return candidate
    return None


def query_message_links(connection: sqlite3.Connection) -> dict[int, list[MessageLink]]:
    """Return every attachment-to-message relationship."""
    result: dict[int, list[MessageLink]] = collections.defaultdict(list)
    rows = connection.execute(
        """
        SELECT maj.attachment_id, m.guid, COALESCE(m.item_type, 0) AS item_type,
               COALESCE(m.date, 0) AS date,
               COALESCE(m.group_title, '') AS group_title,
               COALESCE(group_concat(DISTINCT
                   COALESCE(NULLIF(c.display_name, ''), c.chat_identifier)
               ), '') AS chat_names
        FROM message_attachment_join maj
        JOIN message m ON m.ROWID = maj.message_id
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
        GROUP BY maj.attachment_id, m.ROWID
        ORDER BY m.date, m.ROWID
        """
    )
    for row in rows:
        result[row["attachment_id"]].append(
            MessageLink(
                guid=row["guid"],
                item_type=row["item_type"],
                date=row["date"],
                group_title=row["group_title"],
                chat_names=row["chat_names"],
            )
        )
    return result


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def load_recovered_rows(
    export_path: Path,
    recovered_source_map: Path | None,
    digest_cache: DigestCache,
) -> dict[int, dict[str, object]]:
    """Validate and index separately recovered attachment rows."""
    if recovered_source_map is None:
        return {}
    value = json.loads(recovered_source_map.read_text(encoding="utf-8"))
    recoveries = value.get("recoveries")
    if not isinstance(recoveries, list):
        raise ValueError("Recovered source map must contain a 'recoveries' list")

    result: dict[int, dict[str, object]] = {}
    resolved_export_path = export_path.resolve()
    for recovery in recoveries:
        row_ids = recovery.get("attachment_row_ids")
        relative_path = recovery.get("archive_path")
        expected_digest = recovery.get("sha256")
        if (
            not isinstance(row_ids, list)
            or not row_ids
            or not all(isinstance(row_id, int) for row_id in row_ids)
            or not isinstance(relative_path, str)
            or not isinstance(expected_digest, str)
        ):
            raise ValueError("Recovered source map entry has invalid fields")
        archived = (resolved_export_path / relative_path).resolve()
        try:
            archived.relative_to(resolved_export_path)
        except ValueError as error:
            raise ValueError(
                f"Recovered archive path escapes the export: {relative_path}"
            ) from error
        if not archived.is_file() or archived.is_symlink():
            raise FileNotFoundError(f"Recovered file does not exist: {archived}")
        actual_digest = digest_cache.sha256(archived)
        if actual_digest != expected_digest:
            raise ValueError(f"Recovered file digest mismatch: {archived}")
        for row_id in row_ids:
            if row_id in result:
                raise ValueError(f"Duplicate recovered attachment row: {row_id}")
            result[row_id] = {
                "archive_path": relative_path,
                "path": archived,
                "sha256": actual_digest,
                "source_bytes": archived.stat().st_size,
                "recovery_kind": recovery.get(
                    "recovery_kind",
                    "separately_recovered",
                ),
            }
    return result


def html_page(title: str, body: str) -> str:
    """Create a small standalone private index page."""
    escaped_title = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escaped_title}</title>
<style>
body {{ font: 16px/1.45 -apple-system, BlinkMacSystemFont, sans-serif;
       margin: 2rem auto; max-width: 1100px; padding: 0 1rem; color: #1d1d1f; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border-bottom: 1px solid #ddd; padding: .45rem; text-align: left;
         vertical-align: top; overflow-wrap: anywhere; }}
code {{ font-size: .86em; }}
.warning {{ background: #fff4ce; padding: 1rem; border-radius: .5rem; }}
</style>
</head>
<body>
<h1>{escaped_title}</h1>
{body}
</body>
</html>
"""


def write_indexes(
    preservation_path: Path,
    report: dict[str, object],
    attachment_manifest: list[dict[str, object]],
    omitted_messages: list[dict[str, object]],
) -> None:
    """Write browser-readable preservation, omission, and failure indexes."""
    summary = report["summary"]
    index_body = f"""
<p>This supplemental layer preserves records and files that the HTML exporter
does not normally retain. It is part of the archive, not a replacement for the
conversation HTML files.</p>
<ul>
<li>Database attachment rows: {summary["attachment_rows"]}</li>
<li>Locally readable attachment rows: {summary["locally_readable_rows"]}</li>
<li>Locally readable rows preserved: {summary["locally_readable_rows_preserved"]}</li>
<li>Locally readable rows unpreserved: {summary["locally_readable_rows_unpreserved"]}</li>
<li>Rows with unavailable source paths: {summary["unavailable_rows"]}</li>
<li>Rows restored from separately recovered files:
    {summary["separately_recovered_rows"]}</li>
<li>Effectively unrecovered attachment rows:
    {summary["effectively_unrecovered_rows"]}</li>
<li>Standalone database records not rendered by the exporter:
    {summary["standalone_messages_not_rendered"]}</li>
</ul>
<p><a href="messages-not-rendered.html">Browse database records not rendered
in conversation HTML</a></p>
<p><a href="unavailable-attachments.html">Browse unavailable attachment
records</a></p>
<p><a href="attachment-manifest.json">Open the complete attachment manifest</a></p>
<p><a href="chat.db">Download the consistent Messages SQLite snapshot</a></p>
"""
    (preservation_path / "index.html").write_text(
        html_page("iMessage Source Preservation", index_body),
        encoding="utf-8",
    )

    unavailable_rows = [
        row
        for row in attachment_manifest
        if not row["source_present"] and not row["separately_recovered"]
    ]
    unavailable_body = [
        '<p class="warning">These database records had no locally readable '
        "source file when the preservation pass ran.</p>",
        "<table><thead><tr><th>Row</th><th>Name</th><th>Source path</th>"
        "<th>Messages</th><th>Status</th></tr></thead><tbody>",
    ]
    for row in unavailable_rows:
        unavailable_body.append(
            "<tr>"
            f"<td>{row['row_id']}</td>"
            f"<td>{html.escape(str(row['transfer_name'] or ''))}</td>"
            f"<td><code>{html.escape(str(row['source_filename'] or ''))}</code></td>"
            f"<td><code>{html.escape(', '.join(row['message_guids']))}</code></td>"
            f"<td>{html.escape(str(row['coverage']))}</td>"
            "</tr>"
        )
    unavailable_body.append("</tbody></table>")
    (preservation_path / "unavailable-attachments.html").write_text(
        html_page("Unavailable iMessage Attachments", "".join(unavailable_body)),
        encoding="utf-8",
    )

    message_body = [
        "<p>These standalone database rows do not have their own anchor in "
        "the exporter-generated conversation HTML. The original rows remain "
        "losslessly available in <code>chat.db</code>.</p>",
        "<table><thead><tr><th>Row</th><th>GUID</th><th>Type</th>"
        "<th>Chat</th><th>Text or event data</th></tr></thead><tbody>",
    ]
    for row in omitted_messages:
        description = (
            row["text"]
            or row["subject"]
            or row["group_title"]
            or row["balloon_bundle_id"]
            or "(no displayable text)"
        )
        message_body.append(
            "<tr>"
            f"<td>{row['row_id']}</td>"
            f"<td><code>{html.escape(row['guid'])}</code></td>"
            f"<td>{row['item_type']}</td>"
            f"<td>{html.escape(row['chat_names'])}</td>"
            f"<td>{html.escape(description)}</td>"
            "</tr>"
        )
    message_body.append("</tbody></table>")
    (preservation_path / "messages-not-rendered.html").write_text(
        html_page("Messages Records Not Rendered by the Exporter", "".join(message_body)),
        encoding="utf-8",
    )


def preserve(
    export_path: Path,
    database_path: Path,
    directory_name: str,
    patch_html: bool,
    recovered_source_map: Path | None = None,
) -> tuple[dict[str, object], bool]:
    """Run the complete preservation pass and return its report and status."""
    preservation_path = export_path / directory_name
    preservation_path.mkdir(parents=True, exist_ok=True)
    os.chmod(preservation_path, 0o700)
    attachment_path = preservation_path / "attachments"
    attachment_path.mkdir(parents=True, exist_ok=True)

    snapshot_path = preservation_path / "chat.db"
    snapshot_database(database_path, snapshot_path)
    connection = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row

    digest_cache = DigestCache()
    html_archive = HtmlArchive(export_path, digest_cache)
    recovered_rows = load_recovered_rows(
        export_path,
        recovered_source_map,
        digest_cache,
    )
    message_links = query_message_links(connection)
    candidates_by_size = build_archive_index(export_path, preservation_path)
    content_paths: dict[str, Path] = {}

    attachment_manifest: list[dict[str, object]] = []
    copied_file_count = 0
    copied_bytes = 0
    html_status_counts: collections.Counter[str] = collections.Counter()

    attachment_rows = connection.execute(
        """
        SELECT ROWID, guid, filename, transfer_name, total_bytes, mime_type,
               uti, COALESCE(is_sticker, 0) AS is_sticker,
               COALESCE(hide_attachment, 0) AS hide_attachment
        FROM attachment
        ORDER BY ROWID
        """
    )
    for row in attachment_rows:
        row_id = row["ROWID"]
        source = resolve_source_path(row["filename"])
        source_present = bool(source and source.is_file())
        links = message_links.get(row_id, [])
        record: dict[str, object] = {
            "row_id": row_id,
            "attachment_guid": row["guid"],
            "source_filename": row["filename"],
            "transfer_name": row["transfer_name"],
            "database_total_bytes": row["total_bytes"],
            "mime_type": row["mime_type"],
            "uti": row["uti"],
            "is_sticker": bool(row["is_sticker"]),
            "hidden_internal_payload": bool(row["hide_attachment"]),
            "message_guids": [link.guid for link in links],
            "source_present": source_present,
            "separately_recovered": False,
            "source_bytes": None,
            "sha256": None,
            "archive_path": None,
            "coverage": "missing_file" if row["filename"] else "no_source_path",
            "html_links": [],
        }

        if source_present and source is not None:
            source_size = source.stat().st_size
            source_digest = digest_cache.sha256(source)
            record["source_bytes"] = source_size
            record["sha256"] = source_digest

            archived = content_paths.get(source_digest)
            if archived is None:
                archived = find_identical_file(
                    source,
                    source_digest,
                    candidates_by_size,
                    digest_cache,
                )
            if archived is not None:
                record["coverage"] = "identical_bytes_already_archived"
            else:
                shard = f"{row_id % 256:02x}"
                destination_directory = attachment_path / shard
                destination_directory.mkdir(parents=True, exist_ok=True)
                archived = destination_directory / safe_filename(
                    row_id,
                    row["filename"],
                    row["transfer_name"],
                )
                if archived.exists() and digest_cache.sha256(archived) != source_digest:
                    raise FileExistsError(
                        f"Refusing to replace different bytes at {archived}"
                    )
                if not archived.exists():
                    shutil.copy2(source, archived)
                    os.chmod(archived, 0o600)
                    copied_file_count += 1
                    copied_bytes += source_size
                    candidates_by_size[source_size].append(archived)
                record["coverage"] = "copied_to_source_preservation"

            content_paths[source_digest] = archived
            record["archive_path"] = relative_archive_path(export_path, archived)

            if row["hide_attachment"]:
                html_status_counts["hidden_internal_payload"] += 1
            else:
                for link in links:
                    if html_archive.message_references_content(
                        link.guid,
                        source_digest,
                        archived,
                    ):
                        link_status = "already_linked"
                    elif patch_html:
                        link_status = html_archive.connect_attachment(
                            link.guid,
                            row_id,
                            archived,
                            row["transfer_name"] or source.name,
                            media_type(source, row["mime_type"]),
                        )
                    else:
                        link_status = "not_patched"
                    if link_status in {
                        "already_linked",
                        "already_patched",
                        "appended_to_conversation",
                        "repaired_error",
                    }:
                        html_archive.note_message_content(
                            link.guid,
                            source_digest,
                        )
                    record["html_links"].append(
                        {"message_guid": link.guid, "status": link_status}
                    )
                    html_status_counts[link_status] += 1
                if not links:
                    html_status_counts["no_message_relationship"] += 1
        elif row_id in recovered_rows:
            recovered = recovered_rows[row_id]
            record["separately_recovered"] = True
            record["source_bytes"] = recovered["source_bytes"]
            record["sha256"] = recovered["sha256"]
            record["archive_path"] = recovered["archive_path"]
            record["coverage"] = recovered["recovery_kind"]
            archived = recovered["path"]
            source_digest = recovered["sha256"]
            if row["hide_attachment"]:
                html_status_counts["hidden_internal_payload"] += 1
            else:
                for link in links:
                    if html_archive.message_references_content(
                        link.guid,
                        source_digest,
                        archived,
                    ):
                        link_status = "already_linked"
                    elif patch_html:
                        link_status = html_archive.connect_attachment(
                            link.guid,
                            row_id,
                            archived,
                            row["transfer_name"] or archived.name,
                            media_type(archived, row["mime_type"]),
                        )
                    else:
                        link_status = "not_patched"
                    if link_status in {
                        "already_linked",
                        "already_patched",
                        "appended_to_conversation",
                        "repaired_broken_reference",
                        "repaired_error",
                    }:
                        html_archive.note_message_content(
                            link.guid,
                            source_digest,
                        )
                    record["html_links"].append(
                        {"message_guid": link.guid, "status": link_status}
                    )
                    html_status_counts[link_status] += 1
                if not links:
                    html_status_counts["no_message_relationship"] += 1

        attachment_manifest.append(record)

    changed_html_files = html_archive.save() if patch_html else 0

    omitted_messages: list[dict[str, object]] = []
    message_rows = connection.execute(
        """
        SELECT m.ROWID, m.guid, COALESCE(m.item_type, 0) AS item_type,
               COALESCE(m.text, '') AS text,
               COALESCE(m.subject, '') AS subject,
               COALESCE(m.group_title, '') AS group_title,
               COALESCE(m.balloon_bundle_id, '') AS balloon_bundle_id,
               COALESCE(m.date, 0) AS date,
               COALESCE(group_concat(DISTINCT
                   COALESCE(NULLIF(c.display_name, ''), c.chat_identifier)
               ), '') AS chat_names
        FROM message m
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE COALESCE(m.associated_message_type, 0) = 0
        GROUP BY m.ROWID
        ORDER BY m.date, m.ROWID
        """
    )
    exported_guids = html_archive.exported_guids
    for row in message_rows:
        if row["guid"] in exported_guids:
            continue
        omitted_messages.append(
            {
                "row_id": row["ROWID"],
                "guid": row["guid"],
                "item_type": row["item_type"],
                "text": row["text"],
                "subject": row["subject"],
                "group_title": row["group_title"],
                "balloon_bundle_id": row["balloon_bundle_id"],
                "date": row["date"],
                "chat_names": row["chat_names"],
            }
        )
    connection.close()
    for suffix in ("-shm", "-wal"):
        sidecar = snapshot_path.with_name(snapshot_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()

    locally_readable = [row for row in attachment_manifest if row["source_present"]]
    locally_unreadable = [row for row in attachment_manifest if not row["source_present"]]
    separately_recovered = [
        row for row in attachment_manifest if row["separately_recovered"]
    ]
    effectively_unrecovered = [
        row
        for row in attachment_manifest
        if not row["source_present"] and not row["separately_recovered"]
    ]
    unpreserved = [
        row
        for row in locally_readable
        if not row["archive_path"] or not row["sha256"]
    ]
    summary = {
        "attachment_rows": len(attachment_manifest),
        "locally_readable_rows": len(locally_readable),
        "locally_readable_rows_preserved": len(locally_readable) - len(unpreserved),
        "locally_readable_rows_unpreserved": len(unpreserved),
        "unavailable_rows": len(locally_unreadable),
        "separately_recovered_rows": len(separately_recovered),
        "effectively_unrecovered_rows": len(effectively_unrecovered),
        "preserved_readable_or_recovered_rows": (
            len(locally_readable) + len(separately_recovered)
        ),
        "no_source_path_rows": sum(
            row["coverage"] == "no_source_path" for row in attachment_manifest
        ),
        "missing_file_rows": sum(
            row["coverage"] == "missing_file" for row in attachment_manifest
        ),
        "supplemental_unique_files_copied": copied_file_count,
        "supplemental_bytes_copied": copied_bytes,
        "supplemental_attachment_file_count": sum(
            path.is_file() for path in attachment_path.rglob("*")
        ),
        "supplemental_attachment_bytes": sum(
            path.stat().st_size
            for path in attachment_path.rglob("*")
            if path.is_file()
        ),
        "conversation_html_files_changed": changed_html_files,
        "standalone_messages_not_rendered": len(omitted_messages),
        "html_link_status_counts": dict(sorted(html_status_counts.items())),
    }
    passed = not unpreserved
    report: dict[str, object] = {
        "database_source": str(database_path),
        "database_snapshot": relative_archive_path(export_path, snapshot_path),
        "export_path": str(export_path),
        "preservation_directory": directory_name,
        "recovered_source_map": (
            str(recovered_source_map) if recovered_source_map else None
        ),
        "summary": summary,
        "passed": passed,
    }

    write_json(preservation_path / "attachment-manifest.json", attachment_manifest)
    write_indexes(
        preservation_path,
        report,
        attachment_manifest,
        omitted_messages,
    )
    write_json(export_path / "source-preservation.json", report)
    return report, passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_path", type=Path)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("~/Library/Messages/chat.db"),
        help="Messages database to snapshot and audit.",
    )
    parser.add_argument(
        "--directory-name",
        default="Source Preservation",
        help="Supplemental directory created inside the export.",
    )
    parser.add_argument(
        "--no-patch-html",
        action="store_true",
        help="Preserve all bytes and indexes without amending conversation HTML.",
    )
    parser.add_argument(
        "--recovered-source-map",
        type=Path,
        help="Optional map of separately recovered files to attachment row IDs.",
    )
    args = parser.parse_args()

    export_path = args.export_path.expanduser().resolve()
    database_path = args.database.expanduser().resolve()
    if not export_path.is_dir():
        parser.error(f"{export_path} is not a directory")
    if not database_path.is_file():
        parser.error(f"{database_path} is not a file")

    report, passed = preserve(
        export_path,
        database_path,
        args.directory_name,
        patch_html=not args.no_patch_html,
        recovered_source_map=(
            args.recovered_source_map.expanduser().resolve()
            if args.recovered_source_map
            else None
        ),
    )
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
