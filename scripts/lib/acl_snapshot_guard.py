"""Reject ACL mask recalculation that would unmask an existing principal."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

_ACL_ENTRY = re.compile(
    r"^(?P<default>default:)?(?P<tag>user|group):(?P<qualifier>[^:]*):"
    r"(?P<permissions>[rwx-]{3})(?:\s+#effective:(?P<effective>[rwx-]{3}))?$"
)


def validate_snapshot(path: Path, mutable_users: frozenset[str]) -> None:
    current_path = "unknown path"
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if line.startswith("# file: "):
            current_path = line.removeprefix("# file: ")
            continue
        if (
            not line
            or line.startswith("#")
            or line.startswith("mask:")
            or line.startswith("default:mask:")
            or line.startswith("other:")
            or line.startswith("default:other:")
        ):
            continue
        match = _ACL_ENTRY.fullmatch(line)
        if match is None:
            raise ValueError(f"unrecognized ACL snapshot line {line_number}: {line!r}")
        effective = match.group("effective")
        permissions = match.group("permissions")
        if effective is None or effective == permissions:
            continue
        if match.group("tag") == "user" and match.group("qualifier") in mutable_users:
            continue
        acl_kind = "default ACL" if match.group("default") else "access ACL"
        principal = f"{match.group('tag')}:{match.group('qualifier') or ':'}"
        raise ValueError(
            f"{current_path}: {acl_kind} entry {principal} stores {permissions} but "
            f"currently grants {effective}; refusing to widen its mask"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--mutable-user", action="append", default=[])
    arguments = parser.parse_args()
    try:
        validate_snapshot(arguments.snapshot, frozenset(arguments.mutable_user))
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
