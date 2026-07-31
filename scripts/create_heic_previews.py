#!/usr/bin/env python3
"""Create browser-compatible JPEG previews while preserving HEIC originals."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


HEIC_SUFFIXES = {".heic", ".heif"}
JPEG_QUALITY = {"low": 60, "normal": 75, "high": 90, "best": 100}
JPEG_START_OF_FRAME = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


class ImageParser(HTMLParser):
    """Collect image source attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() != "img":
            return
        for name, value in attrs:
            if name.lower() == "src" and value:
                self.sources.append(value)


def discover_heic_references(
    export_path: Path,
) -> tuple[dict[Path, list[tuple[Path, str]]], list[str]]:
    """Map existing HEIC files to the HTML documents that reference them."""
    export_path = export_path.resolve()
    references: dict[Path, list[tuple[Path, str]]] = defaultdict(list)
    missing: list[str] = []

    for html_file in sorted(export_path.glob("*.html")):
        document = html_file.read_text(encoding="utf-8", errors="strict")
        parser = ImageParser()
        parser.feed(document)
        for source_reference in parser.sources:
            parsed = urlsplit(source_reference)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            if Path(parsed.path).suffix.lower() not in HEIC_SUFFIXES:
                continue

            source_path = (html_file.parent / unquote(parsed.path)).resolve()
            try:
                source_path.relative_to(export_path)
            except ValueError as error:
                raise ValueError(
                    f"HEIC reference escapes the export: {source_reference}"
                ) from error

            if source_path.is_file():
                references[source_path].append((html_file, source_reference))
            else:
                missing.append(f"{html_file.name} -> {source_reference}")

    return dict(references), sorted(set(missing))


def pixel_dimensions(image_path: Path) -> tuple[int, int] | None:
    """Read JPEG dimensions without spawning a process or decoding pixels."""
    try:
        with image_path.open("rb") as image:
            if image.read(2) != b"\xff\xd8":
                return None
            while True:
                marker_prefix = image.read(1)
                if not marker_prefix:
                    return None
                if marker_prefix != b"\xff":
                    continue
                marker_byte = image.read(1)
                while marker_byte == b"\xff":
                    marker_byte = image.read(1)
                if not marker_byte:
                    return None
                marker = marker_byte[0]
                if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                    continue
                length_bytes = image.read(2)
                if len(length_bytes) != 2:
                    return None
                segment_length = int.from_bytes(length_bytes, "big")
                if segment_length < 2:
                    return None
                if marker in JPEG_START_OF_FRAME:
                    frame_header = image.read(5)
                    if len(frame_header) != 5:
                        return None
                    height = int.from_bytes(frame_header[1:3], "big")
                    width = int.from_bytes(frame_header[3:5], "big")
                    return (width, height) if width and height else None
                image.seek(segment_length - 2, 1)
    except OSError:
        return None


def create_preview(
    source_path: Path,
    destination_path: Path,
    quality: str,
    max_pixels: int,
    qlmanage_binary: str,
    sips_binary: str,
) -> tuple[int, int]:
    """Render one HEIC through Quick Look, then encode a validated JPEG."""
    with tempfile.TemporaryDirectory(prefix="imessage-heic-preview-") as temporary:
        temporary_path = Path(temporary)
        subprocess.run(
            [
                qlmanage_binary,
                "-t",
                "-s",
                str(max_pixels),
                "-o",
                str(temporary_path),
                str(source_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        rendered_path = temporary_path / f"{source_path.name}.png"
        if not rendered_path.is_file():
            raise RuntimeError(f"Quick Look did not render {source_path}")

        subprocess.run(
            [
                sips_binary,
                "-s",
                "format",
                "jpeg",
                "-s",
                "formatOptions",
                quality,
                str(rendered_path),
                "--out",
                str(destination_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    dimensions = pixel_dimensions(destination_path)
    if not dimensions or min(dimensions) <= 0:
        destination_path.unlink(missing_ok=True)
        raise RuntimeError(f"Generated JPEG has no pixel data: {destination_path}")
    return dimensions


def create_preview_with_libheif(
    source_path: Path,
    destination_path: Path,
    quality: str,
    heif_convert_binary: str,
) -> tuple[int, int]:
    """Decode one HEIC directly with libheif and validate the JPEG."""
    subprocess.run(
        [
            heif_convert_binary,
            "--quiet",
            "-q",
            str(JPEG_QUALITY[quality]),
            str(source_path),
            str(destination_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    dimensions = pixel_dimensions(destination_path)
    if not dimensions or min(dimensions) <= 0:
        destination_path.unlink(missing_ok=True)
        raise RuntimeError(f"Generated JPEG has no pixel data: {destination_path}")
    return dimensions


def preview_reference(source_reference: str) -> str:
    """Change only the HEIC suffix of a URL-style reference."""
    parsed = urlsplit(source_reference)
    suffix = Path(parsed.path).suffix
    return source_reference[: len(source_reference) - len(suffix)] + ".jpeg"


def patch_html_references(
    reference_map: dict[Path, list[tuple[Path, str]]],
    preview_paths: dict[Path, Path],
) -> int:
    """Patch exported image source attributes and return the document count."""
    replacements_by_html: dict[Path, dict[str, str]] = defaultdict(dict)
    for source_path, occurrences in reference_map.items():
        if source_path not in preview_paths:
            continue
        for html_file, source_reference in occurrences:
            replacements_by_html[html_file][source_reference] = preview_reference(
                source_reference
            )

    patched_count = 0
    for html_file, replacements in replacements_by_html.items():
        document = html_file.read_text(encoding="utf-8", errors="strict")
        updated = document
        for original, replacement in replacements.items():
            updated = updated.replace(
                f'src="{original}"',
                f'src="{replacement}"',
            )
            updated = updated.replace(
                f"src='{original}'",
                f"src='{replacement}'",
            )
        if updated != document:
            html_file.write_text(updated, encoding="utf-8")
            patched_count += 1
    return patched_count


def merge_preview_reports(
    current: dict[str, object],
    previous_reports: list[dict[str, object]],
) -> dict[str, object]:
    """Merge multi-pass preview results while keeping current missing refs."""
    merged_results: dict[str, dict[str, object]] = {}
    created_count = 0
    patched_html_count = 0
    quicklook_fallback_count = 0
    for report in [*previous_reports, current]:
        created_count += int(report.get("created_count", 0))
        patched_html_count += int(report.get("patched_html_count", 0))
        quicklook_fallback_count += int(
            report.get("quicklook_fallback_count", 0)
        )
        for result in report.get("results", []):
            if isinstance(result, dict) and isinstance(result.get("source"), str):
                merged_results[result["source"]] = result
    current["created_count"] = created_count
    current["preview_count"] = len(merged_results)
    current["patched_html_count"] = patched_html_count
    current["quicklook_fallback_count"] = quicklook_fallback_count
    current["results"] = [
        merged_results[source] for source in sorted(merged_results)
    ]
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_path", type=Path)
    parser.add_argument(
        "--quality",
        choices=("low", "normal", "high", "best"),
        default="high",
    )
    parser.add_argument("--max-pixels", type=int, default=4096)
    parser.add_argument(
        "--workers",
        type=int,
        default=min(os.cpu_count() or 1, 8),
    )
    parser.add_argument("--heif-convert", default="")
    parser.add_argument("--report-path", type=Path)
    parser.add_argument(
        "--merge-report",
        type=Path,
        action="append",
        default=[],
        help="Merge results from an earlier preview pass into this report.",
    )
    parser.add_argument("--qlmanage", default="/usr/bin/qlmanage")
    parser.add_argument("--sips", default="/usr/bin/sips")
    args = parser.parse_args()

    export_path = args.export_path.expanduser().resolve()
    if not export_path.is_dir():
        parser.error(f"{export_path} is not a directory")

    reference_map, missing_references = discover_heic_references(export_path)
    preview_paths: dict[Path, Path] = {}
    result_by_source: dict[Path, dict[str, object]] = {}
    created_count = 0
    quicklook_fallback_count = 0
    heif_convert_binary = args.heif_convert or shutil.which("heif-convert")
    pending: list[tuple[Path, Path]] = []

    for source_path in sorted(reference_map):
        destination_path = source_path.with_suffix(".jpeg")
        dimensions = (
            pixel_dimensions(destination_path)
            if destination_path.is_file()
            else None
        )
        if dimensions:
            preview_paths[source_path] = destination_path
            result_by_source[source_path] = {
                "source": str(source_path.relative_to(export_path)),
                "preview": str(destination_path.relative_to(export_path)),
                "pixel_width": dimensions[0],
                "pixel_height": dimensions[1],
                "status": "reused",
            }
        else:
            pending.append((source_path, destination_path))

    if heif_convert_binary and pending:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    create_preview_with_libheif,
                    source_path,
                    destination_path,
                    args.quality,
                    heif_convert_binary,
                ): (source_path, destination_path)
                for source_path, destination_path in pending
            }
            for future in as_completed(futures):
                source_path, destination_path = futures[future]
                converter = "libheif"
                try:
                    dimensions = future.result()
                except (RuntimeError, subprocess.CalledProcessError):
                    dimensions = create_preview(
                        source_path,
                        destination_path,
                        args.quality,
                        args.max_pixels,
                        args.qlmanage,
                        args.sips,
                    )
                    converter = "quicklook_fallback"
                    quicklook_fallback_count += 1
                preview_paths[source_path] = destination_path
                result_by_source[source_path] = {
                    "source": str(source_path.relative_to(export_path)),
                    "preview": str(destination_path.relative_to(export_path)),
                    "pixel_width": dimensions[0],
                    "pixel_height": dimensions[1],
                    "status": "created",
                    "converter": converter,
                }
                created_count += 1
    else:
        for source_path, destination_path in pending:
            dimensions = create_preview(
                source_path,
                destination_path,
                args.quality,
                args.max_pixels,
                args.qlmanage,
                args.sips,
            )
            preview_paths[source_path] = destination_path
            result_by_source[source_path] = {
                "source": str(source_path.relative_to(export_path)),
                "preview": str(destination_path.relative_to(export_path)),
                "pixel_width": dimensions[0],
                "pixel_height": dimensions[1],
                "status": "created",
                "converter": "quicklook",
            }
            created_count += 1

    patched_html_count = patch_html_references(reference_map, preview_paths)
    report = {
        "created_count": created_count,
        "preview_count": len(preview_paths),
        "patched_html_count": patched_html_count,
        "converter": "libheif" if heif_convert_binary else "quicklook",
        "quicklook_fallback_count": quicklook_fallback_count,
        "workers": args.workers if heif_convert_binary else 1,
        "missing_heic_reference_count": len(missing_references),
        "missing_heic_references": missing_references,
        "results": [result_by_source[source] for source in sorted(result_by_source)],
    }
    previous_reports = [
        json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
        for path in args.merge_report
        if path.expanduser().resolve().is_file()
    ]
    if previous_reports:
        report = merge_preview_reports(report, previous_reports)
    serialized_report = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report_path:
        report_path = args.report_path.expanduser().resolve()
        report_path.write_text(serialized_report, encoding="utf-8")
        os.chmod(report_path, 0o600)
        json.dump(
            {
                "created_count": report["created_count"],
                "preview_count": report["preview_count"],
                "patched_html_count": report["patched_html_count"],
                "quicklook_fallback_count": report["quicklook_fallback_count"],
                "missing_heic_reference_count": report[
                    "missing_heic_reference_count"
                ],
                "report_path": str(report_path),
            },
            sys.stdout,
            indent=2,
            sort_keys=True,
        )
        print()
    else:
        sys.stdout.write(serialized_report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
