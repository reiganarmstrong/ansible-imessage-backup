# Ansible iMessage Backup

Create timestamped, verified, private iMessage HTML archives on macOS with
[`imessage-exporter`](https://github.com/ReagentX/imessage-exporter) and
Ansible. Archives can be uploaded and byte-verified in Nextcloud, and an
optional LaunchAgent can run the complete lifecycle every week.

The repository contains automation only. It does **not** contain or commit
messages, contacts, attachments, recovered media, or machine-specific paths.

## What it does

- Exports every conversation as portable HTML.
- Copies attachments in their original formats by default.
- Preserves HEIC originals while creating validated JPEG viewing copies.
- Creates a new timestamped directory and refuses to overwrite an old backup.
- Saves `imessage-exporter --diagnostics` with each archive.
- Creates a consistent `chat.db` snapshot so exporter-omitted event and
  metadata rows remain recoverable.
- Hash-verifies and preserves every attachment currently readable from the
  Messages attachment store, including hidden iMessage app payloads.
- Adds browser indexes for unavailable attachments and database rows that the
  exporter does not render.
- Reconnects locally available visible assets omitted from conversation HTML.
- Detects broken local HTML references, empty files, symlinks, and incomplete
  HTML output.
- Optionally preserves a directory of separately recovered attachments.
- Optionally reconnects known recovered files to their original message HTML.
- Builds a static, full-text master index across every verified archive.
- Deduplicates overlapping messages by their stable Messages GUID.
- Optionally uploads immutable archives to Nextcloud through `rclone`.
- Automatically retries locally verified archives missing from the selected
  Nextcloud destination.
- Byte-compares local and remote data before recording remote success.
- Hydrates compact, byte-verified catalog data for remote-only archives so
  search and overlap analysis survive local staging cleanup.
- Can optionally retire full local staging archives after remote, catalog, and
  restore-test gates pass.
- Can optionally retire a remote archive only after later verified catalogs
  semantically subsume all of its messages and preserved attachments.
- Produces a conservative, non-destructive safe-pruning eligibility report.
- Optionally installs a weekly per-user macOS LaunchAgent.

## Requirements

- macOS with Messages configured
- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/intro_installation.html)
- [imessage-exporter](https://github.com/ReagentX/imessage-exporter)
- Full Disk Access for the executable context running the backup
- [rclone](https://rclone.org/webdav/) for verified Nextcloud publication

Install the command-line dependencies with Homebrew:

```sh
brew install ansible imessage-exporter libheif rclone
```

The upstream exporter recommends Cargo for obtaining its newest release:

```sh
cargo install imessage-exporter
```

Grant Full Disk Access in **System Settings → Privacy & Security → Full Disk
Access**. Test an interactive export before relying on the scheduled job.

## Quick start

```sh
git clone https://github.com/YOUR-USER/ansible-imessage-backup.git
cd ansible-imessage-backup
ansible-playbook archive.yml
```

The default destination is:

```text
~/Documents/Backups/iMessage/imessage-backup-YYYYMMDDTHHMMSS/
```

Each run produces a new directory. By default it never deletes or rotates older
backups. The separately gated local-retirement option is described below.

## Composable playbooks

`archive.yml` is the stable orchestration entry point. It imports the verified
export from `playbook.yml` as a component:

```yaml
---
- name: Create a verified iMessage export
  ansible.builtin.import_playbook: playbook.yml
  tags:
    - export
```

Run the full archive workflow:

```sh
ansible-playbook archive.yml
```

Run only the export component:

```sh
ansible-playbook archive.yml --tags export
```

The component remains independently runnable with
`ansible-playbook playbook.yml`. The complete lifecycle is:

```text
verified export
→ immutable Nextcloud upload and byte verification (optional)
→ repair of any older verified local archives missing from that remote
→ compact catalog hydration for remote-only archives
→ optional gated retirement of full local staging archives
→ optional retirement of fully superseded remote archives
→ master index rebuild
→ safe-pruning eligibility report
→ index/report publication and byte verification (optional)
```

Each stage is independently runnable and tagged. See
[`docs/WORKFLOW.md`](docs/WORKFLOW.md) for component commands, state files,
failure behavior, overlap policy, and restoration guidance. The scheduled
LaunchAgent uses `archive.yml`.

## Nextcloud publication

Copy the example settings and edit the private file:

```sh
cp local.yml.example local.yml
```

The recommended mode keeps a local staging directory and uses an `rclone`
WebDAV remote for an explicit upload plus byte-level verification. Configure
the remote interactively on the Mac running this playbook:

```sh
rclone config
rclone lsd nextcloud:
```

The Homebrew build is sufficient. This project does not use `rclone mount`;
it uses ordinary WebDAV listing, copying, and byte-comparison commands.
Installing rclone only on the Nextcloud server does not enable this
Mac-originated push workflow.

Then set the private variables:

```yaml
---
imessage_backup_root: "{{ lookup('ansible.builtin.env', 'HOME') }}/Documents/Backups/iMessage"
imessage_nextcloud_enabled: true
imessage_nextcloud_remote: "nextcloud:Backups/iMessage"
```

Alternatively, after `rclone lsd nextcloud:` succeeds, activate everything in
one command:

```sh
ansible-playbook activate-nextcloud.yml
```

The activation workflow selects the newest locally verified archive, persists
only the non-secret rclone binary and remote path in `local.yml`, uploads and
byte-verifies that archive, rebuilds and publishes the indexes and pruning
report, and installs the weekly LaunchAgent. It never copies the rclone
password into this repository or `local.yml`.

The workflow publishes each timestamped archive with `rclone copy --immutable`.
It then runs `rclone check --download`, which reads both local and remote bytes
and fails on any missing, extra, or different file. This is stronger—and more
bandwidth-intensive—than trusting a desktop client's “synced” indicator. A
retained archive is never edited in place; optional semantic cleanup can remove
the whole directory only after later verified archives fully subsume it. Root
catalog files are mutable and are republished after each successful run.

Every lifecycle run also scans for older locally verified archives without a
valid marker for the configured destination. Those archives are uploaded and
checked with the same immutable gate. This repairs interruptions automatically
and ensures a remote-path change republishes the complete retained history.

`local.yml` and rclone's credential file are not committed. Do not place
exported messages or credentials inside this repository.

## Compact remote catalog and local staging retirement

The master index needs conversation HTML, the preserved `chat.db`, the
attachment manifest, and the archive verification report—but not another full
copy of every attachment. For a verified archive no longer present locally,
`catalog-hydrate.yml` downloads only those files into:

```text
.imessage-archive-state/catalog-cache/ARCHIVE/
```

It performs a filtered `rclone check --download`, SQLite `quick_check`, and
manifest validation before trusting the cache. A cache is tied to the exact
remote verification marker and is rebuilt if that marker changes.

Full local staging retirement is opt-in and disabled by default:

```yaml
imessage_local_retirement_enabled: true
imessage_local_retirement_keep_count: 0
imessage_local_retirement_require_restore_attestation: true
```

Before deleting a full local archive, the lifecycle requires:

1. a passing local export report;
2. an exact verification marker for the configured Nextcloud destination;
3. a byte-verified compact catalog cache; and
4. by default, at least one successful restore-test attestation for that
   remote.

Local retirement deletes only timestamped local staging directories. It never
changes Messages. Use
`make retire-local` to reevaluate the gates without creating a new export.
An archive is also refused when it is a symlink or contains any configured
recovery input, preventing cleanup from following paths outside the staging
root or deleting data needed by later runs.

A local Nextcloud-synchronized directory can still be used as
`imessage_backup_root`, but the repository cannot independently prove that the
desktop client uploaded it. Such archives do not receive remote-verification
markers and are never considered safe for pruning.

You can also override a setting for one run:

```sh
ansible-playbook playbook.yml \
  --extra-vars 'imessage_backup_root=/Volumes/Archive/iMessage'
```

## Master index and overlap policy

The lifecycle writes these rebuildable files in `imessage_backup_root`:

```text
index.html
archive-catalog.json
message-index.json
pruning-report.html
pruning-report.json
.imessage-archive-state/
```

`index.html` searches message text, senders, and conversations without a web
server. Overlapping messages appear once because copies share a stable message
GUID. The preferred link favors attachment-complete data, then remote
verification and recency. Compact cache rows keep remote-only archives in this
index after their full local staging directories are retired.

The pruning report never changes Messages. The default single-copy policy lists
a message only after one attachment-complete, remotely verified copy exists and
the message is at least 24 hours behind that archive's newest message. Any
unresolved attachment blocks that message.

Optional superseded-archive retirement keeps remote storage from accumulating
whole duplicate snapshots. When enabled, it permanently removes an older
archive only if the union of later verified compact catalogs contains every
message GUID and every preserved attachment digest from the older archive. An
archive with even one unique message or preserved asset remains as an immutable
history segment. Run `make retire-remote-superseded` to reevaluate this gate.

## Restore testing

After Nextcloud activation, restore and independently validate the newest
remotely verified archive:

```sh
make restore-test
```

Select a specific archive when testing older history:

```sh
ansible-playbook restore-test.yml \
  --extra-vars 'imessage_restore_archive_name=imessage-backup-YYYYMMDDTHHMMSS'
```

The playbook always creates a new private directory below
`~/Documents/Backups/iMessage-Restore-Tests`. It downloads the remote archive,
runs another `rclone check --download`, validates the HTML and all local media
references, checks the SQLite snapshot, and re-hashes every attachment covered
by the preservation manifest. It writes `RESTORE-TEST.json` only after all
checks pass, saves a durable copy and the byte-comparison report below
`.imessage-archive-state/restore-verifications`, and never changes or deletes
the remote archive.

By default, the restored copy remains available for manual inspection. For an
automated test that gives the disk space back after every check passes:

```sh
ansible-playbook restore-test.yml \
  --extra-vars 'imessage_restore_keep_copy=false'
```

Cleanup never runs after a failed restore, so the partial copy and diagnostic
report remain available for investigation.

Before downloading, it measures the remote archive and refuses a restore that
would leave less than 20 GiB or 10% of the destination filesystem free,
whichever reserve is larger. Override those private settings only after
deliberately choosing another destination or accepting the disk-space impact:

```yaml
imessage_restore_minimum_free_bytes_after: 21474836480
imessage_restore_minimum_free_percent_after: 10
```

## Attachment behavior

The default copy method is `clone`, which preserves the source files without
conversion. Other supported values are `basic`, `full`, and `disabled`:

```yaml
imessage_copy_method: "clone"
```

For HTML exports, the playbook uses `heif-convert` from Homebrew's `libheif` to
create JPEG viewing copies in parallel while retaining the originals. If
`heif-convert` is unavailable, it falls back to macOS Quick Look and `sips`.
It checks that each JPEG has real pixel dimensions before changing the HTML.

```yaml
imessage_create_heic_previews: true
imessage_heic_preview_quality: "high"
imessage_heic_preview_workers: 8
```

This avoids relying on `imessage-exporter`'s direct HEIC conversion when the
installed macOS `sips` decoder produces a metadata-only JPEG. Disable the
preview step if preserving disk space matters more than browser display.

`full` conversion requires `ffmpeg` for video conversion. The exporter can only
copy attachment data that is currently accessible in the Messages attachment
store. A filename or download button in Messages does not guarantee that the
payload is still available.

By default, a source-preservation pass follows the export. It creates:

```text
Source Preservation/
├── attachment-manifest.json
├── attachments/
├── chat.db
├── index.html
├── messages-not-rendered.html
└── unavailable-attachments.html
source-preservation.json
source-preservation-verification.json
```

The manifest records the source path, SHA-256 digest, archived path, message
relationships, and coverage state for every attachment row. The independent
verifier re-hashes both the archive and live source and runs SQLite
`PRAGMA quick_check`; a mismatch fails the playbook. Hidden
`.pluginPayloadAttachment` files are retained for lossless preservation but
are not injected into conversation HTML.

Disable this layer only when a smaller, non-lossless export is intentional:

```yaml
imessage_preserve_source_data: false
```

To preserve files recovered separately from the Messages database:

```yaml
imessage_recovered_assets_source: "/absolute/path/to/Recovered Attachments"
```

They will be copied into each new archive. If a recovered file still has a
message relationship, copy `recovery-manifest.json.example` to the ignored
`recovery-manifest.json`, fill in its values, and enable it:

```yaml
imessage_recovery_manifest: "{{ playbook_dir }}/recovery-manifest.json"
```

Each manifest entry identifies the original message and missing filename:

```json
{
  "recoveries": [
    {
      "message_guid": "00000000-0000-0000-0000-000000000000",
      "missing_filename": "original-name.mov",
      "recovered_file": "recovered-copy.mov"
    }
  ]
}
```

The patcher fails safely if the message, missing-attachment marker, or recovered
file cannot be found. Orphan records still cannot be reconnected because the
database no longer identifies their message.

For recovered files corresponding to one or more attachment database rows,
copy `recovery-source-map.json.example`, record the row IDs and SHA-256 digest,
and set:

```yaml
imessage_recovered_source_map: "/absolute/path/to/recovery-source-map.json"
```

The map uses archive-relative paths after recovered assets have been copied:

```json
{
  "recoveries": [
    {
      "attachment_row_ids": [123, 124],
      "archive_path": "Recovered Attachments/recovered-photo.heic",
      "sha256": "the-files-sha256"
    }
  ]
}
```

This keeps separately recovered rows distinct from files still readable at
their live Messages paths, so reports identify genuinely unrecovered records.

### Recover from a local Apple device backup

An unencrypted Finder/iTunes iPhone or iPad backup can contain attachments that
are absent from the Mac. Configure its directory in private `local.yml`:

```yaml
imessage_ios_backup_path: >-
  {{ lookup('ansible.builtin.env', 'HOME')
     ~ "/Library/Application Support/MobileSync/Backup/device-id" }}
imessage_recover_ios_device_variants: false
imessage_preserve_ios_hidden_counterparts: true
```

The recovery pass requires the same message GUID and transfer name on both
devices. By default it maps only files whose physical byte count also matches
the Mac attachment record. Enabling `imessage_recover_ios_device_variants`
also preserves same-message assets encoded differently on the device; reports
label those as variants rather than byte-identical originals.

Hidden `.pluginPayloadAttachment` data is device-specific. When enabled, the
playbook preserves backed-up iOS counterparts for affected messages in a
separate, SHA-256-indexed directory without claiming that they are the missing
Mac row bytes. The independent verifier checks those counterpart hashes.

The recovery tool can also be audited manually before applying changes:

```bash
python3 scripts/recover_from_ios_backup.py \
  /absolute/path/to/export \
  "/absolute/path/to/MobileSync/Backup/device-id"
```

For attachments still unavailable after local and device-backup recovery,
`mmcs-recovery-audit.json` reports whether the database retains complete Apple
MMCS download metadata. Private URLs, owner tokens, signatures, and decryption
keys are deliberately redacted from that report. These fields are the metadata
Messages uses for its attachment download control; their presence does not
prove that Apple still retains the encrypted payload.

The audit does not download from MMCS. A current Messages client first requests
authenticated download authorization through the signed-in Messages/APNS
service, then retrieves the encrypted object from the Apple content host and
decrypts it locally. A direct HTTP request to the stored URL is therefore not a
correct general recovery workflow. Using the download control also sends the
private object metadata back to Apple, so this playbook leaves that action
explicit and interactive. After Messages successfully downloads an attachment,
run the playbook again; the source-preservation pass will pick up and verify the
newly local file.

If a download is copied into the export manually, record it in
`Recovered Attachments/recovery-source-map.json` with
`"recovery_kind": "messages_authenticated_download"`. The audit then lists the
recovered row IDs and sets `network_recovery_performed` to `true`, while
continuing to redact all private Apple download metadata.

An interactive recovery pass can also write
`authenticated-messages-recovery-attempt.json` in the export. Each record
contains only an attachment row ID, whether its Messages UI control was
exercised, and a result label. The MMCS audit summarizes attempted and
not-attemptable rows so a filename-only placeholder is not mistaken for a
working Apple download.

The MMCS audit also classifies unavailable database paths without publishing
the paths themselves. In particular, `imagent_temporary` identifies attachments
whose only recorded source was below macOS's temporary `com.apple.imagent`
directory. Those files can disappear when the operating system clears
temporary data even when Messages in iCloud is disabled; the source
preservation pass copies them whenever they are still readable.

By default, a broken local media reference fails verification. If source
diagnostics document known missing attachments that cannot be recovered, they
can instead be retained as warnings:

```yaml
imessage_allow_broken_references: true
```

The optional `style.css` reference emitted for custom styling is not considered
broken when no custom stylesheet has been supplied.

Additional exporter flags can be supplied as a YAML list:

```yaml
imessage_additional_args:
  - "--use-caller-id"
```

Do not put an encrypted iOS backup password in this file or on the command line.
The exporter warns that `--cleartext-password` is visible in process listings
and shell history.

## Weekly scheduling

The optional schedule runs Sundays at 03:00:

```sh
ansible-playbook install-schedule.yml
```

Change the schedule in `local.yml` before installing it:

```yaml
imessage_schedule_weekday: 1
imessage_schedule_hour: 3
imessage_schedule_minute: 0
```

Launchd weekday values run from `1` (Sunday) through `7` (Saturday). Logs are
written to:

```text
~/Library/Logs/ansible-imessage-backup/
```

The wrapper uses an atomic single-instance lock, so a manual wrapper invocation
cannot overlap a LaunchAgent invocation of the same wrapper. Each run gets a
private log, and logs older than 90 days are removed by default.
Machine-readable last-run and last-success records are stored under:

```text
~/Library/Application Support/ansible-imessage-backup/
```

`last-run.json` is written with `outcome: running` as soon as the wrapper
acquires its lock, then atomically replaced with success, failure, or
interruption details. A concurrent invocation leaves the active record intact
and writes `last-skipped.json` instead.

Inspect the complete local operational state at any time:

```sh
make status
```

Run the generated wrapper interactively once to confirm Full Disk Access:

```sh
~/.local/bin/ansible-imessage-backup
```

Remove the schedule without deleting any backups or logs:

```sh
ansible-playbook uninstall-schedule.yml
```

## Validation

Run all local checks:

```sh
make check
```

GitHub Actions syntax-checks each playbook and tests the standard-library
export, recovery, indexing, and pruning scripts. The workflow never accesses
real Messages data.

## Security

iMessage exports are plaintext collections of private conversations, phone
numbers, email addresses, and media. Restrict local and Nextcloud access, enable
encryption and multifactor authentication, and never commit an export.

This project performs read-only access to the Messages database. It creates new
backup directories and can append them to a configured Nextcloud destination,
but it does not modify Messages, delete old backups, or manage Nextcloud
retention. The preserved database, full-text master index, and internal app
payloads may contain more private metadata than the rendered HTML; protect the
entire backup directory accordingly.

## License

[MIT](LICENSE)
