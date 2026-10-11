#!/usr/bin/env python3
"""Reject public commit contact emails without echoing their values."""
import re
import subprocess
import sys

ALLOWED = re.compile(rb"(?:[^@\s]+@users\.noreply\.github\.com|noreply@github\.com)\Z", re.I)

def main():
    shallow = subprocess.check_output(["git", "rev-parse", "--is-shallow-repository"]).strip()
    if shallow != b"false":
        raise SystemExit("Privacy check requires complete Git history.")
    raw = subprocess.check_output(["git", "log", "--all", "--format=%H%x00%ae%x00%ce"])
    bad = []
    for line in raw.splitlines():
        commit, author, committer = line.split(b"\0")
        if not ALLOWED.fullmatch(author) or not ALLOWED.fullmatch(committer):
            bad.append(commit.decode("ascii"))
    if bad:
        print(f"Blocked: {len(bad)} commits use non-private author or committer email.")
        print("Use the account's verified GitHub noreply address; no contact emails are logged.")
        return 1
    print("Commit identity privacy passed.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
