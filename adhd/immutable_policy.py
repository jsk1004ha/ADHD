"""Bounded workspace-owned policy for optional immutable evidence operations.

Request metadata supplies expectations and restrictions, never authority. This
file is a local opt-in boundary, not an ACL against the workspace's OS owner.
"""
from __future__ import annotations

import json
from pathlib import Path
import stat


MAX_POLICY = 8192
PRIVACY = frozenset({"public", "private", "restricted", "sensitive"})
ACTIONS = frozenset({"capture", "read", "export", "restore", "forget"})
FIELDS = frozenset({"schema_version", "policy_revision", "allowed_privacy", "allowed_actions"})


def _safe(path: Path) -> None:
    for component in (path, *path.parents):
        info = component.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) &
                getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise PermissionError("immutable-policy-link-excluded")


def _object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate-immutable-policy-field")
        result[key] = value
    return result


def _choices(value: object, choices: frozenset) -> bool:
    return (isinstance(value, list) and len(value) <= len(choices)
            and all(isinstance(item, str) and item in choices for item in value)
            and len(value) == len(set(value)))


def load_policy(workspace: Path) -> dict:
    """Read a strict current policy; absent, oversized or unknown schemas deny."""
    workspace = Path(workspace).expanduser().absolute()
    path = workspace / ".adhd" / "immutable-policy.json"
    try:
        _safe(path)
        if not workspace.is_dir():
            raise ValueError("immutable-policy-workspace-required")
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_POLICY:
            raise ValueError("immutable-policy-file-invalid-or-too-large")
        with path.open("rb") as source:
            raw = source.read(MAX_POLICY + 1)
        _safe(path)
        after = path.stat()
        if (len(raw) > MAX_POLICY or len(raw) != before.st_size or
                (before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ValueError("immutable-policy-changed-or-too-large")
        policy = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
    except (OSError, UnicodeError, ValueError) as exc:
        raise PermissionError("current-immutable-policy-required") from exc
    if (not isinstance(policy, dict) or set(policy) != FIELDS
            or type(policy.get("schema_version")) is not int or policy["schema_version"] != 1
            or not isinstance(policy.get("policy_revision"), str)
            or not 1 <= len(policy["policy_revision"]) <= 128
            or any(ord(char) < 32 for char in policy["policy_revision"])
            or not _choices(policy.get("allowed_privacy"), PRIVACY)
            or not _choices(policy.get("allowed_actions"), ACTIONS)):
        raise PermissionError("invalid-current-immutable-policy")
    return policy


def authorize(workspace: Path, *, action: str, expected_revision: str,
              privacy: str, requested_privacy: list | tuple | None = None) -> dict:
    policy = load_policy(workspace)
    if (action not in policy["allowed_actions"]
            or expected_revision != policy["policy_revision"]
            or privacy not in policy["allowed_privacy"]):
        raise PermissionError("reference-outside-current-policy")
    if requested_privacy is not None:
        if (not isinstance(requested_privacy, (list, tuple))
                or not all(isinstance(item, str) for item in requested_privacy)
                or len(requested_privacy) > len(PRIVACY)
                or not set(requested_privacy) <= set(policy["allowed_privacy"])
                or privacy not in requested_privacy):
            raise PermissionError("request-cannot-expand-current-policy")
    return policy
