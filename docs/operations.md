# Operations

## Services and logs

```bash
sudo systemctl status music-agent-web.service music-agent-worker.service
sudo systemctl restart music-agent-web.service music-agent-worker.service
sudo systemctl stop music-agent-worker.service music-agent-web.service
sudo systemctl start music-agent-web.service music-agent-worker.service
sudo journalctl -u music-agent-web.service -u music-agent-worker.service --since today
sudo journalctl -u music-agent-worker.service -f
```

Stop the worker first for planned maintenance so it receives its 180-second graceful shutdown window. systemd sends SIGTERM and uses mixed kill mode after the timeout. Logs go only to journald; scripts and services do not create root-owned log files in state directories.

The backup timer is independent:

```bash
systemctl list-timers music-agent-backup.timer
sudo systemctl start music-agent-backup.service
journalctl -u music-agent-backup.service
```

## Administrative commands

Run application commands with the installed venv. `admin-reset` reads the new password from a TTY; do not pipe it or place it in shell history.

```bash
sudo /opt/music-agent/current/scripts/music-agentctl.sh admin-reset
sudo /opt/music-agent/current/scripts/music-agentctl.sh scan
sudo /opt/music-agent/current/scripts/music-agentctl.sh scan --full
sudo /opt/music-agent/current/scripts/validate.sh --services
```

A full scan reconciles `/srv/music` with the SQLite index. It does not send the full library to OpenAI. While the worker is running, it also schedules an idempotent incremental scan every 30 minutes; unchanged files are matched by size and modification time, so their tags are not reread. Navidrome discovers safely published files through its configured periodic scan; the deployment does not rely on an undocumented rescan API and never restarts it.

## Configuration changes

Edit `/etc/music-agent/music-agent.env` as root, retain `root:music-agent` ownership and mode 0640, then validate and restart:

```bash
sudoedit /etc/music-agent/music-agent.env
sudo chown root:music-agent /etc/music-agent/music-agent.env
sudo chmod 0640 /etc/music-agent/music-agent.env
sudo /opt/music-agent/current/scripts/validate.sh
sudo systemctl restart music-agent-web.service music-agent-worker.service
```

Only `MUSIC_AGENT_*` keys are accepted by administrative commands. Never add an API key, token, HMAC key, shell expansion, or executable syntax. Store secrets with `set-secret.sh`; only the web service has `LoadCredential` mappings.

`MUSIC_AGENT_MUSICBRAINZ_USER_AGENT` must identify the application and contain a real monitored email address or HTTPS contact URL. The deployment validator and web unit reject the shipped `example.invalid` placeholder and common example/test domains.

## Troubleshooting

- **Web unit fails before start:** run `journalctl -u music-agent-web.service -n 100`; `ExecStartPre` normally identifies config, credential, database, or schema failure.
- **Worker repeatedly retries:** inspect the durable job error and journal. Confirm `ffmpeg`, `ffprobe`, `yt-dlp`, and `deno` resolve from `/opt/music-agent/tools/current/bin`/the system path. Do not disable URL policy or validation to work around a source failure.
- **Database busy:** confirm only one web service and the configured worker count exist. Long network/media operations must not hold DB transactions. Do not enable WAL. Run the validation script and inspect leases.
- **Library permission failure:** run `getfacl /srv/music`, confirm the mount supports POSIX ACLs, then re-run `configure-library-acl.sh` with the exact Navidrome identity. Do not `chown -R` the library.
- **Disk pressure:** inspect `df -h /srv/music /srv/music-downloads /var/lib/music-agent`; preserve the configured free-space floor. Clean only known abandoned staging jobs after the worker is stopped.
- **Failed deploy:** inspect `/var/lib/music-agent/deployments/<release>.json` and the paired `predeploy-*.db` manifest. A failed release is retained for diagnosis but never selected by rollback.
- **Tailscale URL rejected:** add the exact MagicDNS hostname to trusted hosts, set the public HTTPS URL, and confirm `tailscale serve status`. Do not broadly trust forwarded headers.

## Cost tracking

OpenAI token/use records are durable audit data. Configure the `MUSIC_AGENT_PRICE_*` values using current official rates; cached input, cache writes, output, and web-search costs are distinct. A missing rate should display cost as unknown, not zero. Set per-request track/step/time limits, keep web search disabled unless required, and review usage in the OpenAI account as billing source of truth.
