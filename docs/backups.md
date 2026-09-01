# Backup and restore

## What must be backed up

At minimum, keep encrypted off-host copies of:

- `/srv/music` — the completed library;
- `/var/lib/music-agent/music-agent.db` — jobs, index, auth/session state, audit, and request history;
- `/etc/music-agent/music-agent.env` and `/etc/music-agent/credentials` — configuration and secrets (use stricter access/retention than media);
- `/var/lib/music-agent/artwork` when retaining cached artwork is useful;
- `/var/lib/music-agent/backups` and deployment manifests.

Git reproduces source code, not production data. `/srv/music-downloads` is normally disposable staging but may be retained temporarily for incident diagnosis.

## SQLite snapshots

Never copy a live SQLite file with plain `cp`. The backup script uses `sqlite3.Connection.backup` against the live database, requires rollback-journal mode, runs `PRAGMA integrity_check` on source and snapshot, fsyncs the file and containing directory, and then atomically publishes it. Each `.db` has a SHA-256 sidecar and JSON manifest containing schema revision, size, SQLite version, timestamp, and label. Verification cross-checks the digest, database size, journal mode, integrity result, and schema against that manifest.

```bash
sudo /opt/music-agent/current/scripts/backup.sh --label manual
sudo systemctl start music-agent-backup.service
```

The daily timer is persistent and randomized around 03:15. It does not prune snapshots because an incorrect retention rule is more dangerous than predictable growth. Monitor the backup filesystem and apply an externally reviewed retention/off-host policy.

Verify a snapshot without restoring it:

```bash
sudo /usr/bin/python3 /opt/music-agent/current/scripts/sqlite-maintenance.py verify \
  --require-checksum /var/lib/music-agent/backups/<file>.db
```

## Restore

Restore only from the managed backup directory:

```bash
sudo /opt/music-agent/current/scripts/restore.sh \
  --backup /var/lib/music-agent/backups/<file>.db
```

The script verifies the checksum and integrity first, makes a new safety backup, stops worker/web, refuses unexpected journal/WAL sidecars, atomically restores through SQLite's backup API, runs the current migration and validation commands, and starts services. If validation fails it reinstates the safety database.

Afterward, verify both services, authentication, recent jobs, and a library search. A database restore does not roll back `/srv/music`; reconcile the library with `sudo /opt/music-agent/current/scripts/music-agentctl.sh scan --full`. Restore media and configuration from their off-host backup independently when required.

For a release whose schema differs, use `rollback.sh --restore-backup`; its release manifest enforces the expected revision.
