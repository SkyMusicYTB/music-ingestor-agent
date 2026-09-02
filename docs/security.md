# Security model

## Exposure and trust boundaries

The production default listens on all interfaces at port 8787 for the configured LAN and Tailscale CIDRs. Use tailnet-private Tailscale Serve with HTTPS; do not add router forwarding or public Funnel exposure. Host and client-network allowlists are defense in depth, not a substitute for authentication. Trust `X-Forwarded-*` only from explicitly configured proxy CIDRs.

The single private operator account uses Argon2id password hashing, server-side sessions, idle and absolute expiry, login throttling, effective-HTTPS Secure/HttpOnly/SameSite cookies, and rotation on login/privilege changes. Every state-changing browser request requires a CSRF token. The default `private_network` origin policy rejects explicit cross-site Fetch Metadata but accepts missing/null Origin and legitimate scheme/host changes through a private proxy. `strict` requires Origin or Referer to match the normalized effective origin, public URL, or explicit browser-origin allowlist. Administrative reset reads a password from a TTY.

Request normalization occurs in this order: trusted TCP proxy, client CIDR, trusted host, authentication, then CSRF/origin policy. `X-Forwarded-For`, `X-Forwarded-Proto`, and `X-Forwarded-Host` are honored only from `MUSIC_AGENT_TRUSTED_PROXY_CIDRS`; malformed trusted values are rejected and untrusted values ignored. Uvicorn proxy parsing remains disabled so this normalization happens exactly once.

References: [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html), [Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html), and [CSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).

## Credentials

The environment file contains non-secrets only and is root-owned mode 0640. `auth_hmac_key`, `openai_api_key`, and `listenbrainz_token` are separate root-owned mode-0600 files. systemd copies them into a private runtime credential directory for the web process through `LoadCredential`; the worker has no credential mapping. Deployment preflight validates configuration and strict schemas without credentials, then permits credential-backed administrative execution only after the worker is stopped. The shared helper refuses to stage credentials while the worker unit is active. The set-secret script reads hidden TTY/stdin input into a same-directory temporary file and renames atomically. Secrets never appear in argv, Git, logs, or an environment assignment.

systemd documents that environment variables are not suitable for secrets and describes its credential mechanism in [systemd.exec(5)](https://manpages.ubuntu.com/manpages/resolute/man5/systemd.exec.5.html).

Rotate a suspected key, restart web, invalidate relevant sessions/tokens, and review journals/audit data. Rotating `auth_hmac_key` invalidates signed authentication material by design.

## URL and subprocess safety

All user/model-provided URLs are untrusted. Executable acquisition URLs are HTTPS, credential-free, default-port, non-literal hosts from a curated provider or an explicitly allowed known extractor. Generic extraction remains prohibited in production. A per-job authenticated localhost CONNECT proxy resolves and pins each yt-dlp connection, rejects loopback/private/link-local/multicast/reserved/metadata/Tailscale-CGNAT destinations for every A/AAAA result, and repeats the check for every redirect host. Direct media is capped by type, byte count, duration, redirect count, output volume, job-directory growth, per-filesystem reservations, and time. Never forward application/server credentials to a supplied origin.

The model-visible media tools return opaque evidence/source IDs and sanitized metadata, never executable URLs. The worker owns URLs and subprocesses. OpenAI can select only finite IDs and cannot override provider, network, DRM/login, version, duplicate, byte, or publication policy. Provider descriptions cannot affect tool dispatch, ranking authority, or command construction.

Single-item acquisition requires exact membership in the reviewed extractor alias set; extractor namespace prefixes and future aliases are not implicitly trusted. Collection inspection is flat, capped, and non-downloading, and selected entries return to the strict one-URL `--no-playlist` path. yt-dlp metadata that reports DRM or non-public/login/subscription availability is rejected both when it becomes a candidate and again immediately before acquisition.

The resolver invokes yt-dlp, ffmpeg, and ffprobe with argv arrays, no shell, a minimal environment, bounded timeout/output, and validated executable discovery. Deno is present only as yt-dlp's supported JavaScript runtime. Filenames, tags, and subprocess output are untrusted data. See [OWASP SSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html).

## Host permissions and services

Both long-running units use the non-login `music-agent` account, no capabilities, `NoNewPrivileges`, private temporary/device views, strict system protection, protected kernel/control/proc resources, namespace/SUID/realtime restrictions, native syscall architecture, and only Unix/IPv4/IPv6 address families. The web unit can write state/artwork. The worker can additionally write downloads and music. No syscall filter or private network blocks ffmpeg, yt-dlp, Deno, DNS, or HTTPS.

`/var/lib/music-agent` is `music-agent:music-agent` mode 0750 and the live database is mode 0640. Credential and backup artifacts are mode 0600. Transaction safety backups live in the root-only sibling `/var/lib/music-agent-safety-backups`, after a real service-account probe confirms it cannot create, write, or delete entries through either the directory or its parent. The web and worker mount the ordinary backup, deployment-manifest, and ACL-snapshot subdirectories read-only even though they can update the live state directory.

The worker receives a graceful 180-second stop. Deployment serializes operations with `flock`; service interruption, database backup/migration, code switch, and failure recovery form one transaction. Release trees are `root:music-agent`, group-readable/traversable, inaccessible to unrelated local accounts, and made non-writable after activation. Managed tool directories and files remain `root:root`; only the executable/read bits required by the service account are exposed.

POSIX ACL changes preserve `/srv/music` ownership and Navidrome configuration. The ACL snapshot is root-only because filenames may be private. Review the systemd posture with:

```bash
sudo systemd-analyze verify /etc/systemd/system/music-agent-*.service
sudo systemd-analyze security music-agent-web.service music-agent-worker.service
```

Hardening directives and scoring are documented in [systemd.exec(5)](https://manpages.ubuntu.com/manpages/resolute/man5/systemd.exec.5.html) and [systemd-analyze(1)](https://manpages.ubuntu.com/manpages/resolute/man1/systemd-analyze.1.html).

## Supply chain and updates

Python direct and transitive dependencies are exactly pinned, committed lock files contain package-index artifact hashes, and installs require those hashes and accept wheels only. An Ubuntu/Python-3.14 helper regenerates both locks. CI resolves the production closure for CPython 3.14 amd64 and audits it.

Root never invokes Git in the administrator checkout. Deployment identifies and validates its physical non-root owner, uses an owner-readable temporary index with optional locks disabled, and verifies that the real index and Git lock state were not changed. Clean releases come from the selected commit's archive. Emergency dirty releases come from an explicit Git manifest of tracked and nonignored untracked regular files; ignored files, Git metadata, secret-like paths, symlinks, and submodules cannot enter the release.

Deno and yt-dlp URLs are constrained to official GitHub release layouts with exact SHA-256 pins. Their versioned directories preserve both upstream artifact provenance and an installed-binary digest, preventing silent in-place updates. Every install normalizes parent directories and binaries to root-owned fixed modes, then executes both tools as `music-agent` with the exact systemd PATH before accepting them.
