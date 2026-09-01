# Security model

## Exposure and trust boundaries

The production default listens on all interfaces at port 8787 for the configured LAN and Tailscale CIDRs. Use tailnet-private Tailscale Serve with HTTPS; do not add router forwarding or public Funnel exposure. Host and client-network allowlists are defense in depth, not a substitute for authentication. Trust `X-Forwarded-*` only from explicitly configured proxy CIDRs.

The single private operator account uses Argon2id password hashing, server-side sessions, idle and absolute expiry, login throttling, secure/HttpOnly/SameSite cookies when HTTPS is enabled, and rotation on login/privilege changes. Every state-changing browser request requires a CSRF token plus strict origin validation; JSON/content-type checks alone are insufficient. Administrative reset reads a password from a TTY.

References: [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html), [Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html), and [CSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).

## Credentials

The environment file contains non-secrets only and is root-owned mode 0640. `auth_hmac_key`, `openai_api_key`, and `listenbrainz_token` are separate root-owned mode-0600 files. systemd copies them into a private runtime credential directory for the web process through `LoadCredential`; the worker has no credential mapping. The set-secret script reads hidden TTY/stdin input into a same-directory temporary file and renames atomically. Secrets never appear in argv, Git, logs, or an environment assignment.

systemd documents that environment variables are not suitable for secrets and describes its credential mechanism in [systemd.exec(5)](https://manpages.ubuntu.com/manpages/resolute/man5/systemd.exec.5.html).

Rotate a suspected key, restart web, invalidate relevant sessions/tokens, and review journals/audit data. Rotating `auth_hmac_key` invalidates signed authentication material by design.

## URL and subprocess safety

All user/model-provided URLs are untrusted. Accept only HTTP(S), reject embedded credentials and confusing host syntax, resolve every hostname, reject loopback/private/link-local/multicast/reserved/metadata ranges for every A/AAAA result, cap redirects, and revalidate each hop to resist DNS rebinding. Apply the same validation immediately before yt-dlp. Direct media is capped by type, byte count, duration, redirect count, and time. Never forward application/server credentials to a supplied origin.

The resolver invokes yt-dlp, ffmpeg, and ffprobe with argv arrays, no shell, a minimal environment, bounded timeout/output, and validated executable discovery. Deno is present only as yt-dlp's supported JavaScript runtime. Filenames, tags, and subprocess output are untrusted data. See [OWASP SSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html).

## Host permissions and services

Both long-running units use the non-login `music-agent` account, no capabilities, `NoNewPrivileges`, private temporary/device views, strict system protection, protected kernel/control/proc resources, namespace/SUID/realtime restrictions, native syscall architecture, and only Unix/IPv4/IPv6 address families. The web unit can write state/artwork. The worker can additionally write downloads and music. No syscall filter or private network blocks ffmpeg, yt-dlp, Deno, DNS, or HTTPS.

`/var/lib/music-agent` is `music-agent:music-agent` mode 0750 and the live database is mode 0640. Credential and backup artifacts are mode 0600. The web and worker mount the backup, deployment-manifest, and ACL-snapshot subdirectories read-only even though they can update the live state directory.

The worker receives a graceful 180-second stop. Deployment serializes operations with `flock`; service interruption, database backup/migration, code switch, and failure recovery form one transaction. Release and tool trees are root-owned and made non-writable after activation.

POSIX ACL changes preserve `/srv/music` ownership and Navidrome configuration. The ACL snapshot is root-only because filenames may be private. Review the systemd posture with:

```bash
sudo systemd-analyze verify /etc/systemd/system/music-agent-*.service
sudo systemd-analyze security music-agent-web.service music-agent-worker.service
```

Hardening directives and scoring are documented in [systemd.exec(5)](https://manpages.ubuntu.com/manpages/resolute/man5/systemd.exec.5.html) and [systemd-analyze(1)](https://manpages.ubuntu.com/manpages/resolute/man1/systemd-analyze.1.html).

## Supply chain and updates

Python direct and transitive dependencies are exactly pinned, committed lock files contain package-index artifact hashes, and installs require those hashes and accept wheels only. An Ubuntu/Python-3.14 helper regenerates both locks. CI resolves the production closure for CPython 3.14 amd64 and audits it. Deno and yt-dlp URLs are constrained to official GitHub release layouts with exact SHA-256 pins. Their immutable version directories preserve both upstream artifact provenance and an installed-binary digest, preventing silent in-place updates.
