"""Versioned, bounded edits to a user's active acceptance contract."""
from __future__ import annotations

from copy import deepcopy
from typing import Any
from .delivery_policy import merge_deadline, validate_contract_metadata


def apply_intent_patch(contract: dict, patch: dict, pending_turns: list[str]) -> tuple[dict, list[str]]:
    """Return a new contract and changed targets; never edit the input in place."""
    if not isinstance(patch, dict) or patch.get('source_turn_id') not in pending_turns:
        raise ValueError('Intent patch needs a pending, observed user turn')
    if patch.get('base_revision') != contract.get('intent_version'):
        raise ValueError('Intent patch uses a stale contract revision')
    classification = patch.get('classification')
    if classification not in {'no_change', 'amend', 'new_task'}:
        raise ValueError('Unknown intent classification')
    operations = patch.get('operations', [])
    if not isinstance(operations, list) or len(operations) > 50:
        raise ValueError('Intent needs at most 50 operations')
    if classification == 'no_change':
        if operations:
            raise ValueError('Status-only messages cannot change a contract')
        return deepcopy(contract), []
    if classification == 'new_task':
        if operations:
            raise ValueError('New tasks use a new begin contract')
        return deepcopy(contract), ['new_task']
    if not operations:
        raise ValueError('An amendment needs an add, replace or retract operation')
    updated = deepcopy(contract)
    changed: list[str] = []
    for row in operations:
        if not isinstance(row, dict) or set(row) - {'op', 'target', 'value'}:
            raise ValueError('Invalid intent operation')
        action, target = row.get('op'), row.get('target')
        if action not in {'add', 'replace', 'retract'} or not isinstance(target, str):
            raise ValueError('Unknown intent operation')
        target = target.lstrip('/')
        if '/' not in target:
            raise ValueError('Intent target needs an allowlisted collection and key')
        group, key = target.split('/', 1)
        if group not in {'criteria', 'artifacts', 'documents', 'protected_inputs',
                         'preserve_conditions', 'delivery_target', 'runtime_context', 'deadline'} or not key:
            raise ValueError('Intent target is not allowlisted')
        value: Any = row.get('value')
        if group == 'criteria':
            rows = updated['criteria']
            index = next((i for i, item in enumerate(rows) if item['id'] == key), None)
            if action == 'add':
                if index is not None or not isinstance(value, dict) or value.get('id') != key:
                    raise ValueError('Invalid or duplicate criterion addition')
                rows.append(value)
            elif index is None:
                raise ValueError('Unknown criterion')
            elif action == 'replace':
                if not isinstance(value, dict) or value.get('id') != key:
                    raise ValueError('Criterion ID cannot be changed by replacement')
                rows[index] = value
            else:
                rows.pop(index)
        elif group == 'artifacts':
            rows = updated['artifacts']
            exists = key in rows
            if action == 'add':
                if exists or value not in (None, key):
                    raise ValueError('Invalid or duplicate artifact')
                rows.append(key)
            elif not exists:
                raise ValueError('Unknown artifact')
            elif action == 'retract':
                rows.remove(key)
            else:
                if not isinstance(value, str) or not value or value in rows:
                    raise ValueError('Replacement artifact must be a distinct path')
                rows[rows.index(key)] = value
        elif group == 'documents':
            rows = updated['documents']
            if '/' in key and key.rsplit('/', 1)[1] in {
                'max_pages', 'exact_pages', 'max_chars', 'min_chars',
                'count_whitespace', 'editable_required', 'editable_elements',
                'layout_checks'}:
                path, field = key.rsplit('/', 1)
                index = next((i for i, item in enumerate(rows) if item['path'] == path), None)
                if index is None or action == 'add' and field in rows[index]:
                    raise ValueError('Document field missing or already exists')
                if action == 'retract':
                    if field not in rows[index]:
                        raise ValueError('Unknown document field')
                    del rows[index][field]
                else:
                    rows[index][field] = value
            else:
                index = next((i for i, item in enumerate(rows) if item['path'] == key), None)
                if action == 'add':
                    if index is not None or not isinstance(value, dict) or value.get('path') != key:
                        raise ValueError('Invalid document addition')
                    rows.append(value)
                elif index is None:
                    raise ValueError('Unknown document')
                elif action == 'replace':
                    if not isinstance(value, dict) or value.get('path') != key:
                        raise ValueError('Document path change needs an artifact operation')
                    rows[index] = value
                else:
                    rows.pop(index)
        elif group == 'protected_inputs':
            rows = updated['protected_inputs']
            exists = key in rows
            if action == 'add':
                if exists or not isinstance(value, str):
                    raise ValueError('Invalid protected input addition')
                rows[key] = value
            elif not exists:
                raise ValueError('Unknown protected input')
            elif action == 'replace':
                if not isinstance(value, str):
                    raise ValueError('Protected input replacement needs a hash')
                rows[key] = value
            else:
                del rows[key]
        elif group == 'preserve_conditions':
            rows = updated.setdefault('preserve_conditions', [])
            index = next((i for i, item in enumerate(rows) if item['id'] == key), None)
            if action == 'add':
                if index is not None or not isinstance(value, dict) or value.get('id') != key:
                    raise ValueError('Invalid or duplicate preservation condition')
                rows.append(value)
            elif index is None:
                raise ValueError('Unknown preservation condition')
            elif action == 'replace':
                if not isinstance(value, dict) or value.get('id') != key:
                    raise ValueError('Preservation ID cannot change by replacement')
                rows[index] = value
            else:
                rows.pop(index)
        else:
            if key != 'value':
                raise ValueError('Singleton intent target must end in /value')
            existing = updated.get(group)
            if action == 'add' and existing is not None:
                if group == 'deadline' and isinstance(value, dict) and existing.get('due_at') == value.get('due_at'):
                    # Repeated wording never starts a new clock.
                    continue
                raise ValueError('Intent field already exists; use explicit replacement')
            if action == 'replace' and existing is None:
                raise ValueError('Unknown intent field')
            if action == 'retract':
                if existing is None:
                    raise ValueError('Unknown intent field')
                updated.pop(group)
            elif group == 'deadline':
                updated[group] = merge_deadline(existing, value, explicit_change=action == 'replace')
            else:
                updated[group] = value
        changed.append(target)
    if not updated['criteria'] or not updated['artifacts']:
        raise ValueError('Active contract needs criteria and artifacts')
    validate_contract_metadata(updated, updated['criteria'])
    if not changed:
        return updated, changed
    updated['intent_version'] = contract['intent_version'] + 1
    return updated, changed
