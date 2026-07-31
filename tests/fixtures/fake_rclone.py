#!/usr/bin/env python3
"""Small local rclone stand-in for lifecycle playbook integration tests."""

from __future__ import annotations

import filecmp
import fnmatch
import json
from pathlib import Path
import shutil
import sys


def local_path(value: str) -> Path:
    if value.startswith("fake:"):
        return Path(value.removeprefix("fake:"))
    return Path(value)


def option_value(arguments: list[str], option: str) -> str | None:
    try:
        return arguments[arguments.index(option) + 1]
    except (ValueError, IndexError):
        return None


def option_values(arguments: list[str], option: str) -> list[str]:
    return [
        arguments[index + 1]
        for index, value in enumerate(arguments[:-1])
        if value == option
    ]


def matches_include(relative: Path, patterns: list[str]) -> bool:
    if not patterns:
        return True
    value = relative.as_posix()
    for raw_pattern in patterns:
        pattern = raw_pattern.removeprefix("/")
        if (
            raw_pattern.startswith("/")
            and "/" not in pattern
            and "/" in value
        ):
            continue
        if fnmatch.fnmatchcase(value, pattern):
            return True
    return False


def selected_paths(source: Path, arguments: list[str]) -> list[Path]:
    files_from = option_value(arguments, "--files-from")
    if files_from:
        return [
            Path(line)
            for line in Path(files_from).read_text(encoding="utf-8").splitlines()
            if line
        ]
    include_patterns = option_values(arguments, "--include")
    return sorted(
        path.relative_to(source)
        for path in source.rglob("*")
        if path.is_file()
        and matches_include(path.relative_to(source), include_patterns)
    )


def copy_command(arguments: list[str]) -> int:
    source = local_path(arguments[0])
    destination = local_path(arguments[1])
    immutable = "--immutable" in arguments
    for relative in selected_paths(source, arguments):
        source_file = source / relative
        destination_file = destination / relative
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        if (
            immutable
            and destination_file.exists()
            and not filecmp.cmp(source_file, destination_file, shallow=False)
        ):
            print(f"immutable file differs: {relative}", file=sys.stderr)
            return 1
        if not destination_file.exists() or not filecmp.cmp(
            source_file,
            destination_file,
            shallow=False,
        ):
            shutil.copy2(source_file, destination_file)
    return 0


def check_command(arguments: list[str]) -> int:
    source = local_path(arguments[0])
    destination = local_path(arguments[1])
    selected = selected_paths(source, arguments)
    combined_path = option_value(arguments, "--combined")
    rows = []
    passed = True
    for relative in selected:
        source_file = source / relative
        destination_file = destination / relative
        if not destination_file.is_file():
            rows.append(f"+ {relative}")
            passed = False
        elif not filecmp.cmp(source_file, destination_file, shallow=False):
            rows.append(f"* {relative}")
            passed = False
        else:
            rows.append(f"= {relative}")
    if not option_value(arguments, "--files-from"):
        source_paths = set(selected)
        for relative in selected_paths(destination, arguments):
            if relative not in source_paths:
                rows.append(f"- {relative}")
                passed = False
    if combined_path:
        report = Path(combined_path)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return 0 if passed else 1


def size_command(arguments: list[str]) -> int:
    source = local_path(arguments[0])
    files = [
        source / relative
        for relative in selected_paths(source, arguments)
    ]
    print(
        json.dumps(
            {
                "count": len(files),
                "bytes": sum(path.stat().st_size for path in files),
                "sizeless": 0,
            },
            sort_keys=True,
        )
    )
    return 0


def purge_command(arguments: list[str]) -> int:
    target = local_path(arguments[0])
    if target.exists():
        shutil.rmtree(target)
    return 0


def deletefile_command(arguments: list[str]) -> int:
    target = local_path(arguments[0])
    if not target.is_file():
        return 1
    target.unlink()
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    command = sys.argv[1]
    arguments = sys.argv[2:]
    if command == "version":
        print("rclone vtest")
        return 0
    if command == "listremotes":
        print("fake:")
        return 0
    if command == "lsd":
        return 0
    if command == "copy" and len(arguments) >= 2:
        return copy_command(arguments)
    if command == "check" and len(arguments) >= 2:
        return check_command(arguments)
    if command == "size" and arguments:
        return size_command(arguments)
    if command == "purge" and arguments:
        return purge_command(arguments)
    if command == "deletefile" and arguments:
        return deletefile_command(arguments)
    print(f"unsupported fake rclone command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
