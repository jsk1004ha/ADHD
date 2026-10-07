---
type: artifact
domain: artifact
layer: record
privacy: private
status: inbox
template: true
id: "{{id}}"
created: "{{date}}"
updated: "{{date}}"
source_ids:
  - "{{source_id}}"
topics: []
projects:
  - "{{project_id}}"
artifact_kind: run_report
source_root: ""
source_kind: file
source_uri: ""
verified_at: ""
verification_scope: ""
evidence_strength: unverified
---
# {{title}}

## 상황과 적용 범위
조건·작업·대상 revision을 쓴다.

## 관찰과 결과
관찰한 결과와 정본의 ID/commit/파일/hash를 짧게 쓴다.

## 사용자 평가
미수집. 명시적 평가가 있을 때만 긍정/부정/혼합, 평가 축, 원문 출처, 대상 revision, task/project 범위를 쓴다.

## 검증과 한계
passed/failed/unverified와 실제 검사 receipt, 검증 시점·범위를 쓴다. 만족도를 기술 성공으로 바꾸지 않는다.

## 다음 행동
필요할 때만 한 줄 쓴다.

## 근거
정본 포인터와 사용 조건을 쓴다. 대화·코드·로그 전체를 복사하지 않는다.

## 결정 확장
선택·대안·선택 이유·적용 조건·재검토 조건.
