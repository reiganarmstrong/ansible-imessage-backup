#!/usr/bin/env python3
"""Report messages eligible for manual pruning after verified remote backup."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import html
import json
import os
from pathlib import Path


SCHEMA_VERSION = 1


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def write_private(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)


def build_pruning_report(
    catalog: dict[str, object],
    message_index: dict[str, object],
    minimum_verified_copies: int,
    safety_hours: int,
) -> dict[str, object]:
    verified_archives = sorted(
        (
            archive
            for archive in catalog.get("archives", [])
            if archive.get("remote_verified")
        ),
        key=lambda archive: archive.get("created_at") or "",
    )
    generated_at = datetime.now(timezone.utc)
    report: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "automatic_deletion_performed": False,
        "minimum_remote_verified_complete_copies": minimum_verified_copies,
        "safety_hours": safety_hours,
        "remote_verified_archive_count": len(verified_archives),
        "candidate_cutoff": None,
        "safe_contiguous_cutoff": None,
        "eligible_message_count": 0,
        "blocked_message_count": 0,
        "eligible_message_guids": [],
        "blocked_reasons": {},
        "conversation_summaries": [],
    }
    if len(verified_archives) < minimum_verified_copies:
        report["status"] = "insufficient_remote_verified_archives"
        return report

    cutoff_archive = verified_archives[-minimum_verified_copies]
    cutoff = parse_timestamp(cutoff_archive.get("maximum_message_timestamp"))
    if cutoff is None:
        report["status"] = "verified_archive_has_no_message_cutoff"
        return report
    cutoff -= timedelta(hours=safety_hours)
    report["candidate_cutoff"] = cutoff.isoformat().replace("+00:00", "Z")
    report["cutoff_archive"] = cutoff_archive["name"]
    report["target_archive"] = verified_archives[-1]["name"]

    eligible: list[str] = []
    blocked: list[dict[str, object]] = []
    conversation_counts: Counter[tuple[str, str]] = Counter()
    message_fields = {
        name: index
        for index, name in enumerate(message_index.get("message_fields", []))
    }
    copy_fields = {
        name: index
        for index, name in enumerate(message_index.get("copy_fields", []))
    }
    conversations = catalog.get("conversations", [])
    archives = catalog.get("archives", [])
    target_archive_index = next(
        index
        for index, archive in enumerate(archives)
        if archive["name"] == report["target_archive"]
    )
    for message in message_index.get("messages", []):
        guid = message[message_fields["guid"]]
        timestamp_value = message[message_fields["timestamp"]]
        timestamp = parse_timestamp(timestamp_value)
        if timestamp is None or timestamp > cutoff:
            continue
        verified_copies = 0
        complete_copies = 0
        copies = message[message_fields["copies"]]
        if not any(
            copy[copy_fields["archive_index"]] == target_archive_index
            for copy in copies
        ):
            continue
        for copy in copies:
            archive = archives[copy[copy_fields["archive_index"]]]
            if not archive.get("remote_verified"):
                continue
            verified_copies += 1
            if copy[copy_fields["attachment_complete"]]:
                complete_copies += 1
        reasons = []
        if verified_copies < minimum_verified_copies:
            reasons.append("insufficient_remote_verified_copies")
        if complete_copies < minimum_verified_copies:
            reasons.append("insufficient_attachment_complete_remote_copies")
        if reasons:
            blocked.append(
                {
                    "guid": guid,
                    "timestamp": timestamp_value,
                    "reasons": reasons,
                }
            )
            continue
        eligible.append(guid)
        for conversation_index in message[
            message_fields["conversation_indexes"]
        ]:
            chat = conversations[conversation_index]
            conversation_counts[(chat["guid"], chat["name"])] += 1

    reason_counts = Counter(
        reason
        for message in blocked
        for reason in message["reasons"]
    )
    report["status"] = "ready_for_manual_review"
    report["eligible_message_count"] = len(eligible)
    report["blocked_message_count"] = len(blocked)
    report["eligible_message_guids"] = eligible
    report["blocked_reasons"] = dict(sorted(reason_counts.items()))
    report["blocked_messages"] = blocked
    report["conversation_summaries"] = [
        {
            "chat_guid": chat_guid,
            "chat_name": chat_name,
            "eligible_message_count": count,
        }
        for (chat_guid, chat_name), count in sorted(
            conversation_counts.items(),
            key=lambda item: (-item[1], item[0][1].casefold()),
        )
    ]
    if not blocked:
        report["safe_contiguous_cutoff"] = report["candidate_cutoff"]
    return report


def render_report(report: dict[str, object]) -> str:
    summaries = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(row['chat_name']))}</td>"
        f"<td>{row['eligible_message_count']}</td>"
        "</tr>"
        for row in report.get("conversation_summaries", [])
    )
    reasons = "\n".join(
        f"<li>{html.escape(reason)}: {count}</li>"
        for reason, count in report.get("blocked_reasons", {}).items()
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iMessage Safe-Pruning Report</title>
<style>
body {{ font: 16px/1.45 -apple-system, BlinkMacSystemFont, sans-serif;
       margin: 2rem auto; max-width: 1000px; padding: 0 1rem; }}
.warning {{ border-left: .35rem solid #e39b16; padding: .8rem 1rem;
            background: #e39b1618; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border-bottom: 1px solid #8885; padding: .5rem; text-align: left; }}
code {{ overflow-wrap: anywhere; }}
</style>
</head>
<body>
<h1>Safe-Pruning Eligibility</h1>
<div class="warning"><strong>This report never deletes Messages.</strong>
Review it before manually removing anything through the Messages app.</div>
<dl>
<dt>Status</dt><dd><code>{html.escape(str(report.get("status")))}</code></dd>
<dt>Candidate cutoff</dt><dd>{html.escape(str(report.get("candidate_cutoff")))}</dd>
<dt>Safe contiguous cutoff</dt><dd>{html.escape(str(report.get("safe_contiguous_cutoff")))}</dd>
<dt>Eligible messages</dt><dd>{report.get("eligible_message_count", 0)}</dd>
<dt>Blocked messages</dt><dd>{report.get("blocked_message_count", 0)}</dd>
<dt>Required complete remote copies</dt>
<dd>{report.get("minimum_remote_verified_complete_copies")}</dd>
</dl>
<h2>Blocking reasons</h2>
<ul>{reasons or "<li>None</li>"}</ul>
<h2>Eligible messages by conversation</h2>
<table><thead><tr><th>Conversation</th><th>Eligible messages</th></tr></thead>
<tbody>{summaries}</tbody></table>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup_root", type=Path)
    parser.add_argument("--catalog-name", default="archive-catalog.json")
    parser.add_argument("--message-index-name", default="message-index.json")
    parser.add_argument("--report-name", default="pruning-report.json")
    parser.add_argument("--html-report-name", default="pruning-report.html")
    parser.add_argument("--minimum-verified-copies", type=int, default=1)
    parser.add_argument("--safety-hours", type=int, default=24)
    args = parser.parse_args()
    if args.minimum_verified_copies < 1:
        parser.error("--minimum-verified-copies must be at least 1")
    if args.safety_hours < 0:
        parser.error("--safety-hours cannot be negative")

    backup_root = args.backup_root.expanduser().resolve()
    catalog = json.loads(
        (backup_root / args.catalog_name).read_text(encoding="utf-8")
    )
    message_index = json.loads(
        (backup_root / args.message_index_name).read_text(encoding="utf-8")
    )
    report = build_pruning_report(
        catalog,
        message_index,
        args.minimum_verified_copies,
        args.safety_hours,
    )
    write_private(
        backup_root / args.report_name,
        json.dumps(report, indent=2, sort_keys=True) + "\n",
    )
    write_private(
        backup_root / args.html_report_name,
        render_report(report),
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "eligible_message_count": report["eligible_message_count"],
                "blocked_message_count": report["blocked_message_count"],
                "candidate_cutoff": report["candidate_cutoff"],
                "safe_contiguous_cutoff": report["safe_contiguous_cutoff"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
