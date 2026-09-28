"""User-selected September 2026 model policy; no silent provider fallback."""
from __future__ import annotations
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
def route(role: str) -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError('Unknown ADHD role: ' + role)
    kind, sandbox, description = ROLES[role]
    return {**MODELS[kind], 'kind': kind, 'sandbox': sandbox, 'description': description}

def migrate_role(name: str) -> str:
    """Migration concerns role duties, not a search-and-replace of all models."""
    key = name.lower().replace('_','-')
    if key in {'architect','ideator'}:
        return 'astra'
    if key in {'explore','explorer','scout','researcher','librarian','document-reader','git-master'}:
        return 'luna'
    return 'sol'
