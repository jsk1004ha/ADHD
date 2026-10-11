"""User-selected September 2026 model policy; no silent provider fallback."""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any
MODELS = {
    'sol': {'model': 'gpt-6-sol', 'effort': 'max'},
    'luna': {'model': 'gpt-6-luna', 'effort': 'max'},
    'astra': {'model': 'gpt-6-astra', 'effort': 'low'},
}
ROLES = {
    'adhd-planner': ('sol', 'read-only', 'Deep bounded plan before implementation: intent, evidence, alternatives, risks, dependencies and acceptance.'),
    'adhd-scout': ('luna', 'read-only', 'Narrow evidence collection, repository navigation and extraction.'),
    'adhd-light': ('luna', 'read-only', 'Small bounded explanation, formatting suggestion or fact extraction.'),
    'adhd-architect': ('astra', 'read-only', 'Hard design, research synthesis, hypotheses and reasoning; never implementation.'),
    'adhd-implementer': ('sol', 'workspace-write', 'Implement the approved intent. The only delegated writer.'),
    'adhd-verifier': ('sol', 'read-only', 'Independent review of exact intent and fresh artifact snapshot.'),
}

EXECUTION_PROFILES = {
    'simple': {'name': 'simple', 'plan_depth': 'optional',
               'review_depth': 'independent', 'delegation': 'direct_or_one_child'},
    'standard': {'name': 'standard', 'plan_depth': 'brief',
                 'review_depth': 'independent', 'delegation': 'bounded'},
    'deep': {'name': 'deep', 'plan_depth': 'deep',
             'review_depth': 'strengthened', 'delegation': 'bounded'},
}


def execution_profile(name: str = 'standard') -> dict[str, str]:
    """Execution intensity only; model and effort remain the selected role pins."""
    if name not in EXECUTION_PROFILES:
        raise ValueError('Unknown execution profile: ' + str(name))
    return dict(EXECUTION_PROFILES[name])


# Explicit opt-in, frozen by the native run; base role pins remain unchanged.
EFFORT_OVERRIDABLE = frozenset({'adhd-scout', 'adhd-light'})
EFFORT_VALUES = frozenset({'low', 'max'})


def validate_role_effort(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError('role_effort must be an object')
    for role, effort in value.items():
        if role not in EFFORT_OVERRIDABLE:
            raise ValueError('role_effort may only change: ' + ', '.join(sorted(EFFORT_OVERRIDABLE)))
        if not isinstance(effort, str) or effort not in EFFORT_VALUES:
            raise ValueError('role_effort value must be low or max')
    return dict(value)


def read_role_effort(policy_path: Path) -> dict[str, str]:
    if not policy_path.exists():
        return {}
    if policy_path.is_symlink() or policy_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('native-policy.json is unsafe or too large')
    try:
        data = json.loads(policy_path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError('Invalid native-policy.json: ' + str(error)) from error
    if not isinstance(data, dict):
        raise ValueError('native-policy.json must be an object')
    return validate_role_effort(data.get('role_effort', {}))


def apply_role_effort(role: str, toml_text: str, overrides: dict | None = None) -> str:
    effort = validate_role_effort({} if overrides is None else overrides).get(role)
    if effort is None:
        return toml_text
    result, count = re.subn(r'(?m)^model_reasoning_effort = "[^\"]*"',
                           'model_reasoning_effort = "' + effort + '"', toml_text, count=1)
    if count != 1:
        raise ValueError('Agent template lacks model_reasoning_effort: ' + role)
    return result


def route(role: str, role_effort: dict | None = None) -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError('Unknown ADHD role: ' + role)
    kind, sandbox, description = ROLES[role]
    choice = {**MODELS[kind], 'kind': kind, 'sandbox': sandbox, 'description': description}
    overrides = validate_role_effort({} if role_effort is None else role_effort)
    if role in overrides:
        choice['effort'] = overrides[role]
    return choice

def migrate_role(name: str) -> str:
    """Migration concerns role duties, not a search-and-replace of all models."""
    key = name.lower().replace('_','-')
    if key in {'architect','ideator'}:
        return 'astra'
    if key in {'explore','explorer','scout','researcher','librarian','document-reader','git-master'}:
        return 'luna'
    return 'sol'
