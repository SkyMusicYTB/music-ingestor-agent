# Architecture and data model

## Process boundary

The web service owns operator-facing HTTP, authentication, request validation, OpenAI orchestration, candidate confirmation, and durable enqueueing. The worker owns acquisition and filesystem mutation. OpenAI receives structured tool schemas and bounded results; it never receives a shell, arbitrary SQL, an unrestricted HTTP client, or a filesystem primitive. Python remains authoritative for policy decisions.

A typical request moves through these states:

1. Authenticate, enforce origin/CSRF rules, parse the bounded request, and create an audit record.
2. Search the local SQLite index before remote discovery.
3. Use allowlisted metadata/search clients and validate candidate identifiers and URLs.
4. Present ambiguous or multi-track results for confirmation; exact single-track requests may follow configured policy.
5. Insert idempotent jobs into SQLite.
6. A worker atomically leases one job, resolves permitted media, and downloads to a unique staging directory.
7. ffprobe, metadata rules, Mutagen readback, duplicate checks, and free-space checks gate publication.
8. Rename a completed file onto the same `/srv/music` filesystem. Record the final identity and audit outcome.
9. Navidrome discovers the file on its normal periodic scan.

## SQLite concurrency and migrations

SQLite is sufficient for one web process and a small bounded worker pool. Every process uses independent connections (`NullPool`), foreign keys, `busy_timeout=10000`, rollback journal `DELETE`, and `synchronous=FULL`. Transactions are short. Network calls, OpenAI requests, yt-dlp, ffmpeg, and tag writes must never occur inside a database transaction. Queue claims use one conditional atomic update; leases expire so work can recover after a crash. Idempotency and unique constraints prevent duplicate completion.

WAL is intentionally not used. Ubuntu 26.04 currently packages SQLite 3.46.1, a version in the affected range for SQLite's documented WAL-reset corruption bug; upstream's fixed versions are 3.51.3, 3.50.7, and 3.44.6. Rollback journal avoids that code path and removes a custom SQLite runtime from the deployment. Sources: [SQLite WAL advisory](https://www.sqlite.org/wal.html#the_wal_reset_bug), [Ubuntu 26.04 sqlite3 package](https://packages.ubuntu.com/resolute/sqlite3), and [Ubuntu package changelog](https://lists.ubuntu.com/archives/resolute-changes/2026-July/028772.html).

Alembic owns schema changes. Deployment stops both writers, makes and verifies a consistent backup with Python's SQLite backup API, runs `music-agent migrate` from the staged release, then switches code. Each release manifest records its expected schema and paired pre-deployment backup. A rollback with a schema mismatch refuses to proceed without an explicitly selected matching backup.

## Persistent and reproducible data

Mutable state never lives in a release:

- `/var/lib/music-agent/music-agent.db`: jobs, local library truth, authentication state, sessions, audit/cost data, and Alembic revision.
- `/var/lib/music-agent/artwork`: cached or retained artwork.
- `/var/lib/music-agent/backups`: verified SQLite snapshots, checksums, and JSON manifests.
- `/srv/music-downloads`: disposable per-job staging (retain while diagnosing failures).
- `/srv/music`: completed media; Navidrome remains authoritative for playback state.
- `/etc/music-agent`: root-managed configuration and credentials.

The initial scan gates publication. Completed imports are indexed immediately, and the worker schedules an idempotent incremental reconciliation every 30 minutes. Incremental scans stat the library but reread embedded tags only when a file's size or modification time changed.

Application releases, virtual environments, Deno, and yt-dlp are reproducible from Git and their pinned inputs.
