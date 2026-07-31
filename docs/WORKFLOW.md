# Archive Lifecycle

`archive.yml` is the stable entry point for scheduled and interactive runs. It
composes independent playbooks so export, remote publication, indexing, and
pruning analysis can evolve without turning the lossless exporter into a
monolith.

The lifecycle never edits `chat.db`, removes files below
`~/Library/Messages`, or deletes a conversation. Optional local staging
retirement is isolated behind remote, catalog-cache, and restore-test
gates. Optional remote superseded retirement has its own semantic-containment
gate and never removes an archive containing unique history.

## Component order

| Stage | Playbook | Tag | Result |
| --- | --- | --- | --- |
| Lossless export | `playbook.yml` | `export` | A new verified timestamped archive |
| Archive publication | `nextcloud-archive.yml` | `nextcloud_archive` | An immutable remote copy and verification marker |
| Backlog repair | `nextcloud-backlog.yml` | `nextcloud_backlog` | Publication of every pending verified local archive |
| Catalog hydration | `catalog-hydrate.yml` | `catalog_hydrate` | A compact verified cache for each remote-only archive |
| Local retirement | `retire-local-archives.yml` | `local_retirement` | Optional removal of fully gated local staging directories |
| Remote superseded retirement | `retire-superseded-remote-archives.yml` | `remote_superseded_retirement` | Optional removal of an older archive fully represented later |
| Catalog rebuild | `catalog.yml` | `catalog` | Static full-text index and JSON catalogs |
| Pruning analysis | `pruning-report.yml` | `pruning` | Advisory JSON and HTML eligibility reports |
| Metadata publication | `nextcloud-metadata.yml` | `nextcloud_metadata` | Byte-verified remote copies of the current indexes and reports |

Run every enabled stage:

```sh
ansible-playbook archive.yml
```

Run or repeat one stage:

```sh
ansible-playbook archive.yml --tags catalog
ansible-playbook archive.yml --tags catalog_hydrate
ansible-playbook archive.yml --tags pruning
ansible-playbook archive.yml --tags nextcloud_backlog
ansible-playbook archive.yml --tags remote_superseded_retirement
```

The export component remains independently runnable:

```sh
ansible-playbook playbook.yml
```

When `nextcloud-archive.yml` is run outside the parent workflow, provide the
archive being processed:

```sh
ansible-playbook nextcloud-archive.yml \
  --extra-vars 'imessage_lifecycle_archive_path=/absolute/path/to/imessage-backup-RUN'
```

Use the same variable with `nextcloud-metadata.yml` to identify the remote
verification marker that belongs in the published metadata set.

## One-command activation

The only necessarily interactive step is creating the credentialed WebDAV
remote:

```sh
rclone config
rclone lsd nextcloud:
```

After that succeeds, activate the complete local deployment:

```sh
ansible-playbook activate-nextcloud.yml
```

`activate-nextcloud.yml` performs a preflight before changing local settings.
It requires the named rclone remote to exist, validates authentication by
listing its root, and selects the newest archive whose local
`verification.json` passed. It then:

1. writes only the non-secret remote name and rclone binary path to the ignored
   `local.yml`;
2. uploads and byte-verifies the selected existing archive;
3. publishes and verifies every other pending local archive;
4. rebuilds the master index and pruning report;
5. publishes and verifies the lifecycle metadata and all success markers; and
6. installs the recurring LaunchAgent.

Override the destination or suppress schedule installation when needed:

```sh
ansible-playbook activate-nextcloud.yml \
  --extra-vars 'imessage_activation_remote=nextcloud:Other/iMessage'

ansible-playbook activate-nextcloud.yml \
  --extra-vars 'imessage_activation_install_schedule=false'
```

No password, token, or app credential is written by the activation playbook.
Authentication remains in rclone's private configuration.

## Local archive invariants

Every export receives a unique run identifier. The exporter refuses to use an
existing destination, so a completed archive is never intentionally
overwritten. Local verification must succeed before the Nextcloud stage will
read or upload the directory.

Each archive contains portable conversation HTML, copied media, diagnostics,
source-preservation manifests, a consistent `chat.db` snapshot, and
verification reports. Known unavailable source attachments remain explicitly
represented instead of being silently treated as backed up.

## Nextcloud gate

Nextcloud publication is disabled until private configuration opts in:

```yaml
imessage_nextcloud_enabled: true
imessage_nextcloud_remote: "nextcloud:Backups/iMessage"
```

Configure the named WebDAV remote with `rclone config`; do not put passwords or
tokens in this repository. The archive stage performs:

1. `rclone copy --immutable` into a directory named after the unique archive.
2. `rclone check --download` against that exact directory.
3. A post-check inventory that includes hidden files.
4. A local verification marker only after the comparison exits successfully.

After the current archive passes, backlog repair selects every other locally
verified archive that lacks a successful marker for this exact remote root.
It applies the same immutable copy and byte comparison to each one. A later
scheduled run therefore repairs an interrupted older upload automatically.
Changing `imessage_nextcloud_remote` also causes retained archives to be
published and verified at the new destination.

`--download` streams both sides and compares their contents. It is intentionally
expensive because WebDAV does not always expose a common trustworthy checksum.
An interrupted upload, missing object, extra object, or byte difference prevents
the marker from being created and stops the lifecycle. A retry invalidates the
previous marker before checking, so stale success cannot survive a detected
remote mismatch.

Successful markers are stored under:

```text
.imessage-archive-state/remote-verifications/
```

The catalog trusts a marker only when it reports success and names the archive
being evaluated. A desktop Nextcloud sync status is not treated as equivalent
evidence.

After catalog and pruning generation, the metadata stage publishes an exact
allowlist of root artifacts and byte-compares those files as well. It never
uses a destructive remote sync.

## Master index

`catalog.yml` discovers directories matching `imessage_backup_prefix`, ignores
any archive whose local `verification.json` did not pass, and reads its
preserved `chat.db`, attachment manifest, and conversation HTML.

Messages are deduplicated by the stable `message.guid` value. Every physical
copy remains recorded, but the static index displays each logical message once.
The preferred conversation link is selected in this order:

1. attachment-complete copy;
2. remote-verified copy;
3. newest otherwise-equivalent copy.

Database text is indexed directly. When rich-message text is stored only in an
attributed body, the rendered exporter HTML supplies the searchable fallback.
The generated `index.html` embeds its private search data, so it works when
opened directly from a local or restored folder without a server.

The JSON message index uses self-describing columnar arrays to avoid repeating
field names hundreds of thousands of times. It is an implementation artifact;
`index.html` and `archive-catalog.json` are the human-facing entry points.
Every root index is rebuildable from the immutable archive directories.

When a full archive exists only in Nextcloud, the hydration component caches
its root conversation HTML, `verification.json`, preserved `chat.db`, and
attachment manifest under
`.imessage-archive-state/catalog-cache/ARCHIVE`. The cache is byte-compared
against the remote subset and structurally validated before a marker is
written. The catalog accepts a cache only while that marker still matches the
archive's exact remote-verification marker.

This makes the root index and overlap analysis durable when full local staging
directories are retired. Links continue to target the full archive basename,
which exists at the published Nextcloud root.

## Optional local staging retirement

The public default retains every full local archive. To reclaim staging space:

```yaml
imessage_local_retirement_enabled: true
imessage_local_retirement_keep_count: 0
imessage_local_retirement_require_restore_attestation: true
```

The local-retirement selector requires successful local and remote verification, a
matching compact catalog-cache marker, and—by default—at least one durable
successful restore attestation for the configured remote. It deletes only the
selected `imessage-backup-*` local directories. Catalog caches, indexes,
reports, recovery inputs, remote archives, and Messages are untouched by this
local-retirement stage.
Symlinked archive candidates and archives containing a configured recovery
source, source map, manifest, or device backup are explicitly blocked.

If any gate is missing, the archive remains local and the report records the
blocking reason. Set a positive keep count to retain that many newest eligible
full local archives.

## Single-copy history and pruning eligibility

The default policy requires:

```yaml
imessage_pruning_minimum_verified_copies: 1
imessage_pruning_safety_hours: 24
```

Remote-verified archives are ordered by creation time. With one required copy,
the candidate cutoff is the newest verified archive's maximum message
timestamp minus the safety interval. The interval keeps the recent boundary on
the Mac while older attachment-complete messages can be manually removed.

A message at or before the candidate cutoff is eligible only when:

- it is present in the newest remotely verified archive; and
- at least one remotely verified copy has no unresolved attachment rows.

The attachment condition matters when Messages shows an attachment download
button but the payload is not local. A later archive can make that message
eligible after the asset becomes available in one verified archive.

Enable nonredundant remote-history maintenance explicitly:

```yaml
imessage_remote_superseded_retirement_enabled: true
```

After compact caches are verified, the remote-retirement selector compares
stable message GUIDs and preserved attachment SHA-256 values. It deletes an
older remote archive and its marker only when later verified archives contain
all of both sets. Unique history is therefore appended as additional immutable
segments, while a whole older snapshot that is completely duplicated later is
removed. The generated decision record is
`.imessage-archive-state/remote-superseded-retirement.json`.

`pruning-report.json` contains eligible GUIDs and explicit blockers.
`pruning-report.html` provides a human-readable summary by conversation. If any
older message is blocked, `safe_contiguous_cutoff` remains null; the automation
does not claim that a bulk date-based deletion is safe.

No playbook performs deletion. Review the report and remove messages only
through supported Messages controls. Never modify the live Messages SQLite
database or remove files from its attachment store.

## Restore test

`restore-test.yml` is deliberately separate from the recurring archive
lifecycle because it downloads another full copy. By default it chooses the
newest local success marker matching the configured remote root:

```sh
ansible-playbook restore-test.yml
```

An explicit basename also works, including during disaster recovery when the
local marker directory is unavailable:

```sh
ansible-playbook restore-test.yml \
  --extra-vars 'imessage_restore_archive_name=imessage-backup-YYYYMMDDTHHMMSS'
```

Each invocation refuses to overwrite an existing destination. It downloads
from the immutable remote directory, byte-compares local and remote again,
checks HTML structure and media references, runs SQLite `quick_check`, and
re-hashes all attachment files named by the source-preservation manifest.
`RESTORE-TEST.json` records success inside the restored directory. A durable
copy and the `rclone check` report are also stored under the backup root at
`.imessage-archive-state/restore-verifications`. The restored copy remains
available for opening conversation HTML and sampling media manually by
default.

For an unattended restore test that releases the downloaded archive space
after all checks pass:

```sh
ansible-playbook restore-test.yml \
  --extra-vars 'imessage_restore_keep_copy=false'
```

Only the new restore-test destination and its transient check report are
removed. The verified Nextcloud archive and durable local attestation are
retained. Failed restore copies are never automatically deleted.

The restore preflight measures the remote object bytes and destination
filesystem capacity. By default it refuses to start unless the completed copy
would leave at least 20 GiB and 10% of the filesystem free, using the larger
reserve. This prevents an unattended validation run from filling the Mac.

## Failure behavior

- Export failure: no remote stage runs.
- Local verification failure: upload is refused.
- Remote upload or byte comparison failure: no success marker is written, so
  the archive cannot contribute to pruning eligibility.
- An older failed remote upload remains local and is retried by backlog repair
  during every later successful remote run.
- Catalog failure: existing immutable archives remain usable.
- Catalog hydration failure: the full local archive is retained and retirement
  cannot select it.
- Missing restore attestation: automatic local retirement reports the gate and
  deletes nothing.
- Insufficient remote history: pruning reports zero eligible messages.
- Unresolved attachment: only the affected messages remain blocked.
- Metadata publication failure: the remote archive remains valid, but the
  remote root index may be stale and the overall scheduled run fails.

The next scheduled run can safely retry. Retained timestamped archive
directories are immutable, semantic cleanup removes only whole superseded
directories, and root indexes are derived data.

## Scheduling and operations

Install the per-user LaunchAgent after private settings are correct:

```sh
ansible-playbook install-schedule.yml
```

The generated wrapper calls `archive.yml`. Force it interactively from the
same executable context to confirm Full Disk Access and rclone authentication:

```sh
~/.local/bin/ansible-imessage-backup --force
```

The default calendar is Sunday at 03:00 (`Weekday=7`). A sleeping Mac receives
the calendar event after wake. `RunAtLoad` covers login after a powered-off
interval, while a 144-hour minimum-success interval prevents every login from
creating a backup. `--force` bypasses only that age check; it does not bypass
the single-instance lock or any archive verification gate.

The wrapper acquires an atomic per-user lock before launching Ansible. A
second manual or scheduled invocation records `skipped_already_running` and
exits without creating another export. Per-run logs are retained for 90 days
by default, and JSON status records are written to
`~/Library/Application Support/ansible-imessage-backup/`.

The wrapper writes `last-run.json` immediately after acquiring the lock with a
`running` outcome and null completion fields. It atomically replaces that
record on success, failure, or a handled signal. Lock contention is recorded
separately in `last-skipped.json`, so a second invocation cannot hide the
active run from the status report.

Use the read-only status playbook to see archive counts, remote markers,
pending uploads, configured remotes, schedule state, generated indexes, and
the most recent scheduler result:

```sh
ansible-playbook status.yml
```

Recommended operating sequence:

1. Complete a scheduled run and confirm its remote verification marker exists.
2. Restore the archive and open its HTML from a separate temporary directory.
3. Review `pruning-report.html`.
4. Manually prune only the approved range or messages.
5. Optionally enable gated local staging retirement and superseded remote
   retirement.
6. Let later runs append unique history segments; semantic containment cleanup
   removes only whole segments that add nothing unique.

Periodically repeat the restore test. A green upload check proves remote bytes
matched at the recorded time; a restore test proves the archive remains useful
to a person.
