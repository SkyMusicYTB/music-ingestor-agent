# Production update: accounts, model budgets, history and library

This release upgrades schema 0002 through **0003** to **0004**. It does not move,
retag, rename or delete media, recreate accounts, change Navidrome, or overwrite
an existing environment file. Existing IDs, password hashes and sessions survive
the upgrade. One existing account becomes administrator; if unexpected additional
accounts exist, the oldest `(created_at, id)` becomes admin and the others users.
An inactive account is never silently activated. Deployment refuses a nonempty
database without an active administrator and prints an explicit recovery command.

## Model budgets

New installations use `MUSIC_AGENT_MAX_MODEL_ROUNDS=10`,
`MUSIC_AGENT_OPENAI_MAX_TOOL_CALLS=10` and `MUSIC_AGENT_MAX_AGENT_SECONDS=120`.
Rounds and built-in calls independently accept 1–50 (an application safety policy);
the deadline accepts 10–600 seconds. The model remains configurable.

Existing `MUSIC_AGENT_MAX_AGENT_STEPS=50` works unchanged. If both legacy and new
round settings are supplied they must agree, or database-free preflight fails.
Settings and authorized Usage/Health show the effective values and source.
The legacy constructor and read-only property also remain supported.

Every discovery Responses call consumes a round, including repair and web-search
recovery. The final round cannot use tools. Repeated no-progress calls or an
approaching deadline trigger earlier synthesis. Locally validated partial proposals
survive later provider failures as degraded previews; they cannot auto-queue.
Unreported usage is unknown, not free. Canonical adjudication and local tools share
the overall deadline; auxiliary calls are recorded separately from discovery rounds.

OpenAI's `max_tool_calls` caps built-in tools within a response, not application
rounds or local function calls. See the [Responses reference](https://developers.openai.com/api/reference/python/resources/responses/methods/create).

## Downloads and privacy

Downloads is owner-only, including for admins. All visible, Active, Needs attention,
Finished and Hidden history views support 25/50/100 rows. Active and attention work
sort before finished history. “Clear finished” requires confirmation and hides only
completed, failed and cancelled jobs. No music, request, provenance, accounting or
review data is deleted. Restore brings back one card. Retrying a hidden failed job
restores and queues it atomically. Reviews and filters survive live fragment updates.

Requests, conversations, reviews, jobs and ordinary usage stay private to their owner.
The music library is shared. Admins can explicitly view all/system/grouped usage,
account-management audits, scan diagnostics, Settings and detailed Health. Legacy
usage without provable ownership remains system/unattributed; legacy events without
provable ownership are admin-only. SSE rechecks live sessions and never extends idle
expiry simply because a tab remains open.

## Local accounts

Use **Users** to create a standard account. Generated temporary passwords are shown
once, never stored as plaintext, and default to forced change on first login.
Passwords have a 12-character minimum. Usernames are immutable. Admin creation,
role changes, password reset, deactivation and session revocation require explicit
password reauthentication within five minutes; use does not extend the window.
Self-demotion/deactivation is blocked, as is removing the last active admin.

Account lets anyone change their password and revoke other sessions. Password
changes rotate the current session/CSRF and revoke other sessions. Disabled users
lose access immediately, but their approved downloads finish and their music/history
remain. Jobs needing their review wait. Reactivation does not revive old sessions.
There is no registration, email recovery, impersonation or hard delete.

Local recovery, only if actually needed:

```bash
sudo /opt/music-agent/current/scripts/music-agentctl.sh user-list
sudo /opt/music-agent/current/scripts/music-agentctl.sh admin-reset --username NAME
# Explicitly promote/reactivate an existing standard or disabled target:
sudo /opt/music-agent/current/scripts/music-agentctl.sh admin-reset --username NAME --recover
```

Passwords are entered without echo in a TTY, never in command arguments. Omitting
the name is allowed only when exactly one active admin is unambiguous. Recovery
preserves the selected identity and revokes only that account's sessions. Do not
reset the existing admin as part of a normal update.

## Mixed-format library and diagnostics

The shared registry recognizes case-insensitive extensions:

| Extensions | Read-only audio families |
| --- | --- |
| mp3 | MP3 |
| m4a, mp4, m4b | AAC, ALAC |
| flac | FLAC |
| ogg, oga | Vorbis, Opus, Ogg FLAC |
| opus | Ogg Opus |
| wav | Integer/floating PCM |
| aac | ADTS AAC |
| aif, aiff, aifc | AIFF/AIFF-C PCM |
| wma, asf | WMAv2 |
| wv | WavPack |
| webm | Audio-only Opus/Vorbis |
| mka | Audio-only AAC, ALAC, FLAC, MP3, Opus, Vorbis, PCM |

APE, MPC and other codecs remain explicitly unsupported. Read-only indexing does
not imply a tag-writing adapter or change acquisition's format policy. Mutagen reads
tags first; a bounded, local-only ffprobe verifies missing technical information and
video-capable containers. Ordinary video and ambiguous multiple audio tracks are
rejected. Attached JPEG/PNG covers and a unique default audio track are allowed.
Existing media is not subject to acquisition's 30-minute cap.

Parser-version changes reread unchanged files once. Later incremental scans retain
the size/mtime optimization. Full and immediate publication indexing use the same
reader. Missing reconciliation requires proven absence and unchanged indexed state;
an unreadable subtree cannot falsely remove the library. Scan leases serialize full
and incremental runs without blocking immediate indexing. Duplicate source tags in
copies become provenance aliases, not arbitrary job-recovery targets.

Library supports album/text search, extension/codec filters, present/missing/all and
stable artist/album/disc/track/title order. Admins see scan status, counters, bounded
reason samples and scan history through `/api/v1/library/scans`. Audit is read-only:

```bash
sudo /opt/music-agent/current/scripts/music-agentctl.sh library-audit --json --verbose --limit 100
sudo /opt/music-agent/current/scripts/music-agentctl.sh scan --full
sudo /opt/music-agent/current/scripts/music-agentctl.sh library-audit --json --verbose --limit 100
```

The wrapper runs the actual reader as `music-agent`, not root, without web credentials.
Audit never changes media or indexed rows. Samples show sanitized relative paths
(300 characters), not absolute paths or raw parser errors. Scan summaries distinguish
found, indexed, new/updated/unchanged, missing, rejected and metadata fallback counts.
A completed legacy initial scan remains a baseline. Fresh acquisition requires complete
coverage; a later incomplete scan reports degraded coverage without erasing it.
Initial indexing gates acquisition/publication, not web readiness: the browser must
remain available to show a large first scan's progress. Web readiness checks the
current SQLite schema/journal, writable web state/artwork, and readable music. It
does not demand write access to the worker's sandboxed download directory.

Music Agent support is not a promise that every Navidrome version can discover/play
each format. See [Navidrome's format declarations](https://raw.githubusercontent.com/navidrome/navidrome/master/resources/mime_types.yaml),
[Mutagen readers](https://mutagen.readthedocs.io/en/latest/), and [ffprobe](https://ffmpeg.org/ffprobe.html).

## Safe production reconciliation and rollout

Run the following in **bash as `skymusic`**, not a root login. Set `delivered_sha` to
the exact 40-character SHA in the delivery message. Verify the old checkout path
before proceeding; `/home/skymusic/src/music-agent` below is an explicit example.
Stop editing that checkout while preserving it. Never use `sudo git`, hard reset,
force push, a release-directory patch or `--allow-dirty` for this rollout.

```bash
set -euo pipefail
umask 077
delivered_sha='REPLACE_WITH_DELIVERED_40_CHARACTER_SHA'
old_checkout='/home/skymusic/src/music-agent'
[[ "$(id -un)" == skymusic && "$delivered_sha" =~ ^[0-9a-f]{40}$ ]]
[[ -d "$old_checkout/.git" && ! -L "$old_checkout" && ! -L "$old_checkout/.git" ]]
[[ "$(realpath "$old_checkout")" == "$old_checkout" ]]
[[ "$(stat -c %U "$old_checkout")" == skymusic ]]
[[ "$(stat -c %U "$old_checkout/.git")" == skymusic ]]
[[ -z "$(find "$old_checkout/.git" -maxdepth 0 -perm /022 -print -quit)" ]]
[[ -f "$old_checkout/.git/index" && ! -L "$old_checkout/.git/index" ]]
[[ -z "$(find "$old_checkout/.git" -name '*.lock' -print -quit)" ]]
index_owner="$(stat -c %U "$old_checkout/.git/index")"
[[ "$index_owner" == root || "$index_owner" == skymusic ]]
[[ -z "$(find "$old_checkout/.git/index" -perm /022 -print -quit)" ]]

update_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive_dir="$(mktemp -d /home/skymusic/music-agent-update.XXXXXX)"
stat -c '%u:%g:%a:%s:%y' "$old_checkout/.git/index" > "$archive_dir/index.before.stat"
sudo sha256sum "$old_checkout/.git/index" > "$archive_dir/index.before.sha256"
sudo install -o skymusic -g "$(id -gn)" -m 0600 \
  "$old_checkout/.git/index" "$archive_dir/readable-index"

snapshot_git() {
  env -i PATH=/usr/bin:/bin GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    GIT_OPTIONAL_LOCKS=0 GIT_NO_REPLACE_OBJECTS=1 GIT_TERMINAL_PROMPT=0 \
    GIT_INDEX_FILE="$archive_dir/readable-index" \
    git -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "$old_checkout" "$@"
}
snapshot_git rev-parse HEAD > "$archive_dir/HEAD"
snapshot_git status --porcelain=v2 --branch > "$archive_dir/status.txt"
snapshot_git diff --no-ext-diff --no-textconv --binary > "$archive_dir/unstaged.patch"
snapshot_git diff --cached --no-ext-diff --no-textconv --binary > "$archive_dir/staged.patch"
snapshot_git ls-files --others --exclude-standard -z > "$archive_dir/untracked.nul"
snapshot_git ls-files --others --ignored --exclude-standard -z > "$archive_dir/ignored.nul"
sudo tar --acls --xattrs -cpf "$archive_dir/complete-old-checkout.tar" \
  -C "$(dirname "$old_checkout")" "$(basename "$old_checkout")"
sudo chmod 0600 "$archive_dir/complete-old-checkout.tar"
sudo sha256sum "$archive_dir/complete-old-checkout.tar" > "$archive_dir/checkout.sha256"
stat -c '%u:%g:%a:%s:%y' "$old_checkout/.git/index" > "$archive_dir/index.after.stat"
sudo sha256sum "$old_checkout/.git/index" > "$archive_dir/index.after.sha256"
cmp "$archive_dir/index.before.stat" "$archive_dir/index.after.stat"
cmp "$archive_dir/index.before.sha256" "$archive_dir/index.after.sha256"
[[ -z "$(find "$old_checkout/.git" -name '*.lock' -print -quit)" ]]

# Protected configuration copy contains no new credentials; don't print its contents.
sudo install -o root -g root -m 0600 /etc/music-agent/music-agent.env "$archive_dir/music-agent.env"
sudo cp /opt/music-agent/current/RELEASE.json "$archive_dir/active-release.json"
sudo chmod 0600 "$archive_dir/active-release.json"
sudo /opt/music-agent/current/scripts/backup.sh --protected --label "before-update-$update_stamp" \
  > "$archive_dir/database-backup-path.txt"

clean_checkout="/home/skymusic/src/music-agent-clean-$update_stamp"
[[ ! -e "$clean_checkout" ]]
git clone https://github.com/SkyMusicYTB/music-ingestor-agent.git "$clean_checkout"
git -C "$clean_checkout" fetch origin main
git -C "$clean_checkout" cat-file -e "$delivered_sha^{commit}"
git -C "$clean_checkout" merge-base --is-ancestor "$delivered_sha" origin/main
git -C "$clean_checkout" switch --detach "$delivered_sha"
[[ "$(git -C "$clean_checkout" rev-parse HEAD)" == "$delivered_sha" ]]
[[ -z "$(git -C "$clean_checkout" status --porcelain)" ]]
[[ "$(stat -c %U "$clean_checkout/.git")" == skymusic ]]
[[ "$(stat -c %U "$clean_checkout/.git/index")" == skymusic ]]
[[ -z "$(find "$clean_checkout/.git" -perm /022 -print -quit)" ]]

# Compare the saved emergency patches against committed fixes/tests before activation.
git -C "$clean_checkout" log -1 --oneline
less "$archive_dir/unstaged.patch" "$archive_dir/staged.patch"
# Leave both checkouts in place: no working path replacement is needed to deploy.
cd "$clean_checkout"
sudo bash scripts/deploy.sh
sudo /opt/music-agent/current/scripts/validate.sh --services
sudo /opt/music-agent/current/scripts/music-agentctl.sh user-list
sudo systemctl is-active music-agent-web music-agent-worker music-agent-backup.timer
sudo cat /opt/music-agent/current/RELEASE.json
```

The archive includes ignored files and may contain secrets: keep the directory 0700
and archives/configuration 0600, do not commit or upload them. Preserve the complete
old checkout in place as well. If later replacing its working path, move it to an
explicit archive path only after checks pass; never delete it to make room.

Deployment preflight uses the candidate's final-path venv **as music-agent** before
service stop and must accept the unchanged Luna/legacy-50 environment. It then stops
worker/web, makes its own paired verified backup, migrates, activates atomically,
validates and restarts. Activation then requires both services active and two
consecutive successful `/health/ready` responses within 60 seconds (65-second hard
process timeout, followed by at most five seconds to terminate). A live process with
an unready HTTP application fails activation and restores the old release **and**
matching DB. Existing
ACL, credential separation, tool pins and rollback-journal DELETE/FULL remain intact.

`validate.sh --services` uses the same readiness check. It runs without credentials
as `music-agent`, connects directly to the configured local listener, sends an allowed
Host header, and does not use proxies or follow redirects. Wildcard listeners require
an allowed loopback client CIDR (already present by default); explicitly bound
addresses must belong to this machine and the configured allowed-client networks.
The connection/host settings are checked before deployment stops services.

Acceptance, before considering the update complete:

1. Verify RELEASE.json SHA equals `delivered_sha`, schema is 0004, web/worker and
   backup timer are active. The backup service itself is a oneshot, not continuously active.
2. Existing admin logs in through LAN and configured Tailscale; Users appears.
   Settings shows Luna, 50 rounds from legacy config, independent built-in cap 10
   and the existing deadline. Do not change the environment to make validation pass.
3. Run the read-only audit, then a full scan, then audit again using the commands
   above. Inspect reasons for unindexed files and mixed-format totals in Library.
4. Create a temporary **standard** user through Users; save the one-time password,
   verify forced change, shared library browsing, own requests and admin-route denial.
   Deactivate it through Users; preserve its history. Verify ordinary exact acquisition
   and exception review behavior remain unchanged with an operator-permitted item.
5. Retain archived checkout, patches, configuration and both protected backups until
   all these checks pass. No media or Navidrome configuration needs recreating.

## Rollback boundary

**Never run pre-account-management code against schema 0004 or downgrade in place.**
Use the old release's matching pre-update backup through the current rollback script:

```bash
sudo /opt/music-agent/current/scripts/rollback.sh OLD_RELEASE_ID \
  --restore-backup /var/lib/music-agent-safety-backups/PAIRED_PREUPDATE_BACKUP.db
```

Resolve both placeholders from retained manifests; do not guess. The rollback script
first archives newer DB state in a protected safety backup. The restored DB is a
recovery point: users/password changes/requests/history after its timestamp are not
present in the old application. Music published since then remains on disk and is
not deleted. Retain the newer DB for recovery rather than importing its accounts
into old code. Existing deployment/readiness-failure tests exercise paired restoration.
For rollback to a preserved schema 0001–0003 release only, the trusted target manifest
must match its installed application's schema. Those older readiness endpoints
incorrectly require worker-directory writes from the web sandbox, so the current
validator instead requires target DB/schema validation, both active units, and two
successful `/health/live` responses. Schema 0004 and later never use that fallback.
