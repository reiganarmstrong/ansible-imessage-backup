#!/usr/bin/env python3
"""Verify the basic structural integrity of an imessage-exporter archive."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


IGNORED_SCHEMES = {
    "blob",
    "data",
    "http",
    "https",
    "javascript",
    "mailto",
    "sms",
    "tel",
}
REFERENCE_ATTRIBUTES = {"href", "poster", "src"}
OPTIONAL_LOCAL_REFERENCES = {"style.css"}
ATTACHMENT_ERROR_PATTERN = re.compile(r'class="attachment_error"')


class ReferenceParser(HTMLParser):
    """Collect file-like references from HTML attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del tag
        for name, value in attrs:
            if name.lower() in REFERENCE_ATTRIBUTES and value:
                self.references.append(value)


def local_target(html_file: Path, reference: str) -> Path | None:
    """Resolve a local HTML reference, or return None for a non-file link."""
    if not reference or reference.startswith("#"):
        return None

    parsed = urlsplit(reference)
    if parsed.scheme.lower() in IGNORED_SCHEMES or parsed.netloc:
        return None
    if parsed.scheme:
        return None
    if not parsed.path:
        return None

    decoded_path = unquote(parsed.path)
    candidate = Path(decoded_path)
    if candidate.is_absolute():
        return candidate
    return html_file.parent / candidate


def verify_export(
    export_path: Path, allow_broken_references: bool = False
) -> tuple[dict[str, object], bool]:
    """Return the verification report and whether the archive passed."""
    entries = list(export_path.rglob("*"))
    symlinks = sorted(path for path in entries if path.is_symlink())
    files = sorted(path for path in entries if path.is_file() and not path.is_symlink())
    html_files = [path for path in files if path.suffix.lower() == ".html"]
    empty_files = [path for path in files if path.stat().st_size == 0]
    total_bytes = sum(path.stat().st_size for path in files)

    broken_references: set[str] = set()
    local_reference_count = 0
    incomplete_html: list[Path] = []
    attachment_error_count = 0

    for html_file in html_files:
        text = html_file.read_text(encoding="utf-8", errors="replace")
        attachment_error_count += len(ATTACHMENT_ERROR_PATTERN.findall(text))
        if not text.rstrip().lower().endswith("</html>"):
            incomplete_html.append(html_file)

        parser = ReferenceParser()
        parser.feed(text)
        for reference in parser.references:
            if urlsplit(reference).path in OPTIONAL_LOCAL_REFERENCES:
                continue
            target = local_target(html_file, reference)
            if target is None:
                continue
            local_reference_count += 1
            if not target.exists():
                broken_references.add(
                    f"{html_file.relative_to(export_path)} -> {reference}"
                )

    def relative_strings(paths: list[Path]) -> list[str]:
        return [str(path.relative_to(export_path)) for path in paths]

    source_preservation_path = export_path / "source-preservation.json"
    source_preservation_present = source_preservation_path.is_file()
    source_preservation_passed: bool | None = None
    source_attachment_coverage: dict[str, object] | None = None
    source_preservation_error: str | None = None
    if source_preservation_present:
        try:
            source_preservation = json.loads(
                source_preservation_path.read_text(encoding="utf-8")
            )
            source_preservation_passed = source_preservation.get("passed") is True
            summary = source_preservation.get("summary")
            if isinstance(summary, dict):
                source_attachment_coverage = summary
            else:
                source_preservation_passed = False
                source_preservation_error = "source preservation summary is missing"
        except (OSError, json.JSONDecodeError) as error:
            source_preservation_passed = False
            source_preservation_error = str(error)

    report: dict[str, object] = {
        "export_path": str(export_path),
        "file_count_before_verification_report": len(files),
        "total_bytes_before_verification_report": total_bytes,
        "html_file_count": len(html_files),
        "local_reference_count": local_reference_count,
        "broken_reference_count": len(broken_references),
        "broken_references": sorted(broken_references),
        "empty_file_count": len(empty_files),
        "empty_files": relative_strings(empty_files),
        "symlink_count": len(symlinks),
        "symlinks": relative_strings(symlinks),
        "incomplete_html_count": len(incomplete_html),
        "incomplete_html_files": relative_strings(incomplete_html),
        "attachment_error_placeholder_count": attachment_error_count,
        "source_preservation_present": source_preservation_present,
        "source_preservation_passed": source_preservation_passed,
        "source_preservation_error": source_preservation_error,
        "source_attachment_coverage": source_attachment_coverage,
        "allow_broken_references": allow_broken_references,
    }
    passed = not (
        (broken_references and not allow_broken_references)
        or empty_files
        or symlinks
        or incomplete_html
        or not html_files
        or source_preservation_passed is False
    )
    report["passed"] = passed
    return report, passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_path", type=Path)
    parser.add_argument(
        "--allow-broken-references",
        action="store_true",
        help="Report broken local references without failing verification.",
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

    report, passed = verify_export(
        export_path,
        allow_broken_references=args.allow_broken_references,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report_path:
        report_path = args.report_path.expanduser().resolve()
        report_path.write_text(rendered, encoding="utf-8")
        os.chmod(report_path, 0o600)
    sys.stdout.write(rendered)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
