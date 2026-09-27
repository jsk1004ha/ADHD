---
name: verification-before-completion
description: Use before claiming completion, a fix, or passing checks. Evidence before claims.
---
# Verification Before Completion — Raibit adaptation

Derived from obra/superpowers skills/verification-before-completion/SKILL.md,
blob 7d45333cc4a49c57a80df6c1fe2fa777a207afbc; MIT, Copyright (c) 2025 Jesse Vincent.
Changes: shortened duplicate admonitions; added research/document and native receipt checks.
This is a selectively loaded instruction module, not a deterministic test runner.

## The Iron Law
NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE.

## The Gate Function
Before claiming a status:
1. IDENTIFY: What command or observable evidence proves this claim?
2. RUN: Execute the FULL relevant command, fresh and complete.
3. READ: Inspect the full output, exit code, and failures.
4. VERIFY: Does the actual output support the claim? If not, report the real state.
5. ONLY THEN: State the claim with the evidence.

## Evidence requirements
| Claim | Required | Not sufficient |
|---|---|---|
| Tests pass | Actual output showing zero failures | Previous run or confidence |
| Linter clean | Actual relevant linter output | Partial-file check extrapolated to everything |
| Build succeeds | Actual build with exit zero | Linter passing |
| Bug fixed | Reproduce original symptom and verify correction | Code was edited |
| Regression test useful | Demonstrate red/green when feasible | Test passes once |
| Agent completed | Inspect actual artifacts/diff and checks | Agent says success |
| Requirements met | Original request and every acceptance criterion checked | Tests pass |

## Delegation
An implementation agent reports what it changed and checked. The integrator verifies those
claims; the final verifier separately compares the original request with the real artifacts.
Do not edit acceptance criteria, weaken tests, or mark reviewer-owned checks to obtain a pass.

## Raibit additions
Research: distinguish measured, simulated, calculated and inferred quantities. Locate every
material external claim in its actual source. Never manufacture missing measurements.
Documents: open the user's actual template, render every final page, inspect layout and text.
Games: exercise controls, collision, restart and a representative performance path, not just a screenshot.
Native runtime: queued requests are not success. Wait for host acknowledgement. Completion
requires the current candidate digest and a genuine separate verifier subagent's result.
An artifact edited after review needs another candidate and another review.

Do not imply that mocks, Linux tests or a model's review prove real Windows/App/external-account
integration. Report the exact tested scope and unresolved limits.
