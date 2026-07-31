from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "select_remote_catalog_archives.py"
)
SPEC = importlib.util.spec_from_file_location(
    "select_remote_catalog_archives",
    SCRIPT,
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SelectRemoteCatalogArchivesTests(unittest.TestCase):
    def test_include_local_archives_prepares_retirement_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            name = "imessage-backup-20240101T000000"
            marker_root = root / ".state" / "remote-verifications"
            marker_root.mkdir(parents=True)
            (root / name).mkdir()
            (marker_root / f"{name}.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "archive_name": name,
                        "remote_destination": f"nextcloud:root/{name}",
                        "verified_at": "2024-01-01T00:00:00Z",
                        "file_count": 1,
                        "total_bytes": 1,
                    }
                ),
                encoding="utf-8",
            )

            selected = MODULE.archives_requiring_cache(
                root,
                "imessage-backup",
                ".state",
                "catalog-cache",
                "nextcloud:root",
                include_local_archives=True,
            )

            self.assertEqual(selected, [name])

    def test_selects_only_uncached_remote_only_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state = root / ".state"
            markers = state / "remote-verifications"
            cache = state / "catalog-cache"
            markers.mkdir(parents=True)
            names = [
                "imessage-backup-20240101T000000",
                "imessage-backup-20240201T000000",
                "imessage-backup-20240301T000000",
            ]
            for index, name in enumerate(names):
                marker = {
                    "passed": True,
                    "archive_name": name,
                    "remote_destination": f"nextcloud:root/{name}",
                    "verified_at": f"2024-03-0{index + 1}T00:00:00Z",
                    "file_count": index + 10,
                    "total_bytes": index + 100,
                }
                (markers / f"{name}.json").write_text(
                    json.dumps(marker),
                    encoding="utf-8",
                )
            (root / names[1]).mkdir()
            cached = cache / names[2]
            cached.mkdir(parents=True)
            source_marker = json.loads(
                (markers / f"{names[2]}.json").read_text(encoding="utf-8")
            )
            (cached / ".catalog-cache-verification.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "archive_name": names[2],
                        "remote_destination": source_marker[
                            "remote_destination"
                        ],
                        "remote_verified_at": source_marker["verified_at"],
                        "remote_file_count": source_marker["file_count"],
                        "remote_total_bytes": source_marker["total_bytes"],
                    }
                ),
                encoding="utf-8",
            )

            selected = MODULE.archives_requiring_cache(
                root,
                "imessage-backup",
                ".state",
                "catalog-cache",
                "nextcloud:root",
            )

            self.assertEqual(selected, [names[0]])

    def test_changed_remote_marker_invalidates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            name = "imessage-backup-20240101T000000"
            marker_root = root / ".state" / "remote-verifications"
            cache = root / ".state" / "catalog-cache" / name
            marker_root.mkdir(parents=True)
            cache.mkdir(parents=True)
            (marker_root / f"{name}.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "archive_name": name,
                        "remote_destination": f"nextcloud:root/{name}",
                        "verified_at": "new",
                        "file_count": 5,
                        "total_bytes": 10,
                    }
                ),
                encoding="utf-8",
            )
            (cache / ".catalog-cache-verification.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "archive_name": name,
                        "remote_destination": f"nextcloud:root/{name}",
                        "remote_verified_at": "old",
                        "remote_file_count": 5,
                        "remote_total_bytes": 10,
                    }
                ),
                encoding="utf-8",
            )

            selected = MODULE.archives_requiring_cache(
                root,
                "imessage-backup",
                ".state",
                "catalog-cache",
                "nextcloud:root",
            )

            self.assertEqual(selected, [name])
