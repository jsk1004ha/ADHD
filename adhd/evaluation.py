"""Repeatable fixture evaluation for representative personal task shapes.

These are subprocess test executions. Live GUI, account and human learning
outcomes are deliberately reported as not measured.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import subprocess
import sys
import time

from . import __version__
from .core import ROOT, atomic_json
from .models import execution_profile as profile_settings


SCENARIOS = (
    ('document_pages', 'tests.test_document_evidence.DocumentEvidenceTests.test_exact_pages_uses_actual_pdf'),
    ('editable_presentation', 'tests.test_editable_elements.EditableElementTests.test_rasterized_chart_with_decoy_text_rejected'),
    ('research_number', 'tests.test_v012.ProvenanceTests.test_mismatched_report_number_rejected'),
    ('research_completion_gate', 'tests.test_v012.NativeProvenanceGateTests.test_candidate_requires_manifest_and_real_subject_bound_check'),
    ('coding_regression', 'tests.test_harness.LoopTests.test_missing_artifact_not_success'),
    ('intent_replacement', 'tests.test_native_v011.NativeV011Tests.test_user_replacement_and_retraction_are_revisioned'),
    ('interruption_recovery', 'tests.test_native_v011.NativeV011Tests.test_interrupt_waits_for_running_child_before_release'),
    ('mcp_usable_state', 'tests.test_v12.ExtensionTests.test_mock_handshake_without_real_call_stays_disabled'),
    ('mcp_read_call', 'tests.test_extension_launch.ExtensionLaunchTests.test_reviewed_read_only_call_enables_only_after_result'),
    ('study_self_check', 'tests.test_v012.LearningModeTests.test_study_needs_self_check_but_production_does_not'),
    ('installed_release_digest', 'tests.test_v012_install_integrity.NativeInstallIntegrityTests.test_upgrade_refuses_noop_when_installed_release_is_tampered'),
    ('mcp_local_dependency', 'tests.test_extension_launch.ExtensionLaunchTests.test_script_helper_changed_after_review_rejected'),
    ('mcp_external_python_path', 'tests.test_extension_launch.ExtensionLaunchTests.test_python_interpreter_env_is_rejected_and_external_path_is_not_inherited'),
    ('research_role_alias', 'tests.test_v012.ProvenanceTests.test_raw_file_cannot_impersonate_analysis_code'),
    ('mcp_unrelated_result', 'tests.test_v012.HostObservationTests.test_unrelated_mcp_result_cannot_satisfy_criterion'),
    ('auth_recovery', 'tests.test_v012.RecoveryTests.test_auth_failure_cannot_be_fabricated_and_blocks_retry'),
    ('app_verifier_lifecycle', 'tests.test_native.NativeTests.test_app_observed_lifecycle_can_complete_without_pretooluse'),
    ('app_verifier_profile_change', 'tests.test_native.NativeTests.test_app_observed_lifecycle_rejects_profile_change'),
    ('wiki_stale_refresh', 'tests.test_skill_discovery.SkillDiscoveryTests.test_stale_router_requests_bounded_refresh_and_reports_failure'),
)

PROFILE_SCENARIOS = {
    'simple': 'tests.test_execution_profiles.ExecutionProfileTests.test_simple_direct_or_one_child_and_independent_review',
    'standard': 'tests.test_execution_profiles.ExecutionProfileTests.test_standard_accepts_brief_plan_and_requires_it_for_candidate',
    'deep': 'tests.test_execution_profiles.ExecutionProfileTests.test_deep_needs_full_plan_current_target_check_and_evidence_review',
}


def run_evaluation(output: Path, *, root: Path = ROOT,
                   scenarios: tuple[tuple[str, str], ...] = SCENARIOS,
                   execution_profile: str = 'standard') -> dict:
    profile = profile_settings(execution_profile)
    if scenarios is SCENARIOS:
        scenarios = (*scenarios, ('execution_profile', PROFILE_SCENARIOS[execution_profile]))
    root = Path(root).resolve()
    output = Path(output).resolve()
    if not output.is_relative_to(root):
        raise ValueError('Evaluation scorecard must stay in the source workspace')
    if not scenarios or len(scenarios) > 30 or len({name for name, _ in scenarios}) != len(scenarios):
        raise ValueError('Evaluation needs distinct bounded scenarios')
    cases = []
    for name, test_id in scenarios:
        if not name or not test_id.startswith('tests.') or any(c.isspace() for c in test_id):
            raise ValueError('Invalid scenario identifier')
        started = time.monotonic()
        try:
            process = subprocess.run([sys.executable, '-m', 'unittest', test_id],
                                     cwd=root, capture_output=True, timeout=90, check=False)
            exit_code = process.returncode
            output_bytes = process.stdout + process.stderr
            status = 'passed' if exit_code == 0 else 'failed'
        except subprocess.TimeoutExpired as error:
            exit_code = None
            output_bytes = bytes(error.stdout or b'') + bytes(error.stderr or b'')
            status = 'timed_out'
        cases.append({'id': name, 'test_id': test_id, 'status': status,
                      'exit_code': exit_code, 'elapsed_seconds': round(time.monotonic()-started, 3),
                      'output_sha256': hashlib.sha256(output_bytes).hexdigest(),
                      'output_excerpt': output_bytes.decode('utf-8', errors='replace')[-800:]})
    passed = sum(row['status'] == 'passed' for row in cases)
    scorecard = {
        'schema': 1, 'harness_version': __version__,
        'execution_profile': profile,
        'profile_evaluation': 'Shared regression fixtures; live profile quality, latency and cost are not measured.',
        'measured_at': datetime.now(timezone.utc).isoformat(),
        'assurance': 'local_fixture_subprocess',
        'overall': 'passed_fixture_suite' if passed == len(cases) else 'failed_fixture_suite',
        'cases': cases, 'metrics': {
            'passed': passed, 'total': len(cases), 'failed_or_timed_out': len(cases)-passed,
            'retry_count': 0, 'human_corrections': None, 'human_review_minutes': None,
            'model_usage_tokens': None, 'price_per_verified_task': None,
            'independent_learning_transfer': None,
        },
        'live_integrations': {name: 'not_run' for name in
            ('Codex_App_custom_verifier', 'Office_or_Hancom_render', 'browser_user_flow',
             'credentialed_MCP_tool', 'human_learning_transfer')},
        'limitation': 'Fixture success does not prove live GUI, account, model, human learning or scientific validity.',
    }
    atomic_json(output, scorecard)
    return scorecard
