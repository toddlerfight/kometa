"""What is actually running on this box, no guessing.

The bind-mount deploy shovels files onto the NAS with a tar pipe. git never
enters the building. So the live code has no idea what commit it is, and for
months the only answer to "what's deployed?" was a hand-bumped `?v=` string in
index.html and a shrug. That string tells you an asset changed. It does not
tell you which commit, whether the tree was dirty, or whether the Python you
synced is the Python the interpreter actually loaded.

That gap is not academic — the NAS compose file drifted from the repo's for
long enough that nobody remembered, and nothing anywhere would have told us.

So: deploy writes _build.json, this reads it, /api/version serves it. We report
disk state AND process start, because syncing .py files without a restart
leaves the old code running while the stamp claims otherwise. That failure is
silent and it is the one that eats an afternoon.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import time

_HERE = pathlib.Path(__file__).resolve().parent
_STAMP = _HERE / "_build.json"

# When this process booted. Compared against the stamp to catch "synced but
# never restarted" — the lie that looks exactly like a successful deploy.
_PROCESS_STARTED = time.time()

_UNKNOWN = {"sha": None, "short_sha": None, "dirty": None, "branch": None, "source": "unknown"}


def _from_stamp() -> dict | None:
    """The deploy-written stamp. Authoritative on the NAS."""
    try:
        data = json.loads(_STAMP.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("sha"):
        return None
    data.setdefault("source", "stamp")
    return data


def _from_git() -> dict | None:
    """Local dev fallback — the checkout is its own source of truth."""
    root = _HERE.parent

    def _git(*args: str) -> str | None:
        try:
            r = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    sha = _git("rev-parse", "HEAD")
    if not sha:
        return None
    status = _git("status", "--porcelain")
    return {
        "sha": sha,
        "short_sha": sha[:7],
        "dirty": bool(status),  # None-safe: empty string and None both read false
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "source": "git",
    }


def build_info() -> dict:
    """Read fresh every call — the stamp on disk can change under a live process.

    Deliberately not cached. A cached answer would hide precisely the drift
    this module exists to surface.
    """
    info = dict(_from_stamp() or _from_git() or _UNKNOWN)

    sha = info.get("sha")
    if sha and not info.get("short_sha"):
        info["short_sha"] = sha[:7]

    info["process_started"] = _PROCESS_STARTED

    # Files newer than the running interpreter means the .py on disk is not
    # necessarily the .py in memory. Static-only syncs are fine (served off
    # disk); Python changes are not. We can't tell which from here, so we flag
    # it and let the human decide rather than quietly claiming all is well.
    stamped_at = info.get("deployed_at")
    info["restart_may_be_needed"] = bool(
        isinstance(stamped_at, (int, float)) and stamped_at > _PROCESS_STARTED
    )
    return info
