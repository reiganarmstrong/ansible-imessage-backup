#!/usr/bin/env python3
"""Build a private, static index across verified iMessage archives."""

from __future__ import annotations

import argparse
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, parse_qs


APPLE_EPOCH_SECONDS = 978_307_200
GUID_URL_PREFIX = "sms://open"
STATE_SCHEMA_VERSION = 1


def write_private_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)


def write_private_json(path: Path, value: object) -> None:
    write_private_text(
        path,
        json.dumps(value, indent=2, sort_keys=True) + "\n",
    )


def write_private_compact_json(path: Path, value: object) -> None:
    write_private_text(
        path,
        json.dumps(value, separators=(",", ":")) + "\n",
    )


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def apple_timestamp_to_iso(value: int | float | None) -> str | None:
    if value is None:
        return None
    seconds = float(value)
    if abs(seconds) > 10_000_000_000:
        seconds /= 1_000_000_000
    try:
        return datetime.fromtimestamp(
            APPLE_EPOCH_SECONDS + seconds,
            tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def normalized_text(parts: list[str]) -> str:
    return " ".join(" ".join(parts).split())


class MessageHTMLParser(HTMLParser):
    """Extract rendered message text and the source HTML file for each GUID."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.div_depth = 0
        self.contexts: list[dict[str, object]] = []
        self.messages: dict[str, str] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "div":
            self.div_depth += 1
            classes = set((attributes.get("class") or "").split())
            if "message" in classes:
                self.contexts.append(
                    {
                        "depth": self.div_depth,
                        "guid": None,
                        "parts": [],
                    }
                )
        if tag == "a" and self.contexts:
            href = attributes.get("href") or ""
            parsed = urlsplit(href)
            if href.startswith(GUID_URL_PREFIX):
                guid = parse_qs(parsed.query).get("message-guid", [None])[0]
                if guid:
                    self.contexts[-1]["guid"] = guid
        if tag in {"br", "hr"} and self.contexts:
            parts = self.contexts[-1]["parts"]
            assert isinstance(parts, list)
            parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.contexts:
            parts = self.contexts[-1]["parts"]
            assert isinstance(parts, list)
            parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "div":
            return
        if (
            self.contexts
            and self.contexts[-1]["depth"] == self.div_depth
        ):
            context = self.contexts.pop()
            guid = context["guid"]
            parts = context["parts"]
            assert isinstance(parts, list)
            if isinstance(guid, str):
                rendered = normalized_text(parts)
                if rendered:
                    current = self.messages.get(guid, "")
                    if len(rendered) > len(current):
                        self.messages[guid] = rendered
        self.div_depth -= 1


def scan_conversation_html(
    archive_path: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    guid_files: dict[str, str] = {}
    rendered_text: dict[str, str] = {}
    for html_path in sorted(archive_path.glob("*.html")):
        parser = MessageHTMLParser()
        try:
            parser.feed(
                html_path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            continue
        relative = str(html_path.relative_to(archive_path))
        for guid, text in parser.messages.items():
            guid_files.setdefault(guid, relative)
            current = rendered_text.get(guid, "")
            if len(text) > len(current):
                rendered_text[guid] = text
    return guid_files, rendered_text


def load_attachment_status(
    archive_path: Path,
) -> dict[str, dict[str, int | bool]]:
    manifest_path = (
        archive_path / "Source Preservation" / "attachment-manifest.json"
    )
    value = load_json(manifest_path, [])
    if not isinstance(value, list):
        return {}
    result: dict[str, dict[str, int | bool]] = {}
    for row in value:
        if not isinstance(row, dict):
            continue
        guids = row.get("message_guids", [])
        if not isinstance(guids, list):
            continue
        available = bool(
            row.get("sha256")
            and row.get("archive_path")
            and (
                row.get("source_present")
                or row.get("separately_recovered")
            )
        )
        for guid in guids:
            if not isinstance(guid, str):
                continue
            status = result.setdefault(
                guid,
                {"attachment_count": 0, "unresolved_attachment_count": 0},
            )
            status["attachment_count"] = int(status["attachment_count"]) + 1
            if not available:
                status["unresolved_attachment_count"] = (
                    int(status["unresolved_attachment_count"]) + 1
                )
    return result


def archive_created_at(name: str, path: Path) -> str:
    match = re.search(r"(\d{8}T\d{6})$", name)
    if match:
        try:
            parsed = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S")
            return parsed.replace(tzinfo=timezone.utc).isoformat().replace(
                "+00:00",
                "Z",
            )
        except ValueError:
            pass
    return datetime.fromtimestamp(
        path.stat().st_mtime,
        tz=timezone.utc,
    ).isoformat().replace("+00:00", "Z")


def read_remote_verification(
    backup_root: Path,
    archive_name: str,
    state_directory_name: str,
) -> dict[str, object] | None:
    report_path = (
        backup_root
        / state_directory_name
        / "remote-verifications"
        / f"{archive_name}.json"
    )
    value = load_json(report_path, None)
    if (
        isinstance(value, dict)
        and value.get("passed") is True
        and value.get("archive_name") == archive_name
    ):
        return value
    return None


def valid_catalog_cache(
    cache_path: Path,
    archive_name: str,
    remote: dict[str, object] | None,
) -> bool:
    marker = load_json(
        cache_path / ".catalog-cache-verification.json",
        None,
    )
    return (
        remote is not None
        and isinstance(marker, dict)
        and marker.get("passed") is True
        and marker.get("archive_name") == archive_name
        and marker.get("remote_destination")
        == remote.get("remote_destination")
        and marker.get("remote_verified_at") == remote.get("verified_at")
        and marker.get("remote_file_count") == remote.get("file_count")
        and marker.get("remote_total_bytes") == remote.get("total_bytes")
    )


def discover_archives(
    backup_root: Path,
    backup_prefix: str,
    state_directory_name: str,
    catalog_cache_directory_name: str = "catalog-cache",
) -> list[dict[str, object]]:
    archives: list[dict[str, object]] = []
    candidates: dict[str, tuple[Path, str]] = {}
    cache_root = (
        backup_root
        / state_directory_name
        / catalog_cache_directory_name
    )
    for cache_path in sorted(cache_root.glob(f"{backup_prefix}-*")):
        remote = read_remote_verification(
            backup_root,
            cache_path.name,
            state_directory_name,
        )
        if (
            cache_path.is_dir()
            and valid_catalog_cache(cache_path, cache_path.name, remote)
        ):
            candidates[cache_path.name] = (cache_path, "remote_cache")
    for local_path in sorted(backup_root.glob(f"{backup_prefix}-*")):
        if local_path.is_dir():
            candidates[local_path.name] = (local_path, "local_archive")

    for archive_name in sorted(candidates):
        archive_path, catalog_source = candidates[archive_name]
        if not archive_path.is_dir():
            continue
        verification = load_json(archive_path / "verification.json", {})
        if not isinstance(verification, dict) or verification.get("passed") is not True:
            continue
        database_path = archive_path / "Source Preservation" / "chat.db"
        remote = read_remote_verification(
            backup_root,
            archive_path.name,
            state_directory_name,
        )
        archives.append(
            {
                "name": archive_path.name,
                "path": archive_path,
                "catalog_source": catalog_source,
                "created_at": archive_created_at(
                    archive_path.name,
                    archive_path,
                ),
                "database_path": database_path,
                "database_present": database_path.is_file(),
                "verification": verification,
                "remote_verified": remote is not None,
                "remote_verification": remote,
            }
        )
    return archives


def query_archive_messages(
    archive: dict[str, object],
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    archive_path = archive["path"]
    database_path = archive["database_path"]
    assert isinstance(archive_path, Path)
    assert isinstance(database_path, Path)
    if not database_path.is_file():
        return [], {}

    guid_files, rendered_text = scan_conversation_html(archive_path)
    attachment_status = load_attachment_status(archive_path)
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT m.guid, m.text, m.subject, m.date, m.is_from_me,
               COALESCE(h.id, '') AS sender,
               c.guid AS chat_guid,
               COALESCE(
                   NULLIF(c.display_name, ''),
                   NULLIF(c.chat_identifier, ''),
                   c.guid,
                   'Unassociated messages'
               ) AS chat_name
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
        ORDER BY m.date, m.ROWID, c.ROWID
        """
    )

    messages_by_guid: dict[str, dict[str, object]] = {}
    chats: dict[str, dict[str, object]] = {}
    for row in rows:
        guid = row["guid"]
        message = messages_by_guid.get(guid)
        chat_guid = row["chat_guid"] or "unassociated"
        chat_name = row["chat_name"]
        if message is None:
            status = attachment_status.get(
                guid,
                {"attachment_count": 0, "unresolved_attachment_count": 0},
            )
            database_text = row["text"] or row["subject"] or ""
            message = {
                "guid": guid,
                "timestamp": apple_timestamp_to_iso(row["date"]),
                "text": database_text or rendered_text.get(guid, ""),
                "is_from_me": bool(row["is_from_me"]),
                "sender": "Me" if row["is_from_me"] else row["sender"],
                "chats": [],
                "html_path": guid_files.get(guid),
                "attachment_count": int(status["attachment_count"]),
                "unresolved_attachment_count": int(
                    status["unresolved_attachment_count"]
                ),
            }
            messages_by_guid[guid] = message
        chat_entry = {"guid": chat_guid, "name": chat_name}
        if chat_entry not in message["chats"]:
            message["chats"].append(chat_entry)
        chat = chats.setdefault(
            chat_guid,
            {
                "guid": chat_guid,
                "name": chat_name,
                "message_count": 0,
            },
        )
        chat["message_count"] = int(chat["message_count"]) + 1
    connection.close()
    return list(messages_by_guid.values()), chats


def preferred_copy_key(copy: dict[str, object]) -> tuple[bool, bool, str]:
    return (
        bool(copy["attachment_complete"]),
        bool(copy["remote_verified"]),
        str(copy["archive_created_at"]),
    )


def build_master_index(
    backup_root: Path,
    backup_prefix: str,
    state_directory_name: str,
    catalog_cache_directory_name: str = "catalog-cache",
) -> tuple[dict[str, object], dict[str, object]]:
    archives = discover_archives(
        backup_root,
        backup_prefix,
        state_directory_name,
        catalog_cache_directory_name,
    )
    messages: dict[str, dict[str, object]] = {}
    conversations: dict[str, dict[str, object]] = {}

    for archive in archives:
        archive_messages, archive_chats = query_archive_messages(archive)
        timestamps = [
            message["timestamp"]
            for message in archive_messages
            if message["timestamp"]
        ]
        archive["message_count"] = len(archive_messages)
        archive["minimum_message_timestamp"] = min(timestamps) if timestamps else None
        archive["maximum_message_timestamp"] = max(timestamps) if timestamps else None
        archive["conversation_count"] = len(archive_chats)
        verification = archive["verification"]
        assert isinstance(verification, dict)
        coverage = verification.get("source_attachment_coverage", {})
        archive["attachment_rows"] = (
            coverage.get("attachment_rows", 0)
            if isinstance(coverage, dict)
            else 0
        )
        archive["effectively_unrecovered_rows"] = (
            coverage.get("effectively_unrecovered_rows", 0)
            if isinstance(coverage, dict)
            else 0
        )

        for chat_guid, chat in archive_chats.items():
            conversation = conversations.setdefault(
                chat_guid,
                {
                    "guid": chat_guid,
                    "name": chat["name"],
                    "archive_names": [],
                    "unique_message_guids": set(),
                },
            )
            conversation["archive_names"].append(archive["name"])

        for archive_message in archive_messages:
            guid = str(archive_message["guid"])
            attachment_complete = (
                int(archive_message["unresolved_attachment_count"]) == 0
            )
            copy = {
                "archive_name": archive["name"],
                "archive_created_at": archive["created_at"],
                "remote_verified": archive["remote_verified"],
                "attachment_complete": attachment_complete,
                "attachment_count": archive_message["attachment_count"],
                "unresolved_attachment_count": archive_message[
                    "unresolved_attachment_count"
                ],
                "html_path": archive_message["html_path"],
            }
            entry = messages.get(guid)
            if entry is None:
                entry = {
                    key: archive_message[key]
                    for key in (
                        "guid",
                        "timestamp",
                        "text",
                        "is_from_me",
                        "sender",
                        "chats",
                    )
                }
                entry["copies"] = []
                messages[guid] = entry
            elif (
                not entry.get("text")
                and archive_message.get("text")
            ):
                entry["text"] = archive_message["text"]
            entry["copies"].append(copy)
            for chat in archive_message["chats"]:
                conversation = conversations.setdefault(
                    chat["guid"],
                    {
                        "guid": chat["guid"],
                        "name": chat["name"],
                        "archive_names": [],
                        "unique_message_guids": set(),
                    },
                )
                conversation["unique_message_guids"].add(guid)

    message_values = []
    for message in messages.values():
        copies = message["copies"]
        message["preferred_copy_index"] = max(
            range(len(copies)),
            key=lambda index: preferred_copy_key(copies[index]),
        )
        message_values.append(message)
    message_values.sort(
        key=lambda value: (
            value.get("timestamp") or "",
            value["guid"],
        )
    )

    conversation_values = []
    for conversation in conversations.values():
        unique_guids = conversation.pop("unique_message_guids")
        conversation["unique_message_count"] = len(unique_guids)
        conversation["archive_names"] = sorted(
            set(conversation["archive_names"])
        )
        conversation_values.append(conversation)
    conversation_values.sort(
        key=lambda value: (str(value["name"]).casefold(), value["guid"])
    )

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    catalog_archives = []
    for archive in archives:
        catalog_archives.append(
            {
                key: archive[key]
                for key in (
                    "name",
                    "catalog_source",
                    "created_at",
                    "database_present",
                    "remote_verified",
                    "message_count",
                    "minimum_message_timestamp",
                    "maximum_message_timestamp",
                    "conversation_count",
                    "attachment_rows",
                    "effectively_unrecovered_rows",
                )
            }
        )
    archive_indexes = {
        archive["name"]: index
        for index, archive in enumerate(catalog_archives)
    }
    conversation_indexes = {
        conversation["guid"]: index
        for index, conversation in enumerate(conversation_values)
    }
    message_fields = [
        "guid",
        "timestamp",
        "text",
        "is_from_me",
        "sender",
        "conversation_indexes",
        "copies",
        "preferred_copy_index",
    ]
    copy_fields = [
        "archive_index",
        "attachment_complete",
        "attachment_count",
        "unresolved_attachment_count",
        "html_path",
    ]
    compact_messages = []
    for message in message_values:
        compact_copies = [
            [
                archive_indexes[copy["archive_name"]],
                copy["attachment_complete"],
                copy["attachment_count"],
                copy["unresolved_attachment_count"],
                copy["html_path"],
            ]
            for copy in message["copies"]
        ]
        compact_messages.append(
            [
                message["guid"],
                message["timestamp"],
                message["text"],
                message["is_from_me"],
                message["sender"],
                [
                    conversation_indexes[chat["guid"]]
                    for chat in message["chats"]
                ],
                compact_copies,
                message["preferred_copy_index"],
            ]
        )
    catalog = {
        "schema_version": STATE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "backup_prefix": backup_prefix,
        "archive_count": len(catalog_archives),
        "remote_verified_archive_count": sum(
            bool(archive["remote_verified"]) for archive in catalog_archives
        ),
        "unique_message_count": len(message_values),
        "conversation_count": len(conversation_values),
        "archives": catalog_archives,
        "conversations": conversation_values,
    }
    message_index = {
        "schema_version": STATE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "encoding": "columnar-arrays",
        "message_fields": message_fields,
        "copy_fields": copy_fields,
        "unique_message_count": len(compact_messages),
        "messages": compact_messages,
    }
    return catalog, message_index


def render_master_index(
    catalog: dict[str, object],
    message_index: dict[str, object],
) -> str:
    embedded = json.dumps(
        {
            "catalog": catalog,
            "message_fields": message_index["message_fields"],
            "copy_fields": message_index["copy_fields"],
            "messages": message_index["messages"],
        },
        separators=(",", ":"),
    ).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iMessage Archive</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font: 15px/1.45 -apple-system, BlinkMacSystemFont, sans-serif;
       margin: 2rem auto; max-width: 1200px; padding: 0 1rem; }}
h1 {{ margin-bottom: .25rem; }}
.muted {{ opacity: .68; }}
.controls {{ display: grid; grid-template-columns: 2fr 1fr; gap: .75rem;
             margin: 1.5rem 0; }}
input, select {{ font: inherit; padding: .7rem; }}
.stats {{ display: flex; flex-wrap: wrap; gap: .5rem; margin: 1rem 0; }}
.badge {{ border: 1px solid #8886; border-radius: 999px; padding: .2rem .65rem; }}
.result {{ border-top: 1px solid #8885; padding: .8rem 0; }}
.meta {{ font-size: .88rem; opacity: .7; }}
.message-text {{ white-space: pre-wrap; }}
a {{ color: #1683ff; }}
@media (max-width: 700px) {{ .controls {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<h1>iMessage Archive</h1>
<p class="muted">Private, rebuildable index across verified timestamped exports.</p>
<div class="stats" id="stats"></div>
<div class="controls">
  <input id="query" type="search" placeholder="Search messages, senders, or conversations">
  <select id="conversation"><option value="">All conversations</option></select>
</div>
<p class="muted" id="summary"></p>
<div id="results"></div>
<script>
const DATA = {embedded};
const messages = DATA.messages;
const MF = Object.fromEntries(DATA.message_fields.map((name, index) => [name, index]));
const CF = Object.fromEntries(DATA.copy_fields.map((name, index) => [name, index]));
const stats = document.getElementById("stats");
stats.innerHTML = [
  `${{DATA.catalog.archive_count}} archives`,
  `${{DATA.catalog.remote_verified_archive_count}} remote verified`,
  `${{DATA.catalog.unique_message_count}} unique messages`,
  `${{DATA.catalog.conversation_count}} conversations`
].map(value => `<span class="badge">${{value}}</span>`).join("");
const conversation = document.getElementById("conversation");
for (const chat of DATA.catalog.conversations) {{
  const option = document.createElement("option");
  option.value = chat.guid;
  option.textContent = `${{chat.name}} (${{chat.unique_message_count}})`;
  conversation.appendChild(option);
}}
const query = document.getElementById("query");
const results = document.getElementById("results");
const summary = document.getElementById("summary");
const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, c => ({{
  "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
}}[c]));
const encodePath = value => String(value).split("/").map(encodeURIComponent).join("/");
function render() {{
  const needle = query.value.trim().toLocaleLowerCase();
  const chatGuid = conversation.value;
  const matches = [];
  for (let index = messages.length - 1; index >= 0; index--) {{
    const message = messages[index];
    const chats = message[MF.conversation_indexes].map(
      index => DATA.catalog.conversations[index]
    );
    if (chatGuid && !chats.some(chat => chat.guid === chatGuid)) continue;
    const haystack = [
      message[MF.text], message[MF.sender], ...chats.map(chat => chat.name)
    ].join(" ").toLocaleLowerCase();
    if (needle && !haystack.includes(needle)) continue;
    matches.push(message);
    if (matches.length === 500) break;
  }}
  summary.textContent = matches.length === 500
    ? "Showing the newest 500 matching messages."
    : `Showing ${{matches.length}} matching messages.`;
  results.innerHTML = matches.map(message => {{
    const copies = message[MF.copies];
    const copy = copies[message[MF.preferred_copy_index]];
    const archive = DATA.catalog.archives[copy[CF.archive_index]];
    const chats = message[MF.conversation_indexes].map(
      index => DATA.catalog.conversations[index]
    );
    const href = copy[CF.html_path]
      ? `${{encodeURIComponent(archive.name)}}/${{encodePath(copy[CF.html_path])}}`
      : `${{encodeURIComponent(archive.name)}}/Source%20Preservation/messages-not-rendered.html`;
    const chatNames = chats.map(chat => chat.name).join(", ");
    const attachment = copy[CF.attachment_count]
      ? `${{copy[CF.attachment_count]}} attachment row(s), ` +
        `${{copy[CF.unresolved_attachment_count]}} unresolved`
      : "No attachments";
    return `<article class="result">
      <div class="meta">${{escapeHTML(message[MF.timestamp] || "Unknown date")}} ·
        ${{escapeHTML(chatNames)}} · ${{escapeHTML(message[MF.sender] || "Unknown sender")}}</div>
      <div class="message-text">${{escapeHTML(message[MF.text] || "[Rich message or attachment]")}}</div>
      <div class="meta"><a href="${{href}}">Open conversation</a> ·
        ${{escapeHTML(archive.name)}} · ${{escapeHTML(attachment)}} ·
        ${{copies.length}} archived copy/copies</div>
    </article>`;
  }}).join("");
}}
let renderTimer;
query.addEventListener("input", () => {{
  clearTimeout(renderTimer);
  renderTimer = setTimeout(render, 150);
}});
conversation.addEventListener("change", render);
render();
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup_root", type=Path)
    parser.add_argument("--backup-prefix", default="imessage-backup")
    parser.add_argument(
        "--state-directory-name",
        default=".imessage-archive-state",
    )
    parser.add_argument(
        "--catalog-cache-directory-name",
        default="catalog-cache",
    )
    parser.add_argument("--index-name", default="index.html")
    parser.add_argument("--catalog-name", default="archive-catalog.json")
    parser.add_argument("--message-index-name", default="message-index.json")
    args = parser.parse_args()

    backup_root = args.backup_root.expanduser().resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    catalog, message_index = build_master_index(
        backup_root,
        args.backup_prefix,
        args.state_directory_name,
        args.catalog_cache_directory_name,
    )
    write_private_json(backup_root / args.catalog_name, catalog)
    write_private_compact_json(
        backup_root / args.message_index_name,
        message_index,
    )
    write_private_text(
        backup_root / args.index_name,
        render_master_index(catalog, message_index),
    )
    print(
        json.dumps(
            {
                "archive_count": catalog["archive_count"],
                "remote_verified_archive_count": catalog[
                    "remote_verified_archive_count"
                ],
                "unique_message_count": catalog["unique_message_count"],
                "conversation_count": catalog["conversation_count"],
                "index_path": str(backup_root / args.index_name),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
