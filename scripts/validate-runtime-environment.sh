#!/usr/bin/env bash
set -euo pipefail
umask 027

die() {
    printf '[music-agent] ERROR: %s\n' "$*" >&2
    exit 1
}

user_agent="${MUSIC_AGENT_MUSICBRAINZ_USER_AGENT:-}"
[[ -n "$user_agent" ]] || die "MUSIC_AGENT_MUSICBRAINZ_USER_AGENT is required"
[[ "$user_agent" != *$'\n'* && "$user_agent" != *$'\r'* ]] ||
    die "MusicBrainz User-Agent contains a newline"

if [[ ! "$user_agent" =~ ^[^/\(\)[:space:]]+/[^\(\)[:space:]]+[[:space:]]+\(([^\(\)]+)\)$ ]]; then
    die "MusicBrainz User-Agent must be 'Application/version (contact)'"
fi
contact="${BASH_REMATCH[1]}"
contact="${contact#+}"
lower_user_agent="$(printf '%s' "$user_agent" | tr '[:upper:]' '[:lower:]')"

case "$lower_user_agent" in
    *example.invalid*|*example.com*|*example.net*|*example.org*|*example.test*|*change-me*|*configure-contact*)
        die "replace the placeholder MusicBrainz contact in /etc/music-agent/music-agent.env"
        ;;
esac

lower_contact="$(printf '%s' "$contact" | tr '[:upper:]' '[:lower:]')"
if [[ "$lower_contact" == mailto:* ]]; then
    contact="${contact#*:}"
    lower_contact="${lower_contact#mailto:}"
fi
if [[ "$contact" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$ ]]; then
    :
elif [[ "$lower_contact" =~ ^https://[a-z0-9.-]+(:[0-9]+)?(/[^[:space:]]*)?$ ]]; then
    [[ "$lower_contact" != https://localhost* ]] || die "MusicBrainz contact URL must not use localhost"
else
    die "MusicBrainz contact must be a real email address or HTTPS URL"
fi
