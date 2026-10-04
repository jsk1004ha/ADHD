# 대규모 작업 실행

대규모 작업은 원문 요구와 작업 DAG를 영속화하고, 충돌 없는 준비된 파트를
격리 공간에 나눠 실행한 뒤 한 통합본을 검사합니다. 일반 작업은 기존 단일
작성자 경로를 유지하며 `batch` 명령으로 같은 일괄 검사 정책을 사용할 수 있습니다.

## 실제 연결

- `large_tasks.py`: SQLite 작업 정본, 요구 원문 발췌, 의존성·인터페이스 버전,
  핵심 경로 우선순위, 소유권·임대 세대, 취소, 통합·후보 고정·복구.
- `large_execution.py`: 실제 Codex CLI 프로세스를 `-C` 지정 작업 공간에서
  실행하고 종료를 관측합니다. 정상 결과만 범위를 검사해 제출·직렬 통합합니다.
- `validation_batch.py`: 제작 완료 후 검사 계획과 대상 파일·환경을 고정하고
  명시적 argv를 실행합니다. 전체 로그와 실행 영수증을 저장합니다.
- `large_native.py`: 원래 native 계약에 작업 정본을 연결하고, 최종 후보와
  독립 승인 시 현재 통합본·배치 근거를 대조합니다. 훅에서 Git·검사를 실행하지 않습니다.

Git 작업은 실제 worktree, 문서·연구처럼 Git이 없는 작업은 선언된 입력의
복사 작업 공간을 사용합니다. 공유 API·스키마·타입·락파일·DB·포트와 바이너리
문서 조립은 한 소유자에게 배정하세요. 복사 공간과 worktree는 보안 샌드박스가
아닙니다. CLI의 `workspace-write` 정책과 실제 도구 권한을 함께 적용합니다.
외부 연결의 권한과 부작용은 파일 소유권만으로 차단되지 않습니다.

App 훅은 모든 구현자의 작업 디렉터리와 도구 쓰기 범위를 상관시킬 수 없어
기존 App 단일 구현자 가드를 유지합니다. 병렬 작성은 관측 가능한 CLI
프로세스 경로를 사용합니다. 설치·인증 파일이나 부모 모델을 변경하지 않습니다.
구현자는 기존 `adhd-implementer` 모델·추론 핀을 사용하고 새 제공자를 추가하지 않습니다.

소비자의 착수 조건은 `contract_ready` 또는 `artifact_ready`입니다. `accepted`는
전체 통합본의 최종 인수 상태이므로 같은 실행의 의존성에 사용할 수 없습니다.
이 잘못된 조건은 모든 제작을 마친 뒤 승인하는 흐름과 교착하므로 초기화 시 거부합니다.

## 실행 순서

에이전트가 이 명령들을 수행합니다. 사용자에게 각 단계의 실행을 넘기지 않습니다.
`python`은 실제 확인한 Python 실행 경로, `PROJECT`는 실제 작업 루트입니다.
예시는 복사해 원문 요구·소유 경로·검사 명령을 실제 과제에 맞게 채우세요.
예시 모듈과 검사 파일을 자동으로 만들어 주는 템플릿은 아닙니다.

```powershell
python adhd.py large init --workspace PROJECT --run-id example-large --payload-file examples/large-task.json
python adhd.py large run-workers --workspace PROJECT --run-id example-large --codex ABSOLUTE_CODEX_EXE --payload-file examples/large-limits.json
python adhd.py large freeze --workspace PROJECT --run-id example-large
python adhd.py large validate --workspace PROJECT --run-id example-large --payload-file examples/batch-checks.json
python adhd.py large status --workspace PROJECT --run-id example-large
```

`init --session SESSION`은 실제 native 상태에서 원문·계약 버전·해시를 가져오며
현재 계획과 모든 요구의 할당을 요구합니다. 기존 `native begin`과 깊은 계획 뒤에
작업 정본을 초기화하고 다음 요청을 연결합니다.

```json
{"run_id":"CURRENT_NATIVE_RUN_ID"}
```

```powershell
python adhd.py native attach-large --session SESSION --workspace PROJECT --payload-file attach.json
```

최종 `native candidate`는 기존 파일·기준별 결과에 다음 필드를 추가합니다.
대규모 정본에서 모든 파트가 현재 배치로 검증되어야 후보를 받을 수 있습니다.

```json
{"large_task_evidence":{"run_id":"CURRENT_NATIVE_RUN_ID","snapshot_digest":"FREEZE_SNAPSHOT_DIGEST","batch_report":".adhd/batches/BATCH_ID/report.json"}}
```

`batch_report`는 **통합 작업 공간 기준 경로**입니다. `large validate`가 실제
통합 공간과 이 경로를 반환합니다. 최종 reviewer는 원문 전체, 현재 통합본과
영수증을 읽습니다. 작성자의 완료 선언만으로 인수되지 않습니다. native 승인
후 실제 산출물은 표시된 통합 공간에서 사용할 수 있으며, 새 로컬 목적지로
복사하려면 `large publish`에 명시적인 원본→새 목적지 매핑을 줍니다. 기존 파일을
덮어쓰지 않습니다. 외부 배포·메시지 발송은 이 흐름에 포함되지 않습니다.

## 동시성·예산

`large-limits.json`은 실제 호스트·정책·자원·남은 운영 예산의 슬롯을 지정합니다.
기본값은 관측되지 않은 가용성을 추측하지 않도록 1이며 지원 상한은 6입니다.
준비된 파트 수, 공유 자원, 핵심 경로, 통합 적체와 실행 중 작업 수에 따라
실제 폭을 줄입니다. 비어 있는 슬롯을 중복 작업으로 채우지 않습니다.
API 한도·CPU/RAM을 자동 측정했다고 주장하지 않습니다. 실제 가용성을 확인한
Director가 한 번의 제한 입력을 갱신하고 상태 이벤트에서 재배정합니다.
native 연결에서는 현재 native 정책의 남은 동시성·호출 예산도 적용합니다.

검사 프로세스 병렬도는 `process_slots`로 별도 제한합니다. CLI가 사용량을
보고하면 보존하고, 보고하지 않으면 `unmeasured`와 `tokens: null`로 남깁니다.
호출 수·작업 시간·문맥 길이는 대체 지표이며 하드 토큰 상한이나 절감률이 아닙니다.

## 검사는 제작 뒤 한 배치

검사 계획은 제작 중 준비합니다. 기존 필수 검사, 변경 동작과 회귀, 핵심
사용자 흐름·실패 경로가 요구를 충족하도록 최소 충분 집합을 선택합니다.
각 검사에 요구 ID·대상 파일·명령·선행 검사·공유 자원을 선언합니다. `mandatory_checks`
와 원문 요구를 누락한 계획은 거부합니다. 저장소의 필수 CI·보안 검사를 빼는
정책이 아닙니다. 로컬 검사 프로세스 자체가 모델 추론 토큰인 것은 아닙니다.

첫 검사 계획은 작업 정본에 고정됩니다. 같은 계약의 수정 배치에서는 기존
검사·명령·대상·필수 조건을 제거하거나 약화할 수 없고 필요한 검사를 추가할 수
있습니다. 계약 변경은 실제 사용자 변경과 함께 별도 버전으로 기록합니다.

대규모 후보의 파일은 별도 스트리밍 한도를 사용합니다. 기본 상한은 20,000개,
단일 파일 64 GiB, 총 128 GiB, 작업 manifest 8 MiB입니다. 일반 파일·문서의
기존 한도는 유지합니다. native 훅은 고정된 목록과 Git 메타데이터도 읽어
예상하지 않은 파일 추가·삭제·HEAD/index 변경을 승인 전에 거부합니다.

독립 검사는 다른 검사가 실패해도 계속합니다. 선행 빌드 실패로 불가능한
검사는 `blocked`, 시간 초과는 `timeout`, 변경된 대상은 `stale`로 구별합니다.
전체 로그는 저장하고 모델에는 실패 원인 후보와 대표 위치만 전달합니다.
같은 메시지가 다른 위치·소유자에서 나온 경우 별도 후보로 유지합니다.
QA는 후보를 확인·분리한 뒤 한 원인에 한 소유자를 지정해 다음처럼 모아 수정합니다.

```json
{"groups":[{"id":"F1","task_id":"search","owner_package":"search","confidence":"confirmed","suspected_cause":"Concrete confirmed cause","affected_checks":["search-flow"],"evidence_ref":"Actual batch report / receipt path"}]}
```

```powershell
python adhd.py large repairs --workspace PROJECT --run-id RUN --payload-file confirmed-repairs.json
python adhd.py large run-workers --workspace PROJECT --run-id RUN --payload-file actual-limits.json
python adhd.py large validate --workspace PROJECT --run-id RUN --payload-file repaired-checks.json
```

실패한 파트와 소비자의 기존 제출은 무효화됩니다. 새 통합본에서 실패 검사,
영향 검사와 `critical` 검사를 재실행합니다. `previous_report` 재사용은 계획·계약·
입력·환경·실제 영수증이 유지되고 검사에 `impact_complete: true`가 명시된 경우만
허용됩니다. 영향 범위가 불확실하면 해당 배치를 다시 실행하는 기본값을 유지합니다.
S0 결과를 S1 전체의 통과로 바꾸지 않습니다. 필요한 수정 배치는 반복할 수 있습니다.

일반 작업도 다음 실행으로 배치를 사용할 수 있습니다. spec에는 실제
`run_id`, `contract_revision`, `contract_hash`, `requirements`, `checks`를 채웁니다.

```powershell
python adhd.py batch run --workspace PROJECT --spec-file ordinary-checks.json
python adhd.py batch verify --workspace PROJECT --report .adhd/batches/BATCH_ID/report.json
```

## 중단·재개와 적용 범위

`large cancel`은 요청만 기록합니다. 실제 프로세스 종료를 확인하기 전에는
같은 자원을 재배정하지 않습니다. CLI 감독자는 자신이 시작한 프로세스 트리만
종료합니다. 재시작 시 `large reconcile`로 기록과 실제 상태를 대조하고, 불확실한
실행을 격리합니다. PID만 보고 다른 프로세스를 종료하거나 종료 근거를 만들지 않습니다.
사용자 변경은 먼저 native 의도에 반영하고 `large amend --session`으로 DAG를
다시 연결합니다. 오래된 계약·인터페이스·임대 세대 제출은 거부됩니다.

SQLite 이벤트와 상태에는 결정·이유, 현재 파트, 계약/인터페이스, 원인별 수정과
실행 근거가 남습니다. `large events`로 필요한 구간을 읽을 수 있습니다.
App 종료 뒤 계속 실행되는 서비스를 제공하지 않습니다. 이 구현과 동시성·영어
지시·Ponytail의 실제 속도/토큰 효과는 구별해야 하며, 절감률을 사전 보장하지 않습니다.

저장소에서 `python adhd.py ...`를 실행하면 새 구현을 사용합니다. 이미 실행 중인
설치 릴리스는 코드 수정만으로 바뀌지 않습니다. 관리 업그레이드와 새 세션이
필요한 경우 기존 설치기의 검토·보존·롤백 경로를 사용하고 훅 신뢰를 우회하지 않습니다.
