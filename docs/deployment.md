# Native Ubuntu deployment

## Supported host and prerequisites

Production is guarded to Ubuntu Server 26.04.x (`VERSION_ID=26.04`), amd64, and the distribution Python 3.14. The first-install script checks those values and installs only the required native packages: Python/venv, ffmpeg, Git, curl, CA certificates, rsync, ACL tools, unzip, and OpenSSL. It performs no distribution upgrade and never installs Python packages globally.

Keep the Git checkout under an administrator's home directory and keep the repository root and `.git` directory owned by that non-root account. Root never runs Git against the checkout and never needs Git credentials. Deployment resolves the physical checkout owner, copies the current index to an owner-readable private temporary directory, and runs every read-only Git command as that owner with optional locks and filesystem monitoring disabled. It verifies that the original index owner, group, mode, size, modification time, checksum, and lock state are unchanged before proceeding.

A clean deployment is materialized from `git archive`, not by recursively copying the checkout. An explicitly authorized `--allow-dirty` deployment uses a NUL-delimited Git manifest containing modified tracked files and nonignored untracked files. Ignored files are never copied. Git metadata, environment overrides, credential/secret directories, private-key-like files, submodules, unmerged entries, and tracked symlinks are rejected. Symlink-safe content hashes must match before, inside the candidate release, and after copying; source metadata must also remain unchanged, so an `M`-to-`M` concurrent edit cannot evade the snapshot check. The manifest checksum and checkout owner are recorded in `RELEASE.json`.

Each deployment creates its unique final `/opt/music-agent/releases/<commit>-<UTC timestamp>` directory and creates the Linux virtualenv directly at that permanent absolute path. The release is never renamed after virtualenv creation, so pip-generated absolute console-script shebangs remain valid. It installs a fully pinned dependency closure, normalizes the tree to `root:music-agent`, readable/traversable but not writable by the runtime account and inaccessible to unrelated local users, and validates the interpreter, imports, web CLI, worker CLI, tool executables, and shebangs as `music-agent` before activation. `/opt/music-agent/current` is the sole atomic activation pointer; all write bits are removed after successful activation.

## First installation

```bash
git clone <repository-url> ~/src/music-agent
cd ~/src/music-agent
sudo bash scripts/install.sh --navidrome-unit navidrome.service
sudoedit /etc/music-agent/music-agent.env
sudo bash scripts/install.sh --navidrome-unit navidrome.service
```

If the service has a different name, pass it. If Navidrome is not managed by systemd, pass its actual non-root account with `--navidrome-user`. Detection failure is deliberate: the installer will not guess at library access.

The first invocation creates `/etc/music-agent/music-agent.env` only when absent, then intentionally stops because its `CHANGE-ME@example.invalid` MusicBrainz contact is not valid for production. Replace it with a monitored operator email or HTTPS contact URL, review the remaining values, and rerun the same install command. Validation and the web unit both reject known placeholder/test contacts, so the client cannot go live with the template identity. Existing content is preserved. The completed install generates a strong authentication HMAC key. Empty root-only files for optional OpenAI and ListenBrainz credentials allow systemd's mandatory credential mapping; set them after install with `set-secret.sh`.

Use `--no-start` to prepare and migrate without enabling or starting services. `--allow-dirty` exists for an audited emergency, marks the release dirty, and should not be routine.

## Navidrome and ACLs

`configure-library-acl.sh` records and retains a physical, non-symlink-following recursive ACL snapshot in the root-only `/var/lib/music-agent-safety-backups/acl` directory before changing access. It adds:

- read/write/traverse access for `music-agent` to existing library entries;
- default write ACLs for future Music Agent entries;
- explicit existing/default read access for the detected Navidrome account.

It checks that `/srv/music` ownership did not change and tests both accounts with real operations rather than predictive mode-bit checks. As `music-agent` it lists the root, creates a securely unique nested directory and file, writes/fsyncs/closes it, and cleans both entries even on failure; this also exercises inherited default ACLs. As the detected Navidrome account it lists the root and physically opens/reads an existing nested regular file when one is present. Existing ACL entries with dormant permissions are rejected before mask recalculation could make those permissions effective. Any later failure or handled termination signal automatically restores the complete snapshot from the protected area, which the runtime account cannot rename or replace. The same protected snapshot remains available to the operator after success. It does not run `chown`, change Navidrome configuration, or restart Navidrome. Directory write access inherently permits entry creation, rename, and deletion; application no-clobber/duplicate checks are therefore a required second control.

Re-run the script only when the Navidrome identity or library mount changes. The ACL snapshot can be inspected or restored with `setfacl --restore=<snapshot>` after carefully reviewing its absolute paths.

## Private HTTPS with Tailscale

The default backend listens on all server interfaces at port 8787 so allowed LAN and Tailscale clients can connect, while avoiding Pi-hole and Navidrome ports. Application CIDR checks, trusted-host checks, and authentication remain mandatory. With a current Tailscale CLI, the same listener can be presented through tailnet-private HTTPS:

```bash
sudo tailscale serve --bg http://127.0.0.1:8787
tailscale serve status
```

Serve is tailnet-private; do not use Funnel. Update `MUSIC_AGENT_PUBLIC_BASE_URL`, set `MUSIC_AGENT_HTTPS_ENABLED=true`, and add the exact HTTPS host to `MUSIC_AGENT_TRUSTED_HOSTS`. Unless a trusted proxy is explicitly configured, forwarded client-IP headers are ignored. Tailscale's current syntax is documented at [tailscale serve](https://tailscale.com/docs/reference/tailscale-cli/serve).

## Application updates

```bash
cd ~/src/music-agent
git pull --ff-only
sudo bash scripts/deploy.sh
```

Before interruption, deployment builds a complete inactive release, allows only wheels, runs `pip check`, compiles the app, validates credential ownership/shape, configuration, strict schemas, tools, and service-account execution, and installs parseable units. Native operations require the exact managed production database, artwork, downloads, music, and backup paths from the shipped environment template. The pre-stop configuration/schema check runs under the worker role without a credential directory, so a still-running worker can never read a temporary copy of web credentials. A build that fails before validation is removed; an unexpected interruption leaves a `.build-incomplete` marker, while a validated candidate has a `prepared` manifest and remains inactive. During the activation transaction it records which services were running, stops worker then web, writes the paired SQLite snapshot under root-only `/var/lib/music-agent-safety-backups`, loads credentials for migration and full validation only while the worker is stopped, switches the symlink, makes the release immutable, completes final offline validation, and then starts both services. The web unit performs its own isolated `LoadCredential` validation before start. The candidate service identity cannot alter or delete the safety snapshot. Any failed migration or activation restores the old symlink and paired database backup before attempting to restart the old units. Rollback uses the same credentialless preflight and offline credential boundary.

Do not edit an installed release or its venv. Commit a fix and deploy a new one.

### Canonical-enrichment update

This update adds `MUSIC_AGENT_CANONICAL_METADATA_POLICY=prefer` and
`MUSIC_AGENT_PROVIDER_METADATA_FALLBACK_MIN_SCORE=0.90`. They take effect by default
when absent, so no production environment edit is needed; deployment never
overwrites `/etc/music-agent/music-agent.env`. `prefer` attempts MusicBrainz and
permits only independently validated source metadata when a safe canonical match
is unavailable. `require` keeps automatic enrichment mandatory, with actionable
metadata review or bounded provider retry. Neither setting weakens source identity,
network, extraction, version, duplicate or publication checks. Settings shows the
effective policy. See [operations](operations.md#canonical-metadata-and-validated-source-fallback)
for null MBIDs, warnings, review and the narrowly scoped legacy-job repair.

The database stays at schema **0004**; no migration or account reset is required.
The library parser revision increases to **2** so subsequent scans retain explicit
provider authority and structured artist provenance. This rereads changed parser
records only; it does not alter or retag media. Run normal clean deployment and
service validation, then preview/apply the repair only for the affected source ID.
The existing paired-backup activation and rollback procedure remains in force.
If rolling back after jobs have advanced, retain the newer state backup and use
the release-matched backup when instructed by the rollback validator; its recovery
point determines which later job/account changes will be absent.

## Rollback

```bash
# Previous release named in the active manifest
sudo /opt/music-agent/current/scripts/rollback.sh

# Explicit release
sudo /opt/music-agent/current/scripts/rollback.sh <release-id>

# Required when the older release expects another schema
sudo /opt/music-agent/current/scripts/rollback.sh <release-id> \
  --restore-backup /var/lib/music-agent/backups/<matching-backup>.db
```

The command checks release status and schema metadata, creates a new safety backup, switches matching units, and validates. It refuses an unmatched backup and automatically reinstates the pre-rollback pair on failure.

## Tool updates

Normal deployments verify `requirements/tool-pins.env`, official GitHub URLs, and SHA-256 values. Deno and yt-dlp live in versioned root-owned directories and are found through `/opt/music-agent/tools/current/bin`. Every install/update repairs all managed parent/version/bin directories to `root:root` mode 0755, executables to 0755, manifests to 0644, and symlink ownership to root. Activation is refused unless both tools resolve and execute as `music-agent` using the exact systemd PATH. A failed yt-dlp probe or worker restart restores the prior link.

To apply the audited yt-dlp pin:

```bash
sudo /opt/music-agent/current/scripts/update-yt-dlp.sh
```

For an urgent upstream fix, independently verify the checksum from the official yt-dlp release's `SHA2-256SUMS`, then provide both values:

```bash
sudo /opt/music-agent/current/scripts/update-yt-dlp.sh \
  --version YYYY.MM.DD --sha256 <64-lowercase-hex-digits>
```

The URL is constructed from the validated version; arbitrary URLs are impossible. Commit the new pin afterward so future hosts reproduce it. Never use yt-dlp self-update or a global pip install.

## Conservative uninstall

```bash
sudo /opt/music-agent/current/scripts/uninstall.sh --yes
```

This disables/removes the four managed systemd units and deletes only `/opt/music-agent`. It always preserves `/srv/music`, `/srv/music-downloads`, `/var/lib/music-agent`, `/etc/music-agent`, credentials, ACLs, and the service account. `--keep-code` preserves `/opt/music-agent` too. There is intentionally no automatic data-purge option.
