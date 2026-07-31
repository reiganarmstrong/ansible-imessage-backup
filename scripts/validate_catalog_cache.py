#!/usr/bin/env python3
"""Validate the compact data required to index one remote-only archive."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3


def validate(cache_path: Path) -> dict[str, object]:
    verification_path = cache_path / "verification.json"
    manifest_path = (
        cache_path / "Source Preservation" / "attachment-manifest.json"
    )
    database_path = cache_path / "Source Preservation" / "chat.db"

    verification = json.loads(verification_path.read_text(encoding="utf-8"))
    if not isinstance(verification, dict) or verification.get("passed") is not True:
        raise ValueError("Archived verification.json did not pass")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        raise ValueError("Attachment manifest must be a JSON array")
    html_files = sorted(cache_path.glob("*.html"))
    if not html_files:
        raise ValueError("Catalog cache contains no root conversation HTML")

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        message_count = connection.execute(
            "SELECT COUNT(*) FROM message"
        ).fetchone()[0]
    finally:
        connection.close()
    if not quick_check or quick_check[0] != "ok":
        raise ValueError(f"Catalog chat.db quick_check failed: {quick_check}")

    return {
        "passed": True,
        "cache_path": str(cache_path),
        "html_file_count": len(html_files),
        "attachment_manifest_row_count": len(manifest),
        "message_count": message_count,
        "database_quick_check": "ok",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cache_path", type=Path)
    args = parser.parse_args()
    result = validate(args.cache_path.expanduser().resolve())
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
