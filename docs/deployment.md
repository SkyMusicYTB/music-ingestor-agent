# Native Ubuntu deployment

## Supported host and prerequisites

Production is guarded to Ubuntu Server 26.04.x (`VERSION_ID=26.04`), amd64, and the distribution Python 3.14. The first-install script checks those values and installs only the required native packages: Python/venv, ffmpeg, Git, curl, CA certificates, rsync, ACL tools, unzip, and OpenSSL. It performs no distribution upgrade and never installs Python packages globally.

Keep the Git checkout under an administrator's home directory. Root never needs Git credentials. Each deployment copies the committed tree to a new `/opt/music-agent/releases/<commit>-<UTC timestamp>` directory, builds its Linux venv there, installs a fully pinned dependency closure, validates imports, and later removes all write bits. `/opt/music-agent/current` is the sole atomic activation pointer.

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

`configure-library-acl.sh` records a physical, non-symlink-following recursive ACL snapshot in `/var/lib/music-agent/acl-backups` before changing access. It adds:

- read/write/traverse access for `music-agent` to existing library entries;
- default write ACLs for future Music Agent entries;
- explicit existing/default read access for the detected Navidrome account.

It checks that `/srv/music` ownership did not change and tests both accounts. It does not run `chown`, change Navidrome configuration, or restart Navidrome. Directory write access inherently permits entry creation, rename, and deletion; application no-clobber/duplicate checks are therefore a required second control.

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

Before interruption, deployment builds a complete release, allows only wheels, runs `pip check`, compiles the app, validates credentials/config/tools, and installs parseable units. During the activation transaction it records which services were running, stops worker then web, backs up/integrity-checks SQLite, migrates and validates with the service identity, switches the symlink, starts both services, and revalidates. Any failed migration or activation restores the old symlink and the paired database backup before attempting to restart the old units.

Do not edit an installed release or its venv. Commit a fix and deploy a new one.

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

Normal deployments verify `requirements/tool-pins.env`, official GitHub URLs, and SHA-256 values. Deno and yt-dlp live in versioned root-owned directories and are found through `/opt/music-agent/tools/current/bin`.

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
