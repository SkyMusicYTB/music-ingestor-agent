# Music Agent

For the accounts, model-budget, download-history and mixed-format update, see the
[production update and safe rollout guide](docs/production-update.md).

Music Agent is a private, self-hosted music discovery and library-ingestion service. It accepts natural-language requests, lets an OpenAI model select only explicitly exposed research tools, resolves permitted media with yt-dlp, prepares tags and artwork, and atomically places completed files in an existing Navidrome library. It does not stream media or replace Navidrome.

The production target is **Ubuntu Server 26.04.1 LTS amd64**, installed natively with Python 3.14 and systemd. Docker is neither required nor supported.

## Architecture

The FastAPI web process authenticates the operator, validates requests, orchestrates discovery, and enqueues durable work. A separate worker leases jobs from SQLite, downloads into `/srv/music-downloads`, validates/transcodes/tags media, and atomically publishes it to `/srv/music`. SQLite is also the library index and audit record. Navidrome reads the finished library on its own schedule.

## Automatic acquisition

The default interaction is automatic unless evidence is genuinely exceptional. An exact Add request such as `add Yellow by Coldplay` resolves a finite MusicBrainz recording/release set, searches curated SoundCloud and YouTube candidates, incorporates direct Bandcamp evidence when supplied, groups equivalent uploads, and queues the coherent result without showing source or release-edition forms. The uploader is retained as provenance and is never treated as the canonical recording artist, so a valid third-party upload can still match the requested Coldplay recording.

Recording version is classified independently from release context: a studio track appearing on a compilation or album whose title contains “Live” remains a studio track unless the request or recording-level evidence says otherwise. Exact-track worker discovery uses bounded, deduplicated artist-credit and official-audio/video query variants, so natural-language Add requests do not rely on the model having already located an executable source.

MusicBrainz enrichment is preferred, not mandatory: `MUSIC_AGENT_CANONICAL_METADATA_POLICY=prefer` allows strongly validated provider metadata when no safe canonical match is available, with null MusicBrainz IDs and a visible warning. Use `require` to require enrichment or an explicit metadata decision. Metadata review happens before the full download when probe information is sufficient. See [operations](docs/operations.md#canonical-metadata-and-validated-source-fallback) for policy details and repair of older empty metadata reviews.

Deterministic high-confidence matches avoid an extra OpenAI call. Borderline matches use strict structured decisions over opaque candidate IDs already returned by local tools; OpenAI cannot invent a URL or MBID, relax extractor/network rules, or override version, duplicate, filesystem, and publication gates. Failed source-specific attempts advance through at most three persisted safe candidates. Find and fuzzy/bulk requests retain their request-level preview, while per-track source and ordinary release selection stays automatic. Only conflicting versions, unsafe duplicates, exhausted permitted sources, or similarly irreducible cases produce one durable exceptional-review bundle.

Direct single-item URLs support reviewed YouTube, SoundCloud, and Bandcamp paths. A YouTube playlist, SoundCloud set, or Bandcamp album is inspected only as a flat bounded collection (25 items by default), never downloaded as one playlist; the operator selects its entries once and each selected item becomes an independently validated job. Profiles, likes pages, cookies, login-only media, DRM, and generic extraction remain unsupported.

Production uses SQLite rollback-journal mode (`journal_mode=DELETE`, `synchronous=FULL`), short transactions, a 10-second busy timeout, and independent connections. WAL is deliberately disabled; see [the database rationale](docs/architecture.md).

| Concern | Production location |
| --- | --- |
| Active code | `/opt/music-agent/current` → immutable release |
| Configuration | `/etc/music-agent/music-agent.env` |
| Credentials | `/etc/music-agent/credentials/*` (root-only) |
| SQLite and state | `/var/lib/music-agent` |
| Work/downloads | `/srv/music-downloads` |
| Finished music | `/srv/music` |
| yt-dlp and Deno | `/opt/music-agent/tools` |

## Development

Python 3.12–3.14 is supported for development. On macOS, Linux, Windows, or WSL2, create a local virtual environment; never copy it to production:

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# POSIX shells:
source .venv/bin/activate
python -m pip install --require-hashes -r requirements/development.lock
python -m pip install --no-build-isolation --no-deps -e .
pytest
ruff check app tests
mypy app
```

Use the default `dev-data/` paths locally. WSL2 is useful for exercising Linux filesystem and ffmpeg behavior, but Ubuntu 26.04 remains the deployment source of truth. Shell files are forced to LF by `.gitattributes`.

## First Ubuntu deployment

Confirm that Navidrome is already installed and points at `/srv/music`, then clone as your normal administrator account:

```bash
git clone <repository-url> ~/src/music-agent
cd ~/src/music-agent
sudo bash scripts/install.sh --navidrome-unit navidrome.service
sudoedit /etc/music-agent/music-agent.env
sudo bash scripts/install.sh --navidrome-unit navidrome.service
sudo /opt/music-agent/current/scripts/set-secret.sh openai_api_key
sudo systemctl restart music-agent-web.service
sudo /opt/music-agent/current/scripts/validate.sh --services
```

On a first run, the installer creates the environment file and deliberately stops at its invalid `CHANGE-ME` MusicBrainz contact. Replace that value with a monitored email address or HTTPS contact URL, review the remaining settings, and rerun the same command. The installer also refuses non-Ubuntu-26.04 hosts, non-amd64 machines, dirty Git trees (unless explicitly overridden), and unknown Navidrome identities. It installs apt prerequisites without upgrading the OS, creates a non-login `music-agent` account, preserves existing configuration, takes an ACL snapshot before granting library access, verifies pinned tool checksums, builds the virtualenv at its permanent release path, backs up SQLite, migrates, and activates the immutable release atomically. It never `chown`s `/srv/music` or restarts Navidrome.

Review `/etc/music-agent/music-agent.env`, especially the MusicBrainz contact, host allowlist, client networks, origin/proxy policy, enabled media providers, and HTTPS settings. Deployment's credentialless all-role preflight rejects malformed lists, prohibited generic extraction, and incoherent matching limits before activation. Secrets are set interactively or from stdin and never placed in this file:

```bash
printf '%s\n' "$OPENAI_API_KEY" | sudo /opt/music-agent/current/scripts/set-secret.sh openai_api_key --stdin --restart
sudo /opt/music-agent/current/scripts/set-secret.sh listenbrainz_token --restart
```

The production default binds `0.0.0.0:8787` so authorized LAN and Tailscale clients can connect; the application CIDR gate and authentication still apply. Do not add router forwarding. For private tailnet HTTPS, a current Tailscale CLI can proxy the loopback listener persistently:

```bash
sudo tailscale serve --bg http://127.0.0.1:8787
tailscale serve status
```

Then set `MUSIC_AGENT_HTTPS_ENABLED=true`, `MUSIC_AGENT_PUBLIC_BASE_URL` to the displayed HTTPS URL, and add its hostname to `MUSIC_AGENT_TRUSTED_HOSTS`. Restart the web unit. Do not use Funnel or router port forwarding.

## Operations

```bash
sudo systemctl start music-agent-web.service music-agent-worker.service
sudo systemctl stop music-agent-worker.service music-agent-web.service
sudo systemctl restart music-agent-web.service music-agent-worker.service
sudo journalctl -u music-agent-web.service -u music-agent-worker.service -f
sudo /opt/music-agent/current/scripts/music-agentctl.sh scan --full
sudo /opt/music-agent/current/scripts/music-agentctl.sh admin-reset
```

For an application update:

```bash
cd ~/src/music-agent
git pull --ff-only
sudo bash scripts/deploy.sh
```

Dependency installation and import validation happen before service interruption. Activation then stops the worker and web process, creates a verified SQLite backup, migrates, switches the symlink, and checks both units. Failure reinstates the paired prior code and database. Roll back explicitly with:

```bash
sudo /opt/music-agent/current/scripts/rollback.sh
```

Update yt-dlp using the repository-audited pin, independently of application releases:

```bash
sudo /opt/music-agent/current/scripts/update-yt-dlp.sh
```

An emergency upstream version requires both the exact release version and a SHA-256 copied from the official release checksums; see [deployment](docs/deployment.md). The worker is restarted only after the new executable validates, and its link is reverted if activation fails.

Back up and restore SQLite with verified scripts:

```bash
sudo /opt/music-agent/current/scripts/backup.sh --label before-maintenance
sudo /opt/music-agent/current/scripts/restore.sh --backup /var/lib/music-agent/backups/<file>.db
```

The daily timer backs up SQLite, but `/srv/music`, `/etc/music-agent`, and backups still need an encrypted off-host copy. See [backups and restore](docs/backups.md).

## Documentation

- [Architecture and queue/database design](docs/architecture.md)
- [Installation, updates, rollback, permissions, and uninstall](docs/deployment.md)
- [Service operation, scans, logs, and troubleshooting](docs/operations.md)
- [Backup, restore, and disaster recovery](docs/backups.md)
- [Authentication, network, secret, SSRF, and systemd security](docs/security.md)
- [Cross-platform development and test workflow](docs/development.md)

The OpenAI API is usage-billed. The application records request/token/cost data when pricing is configured; set the `MUSIC_AGENT_PRICE_*` values from the current OpenAI pricing page and treat displayed cost as an estimate. Keep web search disabled unless its value and cost are understood.
