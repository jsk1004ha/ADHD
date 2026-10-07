# ADHD — Autonomous Delegation Harness Director

ADHD v0.1.6는 Codex 앱과 CLI에서 실행하는 로컬 하네스입니다. Codex 후크를 통해 계획, 실행 근거, 검토 단계를 연결합니다. 기존 인증, 선택 모델, 라우터, 플러그인, 스킬, 개인 설정을 설치기가 덮어쓰지 않도록 설계했습니다.


[0.1.6 변경 내역](docs/releases/0.1.6.md)에 지속적인 goal 실행, 내장 목표 연결, 선택적인 Obsidian 문맥·경험 기록을 정리했습니다. 소스 목록 검사와 저장공간 정리는 [0.1.5](docs/releases/0.1.5.md), 대규모 실행·종료·프로필·MCP·작업 흐름 스킬은 [0.1.4](docs/releases/0.1.4.md)를 참고하세요.
이 공개 저장소에는 소스와 일반적인 기본 설정만 있습니다. 계정 인증 파일, 개인 `config.toml`, 실행 기록, 개인 위키 파일은 포함하지 않습니다. Python 패키지, 스킬, 역할, 후크와 설치 경로의 이름을 ADHD로 통일했으며 이전 명령 별칭은 제공하지 않습니다. 유효한 상태 기록이 하나로 식별되는 경우에만 기존 세션 상태를 원래 위치에서 계속 사용합니다.

## 준비와 설치

Python 3.11 이상과 기존 Codex 앱 또는 CLI가 필요합니다. 관리자 권한, 별도의 유료 서비스 가입, 설치 중 다운로드는 필요하지 않습니다. 이 저장소의 코드를 검토한 뒤 PowerShell에서 실행하세요.

```powershell
py -3 .\adhd.py doctor
py -3 .\adhd.py upgrade
```

macOS/Linux에서는 `python3 adhd.py upgrade`를 사용합니다. `install.ps1`, `install.sh`도 같은 업그레이드를 수행합니다. 새 설치와 기존 관리 설치 이전에 사용할 수 있습니다. 설치기는 릴리스를 `$CODEX_HOME/adhd/releases`에 복사하고, `hooks.json`에 후크 9개와 `adhd-*` 스킬·역할을 추가하며, 아래 기본 번들도 설치합니다. 설치 기록과 릴리스 해시, 관리 파일 백업을 검증한 뒤 이전 설치를 옮깁니다.

새 후크가 ‘검토 필요’로 표시되면 Codex의 `/hooks`에서 실제 명령을 확인하고 신뢰 승인하세요. 승인 상태를 우회하지 않습니다. 업그레이드 후에는 새 앱/CLI 세션에서 후크와 역할 설정을 다시 로드해야 합니다.

```powershell
py -3 .\adhd.py doctor
py -3 .\adhd.py rollback-native
```

`rollback-native`는 설치 뒤 사용자가 관리 파일을 바꿨다면 자동 복원을 거부합니다. 별도 평가에는 `--codex-home PATH`를 사용하세요.

이 체크아웃 또는 설치된 릴리스 폴더에서 `python -m adhd.storage scan`으로 오래된 관리 릴리스의 정리 대상을 확인합니다. `python -m adhd.storage prune --apply`를 지정할 때만 검증된 미참조 릴리스를 삭제합니다. 현재·최근 복구용 버전과 알려진 채팅·설정 참조를 보호하며 백업과 작업 기록은 보존합니다. `--keep-releases N`은 최소 2개이고 참조된 버전은 추가로 남깁니다. 업그레이드 자체는 이전 릴리스를 삭제하지 않습니다.

## ADHD 작업을 돕는 스킬

아래 스킬은 기존 ADHD 실행의 필요한 지점에서 사용합니다. 모든 스킬을 순서대로 실행할 필요는 없습니다.

| 호출 | 사용 시점과 결과 |
| --- | --- |
| [`$adhd-goal`](skills/adhd-goal/SKILL.md) | 지정한 목표가 독립 검증을 통과할 때까지 구현·점검·수정을 반복 |
| [`$adhd-shape`](skills/adhd-shape/SKILL.md) | 막연한 아이디어를 대상·범위·완료 기준이 있는 실행 브리프로 구체화 |
| [`$adhd-challenge`](skills/adhd-challenge/SKILL.md) | 계획의 중요한 가정·위험을 근거와 작은 검증으로 점검 |
| [`$adhd-decide`](skills/adhd-decide/SKILL.md) | 실제 대안을 비교하고 선택 이유·영향·재검토 조건을 기록 |
| [`$adhd-steer`](skills/adhd-steer/SKILL.md) | 새 피드백을 기존 요구사항에 반영하고 계획·진행 작업을 조정 |
| [`$adhd-unblock`](skills/adhd-unblock/SKILL.md) | 실패 원인을 좁히고 검증 가능한 복구로 작업을 재개 |
| [`$adhd-optimize`](skills/adhd-optimize/SKILL.md) | **코드 최적화**: 동작을 보존하며 성능·자원 사용·구조를 개선하고 검증 |
| [`$adhd-retro`](skills/adhd-retro/SKILL.md) | 결과·피드백을 실제 수정과 근거 있는 교훈으로 연결 |

예: `$adhd-shape 연구 기록을 정리하는 도구 아이디어를 구체화해 줘`.
명확하고 작은 요청은 바로 처리하며, 구체화·비교·검토만 요청하면 파일을 바꾸지 않습니다.
이미 구현을 요청했다면 필요한 보조 결과를 기존 실행에 넘겨 계속 진행합니다.
[공통 인계 규칙](skills/adhd-native/references/skill-handoff.md)은 설명용 기록과 실제 native 명령을 구분합니다.
`retro`의 교훈을 장기 메모리에 저장하려면 사용자의 명시적 요청이 필요합니다.

일반 `upgrade`가 아래 스킬과 호출 메타데이터·공통 참조를 설치합니다.
이름이 같은 사용자 폴더는 전체 보존하고, 관리 릴리스의 사본은 ADHD 로컬 탐색의 대안 경로로 남깁니다.
기존 위키 라우터와 Codex가 제공한 스킬 목록의 선택 권한은 유지합니다.
업그레이드 뒤 새 Codex 세션에서 `$adhd-*` 호출 목록을 다시 불러오세요.
`builtin apply`는 아래 공개 번들과 MCP만 적용합니다.

`/goal 원하는 결과` 또는 `$adhd-goal 원하는 결과`로 목표 루프를 시작합니다.
원래 요청과 독립 검증 절차를 유지하며, 목표 미달이나 검증 거절 시 수정하고 다시 점검합니다.
기본 반복 횟수·시간·무진전·재개·누적 자식 호출 제한으로 자동 종료하지 않습니다.
실행 시작 때 `native-policy.json`에 명시한 제한, 동시 자식 수와 Astra 상담 제한은 유지합니다.
사용자 중단·취소, 일시 정지, 필수 조건을 해결할 수 없는 상태, 세션 종료 시 멈춥니다.
반복 실패에는 접근 방법을 바꾸며, 진척이나 연구 결론을 만들어내지 않습니다.
호스트의 내장 `/goal`은 같은 세션의 실제 목표 기록을 통해 ADHD에 연결되며,
`$adhd-goal`로도 직접 실행할 수 있습니다.
실행 중인 앱과 정상적인 생명주기 훅이 필요합니다.

## Obsidian 문맥과 경험 기록

기존 Markdown 위키와 `wiki_context.py`를 [Obsidian 연결 안내](docs/obsidian.md)에
따라 연결합니다. 설정은 프로젝트별이며 기본 비활성입니다. 필요한 읽기·privacy·쓰기
범위만 허용합니다. `wiki context`, `project`, `read`는 분량을 제한하고 현재 revision을
검사한 출처 자료를 반환합니다. 현재 사용자 요청과 작업 계약이 우선합니다.

결정, 명시적인 긍정·부정 평가, 재현 가능한 성공을 선별해 검토 후보로 남깁니다.
템플릿과 Bases로 검토·절차를 정리하고, 성공한 코드·문서는 hash가 있는 참조로 저장합니다.
절차 채택은 controller 검증을 요구합니다. 용량 집계와 압축·복원은 활성 작업을 보호합니다.
전체 토큰, 검색 속도, 사용자 만족도는 따로 측정하며 작은 진단 fixture만으로 개선을 주장하지 않습니다.

## 기본 내장 스킬과 MCP

일반 `upgrade`는 라이선스가 포함된 **실제 공개 스킬 50개**(OpenAI 30개, [K-Dense Scientific Agent Skills](https://github.com/K-Dense-AI/scientific-agent-skills) 20개)의 `SKILL.md`, 필요한 참조·스크립트·자산을 함께 설치합니다. [스킬 manifest](config/builtin-skills.json)에 각 파일의 출처·고정 커밋·해시가 있고, [제3자 고지](THIRD_PARTY_NOTICES.md)에 라이선스 범위가 정리되어 있습니다. 이름이 같은 기존 스킬 폴더는 그대로 두고, 번들 사본은 관리 릴리스에 보존합니다. 스킬별 Python/Node 패키지와 외부 서비스는 자동 설치하지 않습니다.

[MCP 카탈로그](config/mcp-selection.json)의 16개 연결 정의도 기본 등록됩니다. 같은 이름이나 URL의 기존 연결은 유지하고 `config.toml`에는 없는 항목만 추가합니다. 기존 모델·제공자·플러그인·인증 파일은 보존합니다.

| 준비 상태 | MCP |
| --- | --- |
| 익명 원격 연결: 기본 활성화 | `firecrawl`, `exa`, `openai-docs` |
| 로컬 실행기·런타임·엔진 확인 후 활성화 | `aside`, `chrome-devtools`, `arxiv`, `godot`, `drawio` |
| 인증 필요: 기본 비활성화 | `tavily`, `brave-search`, `jupyter`, `figma`, `sentry`, `notion` |
| 별도 엔진 통합 확인 필요 | `blender`, `unity` |

Aside는 별도 설치하는 [공식 CLI](https://docs.aside.com/help/developers)의 `aside mcp` 명령을 사용합니다. 고정 버전 `npx`·`uvx` 서버 패키지는 ADHD에 실행 파일로 포함되지 않으며, Codex가 해당 연결을 시작할 때 내려받을 수 있습니다. 등록·실행기 확인만으로 인증이나 실제 MCP 연결 성공이 증명되지는 않습니다.

기존 ADHD 후크와 역할을 유지하고 번들만 적용할 때는 다음 명령을 사용하세요. `KEY`에는 위 표의 MCP 이름을 넣습니다. `builtin enable`은 관리 항목의 현재 선행 조건이 충족된 경우에만 켭니다. `builtin rollback`은 이 독립 적용분을 되돌립니다. 변경된 도구 목록은 새 Codex 앱/CLI 세션에서 확인하세요.

```powershell
py -3 .\adhd.py builtin apply
py -3 .\adhd.py builtin status
py -3 .\adhd.py builtin enable KEY
py -3 .\adhd.py builtin rollback
```

`builtin status`는 `registered → dependencies_ready → auth_integration_verified →
connected → read_verified` 단계를 각각 표시합니다. `usable`은 검토한 읽기 도구의
실제 호출이 성공한 경우에만 참이 됩니다. OAuth와 엔진 통합도 기존 extension의
읽기 검증을 재사용하는 probe를 통해 확인할 수 있습니다.

```powershell
py -3 .\adhd.py builtin probe KEY --consent --probe-file read-probe.json
py -3 .\adhd.py builtin probe KEY --consent --probe-file read-probe.json --oauth-token-env MCP_ACCESS_TOKEN
py -3 .\adhd.py builtin enable KEY
```

probe 파일은 `{"tool":"get_status","arguments":{},"read_only":true,
"purpose":"최소 읽기 호출로 연결 확인"}` 형식입니다. 해당 서버가 제공하는 실제 읽기
도구 이름을 사용하세요. handshake만 성공하면 사용 가능으로 처리하지 않습니다.
기존 선택 의존성인 공식 Python MCP SDK가 있어야 하며, 자동 설치하지 않습니다.
OAuth 옵션에는 이미 승인받은 access token이 있는 환경 변수의 이름만 전달합니다.
ADHD는 OAuth 승인을 대신 진행하거나 토큰을 저장하지 않습니다. 활성화할 때는
Codex가 같은 인증을 사용할 수 있도록 환경 변수의 이름만 설정합니다.
검증은 한 시간 후 만료되며 연결 정의·런타임·인증이 바뀌거나 재검증이 실패하면
이전 성공 근거를 사용할 수 없습니다. `--consent`는 서버 실행(실행기의 다운로드 포함)과
지정한 읽기 호출에 대한 동의입니다. 서버의 읽기 전용 표시는 참고 정보입니다.

## 실행 강도와 진전 판단

`native begin --profile NAME` 또는 begin JSON의 `execution_profile`로 선택합니다.

| 프로필 | 계획·위임 | 완료 검증 |
| --- | --- | --- |
| `simple` | 직접 처리 또는 제한된 자식 하나, 계획 선택 | 독립 검토 |
| `standard`(기본) | 요구사항을 포함한 짧은 계획과 제한된 위임 | 자동 검사와 독립 검토 |
| `deep` | 깊은 계획과 제한된 위임 | 모든 대상에 연결된 최신 실행 근거와 독립 검토 |

작은 설명은 지속 실행을 시작하지 않고 직접 답할 수 있습니다. 프로필은 모델이나
effort를 자동으로 바꾸지 않으며 명시적인 로컬 설정을 존중합니다.
`adhd.py eval --out .adhd/evaluation.json --profile deep`은 선택한 강도와 fixture
검사 결과를 기록합니다. 실제 작업의 품질·시간·비용상 우열은 별도 평가가 필요합니다.

checkpoint의 `criterion_results`와 `completed_steps`에는 최신 실행 기록을 연결합니다.
새 요구사항 검증 통과, 실패 테스트 감소, 알려진 계획 단계 완료, 후보 제출 전 실제
대상 내용 변경을 진전으로 기록합니다. 시간값만 바뀐 출력과 오래된 증거는 정체를
해소하지 않습니다. 같은 실패의 반복 횟수는 따로 기록하고 시간·라운드 상한을 유지합니다.
중단·취소·예산 소진 때도 마지막 자식이 끝날 때까지 작업 소유권을 유지합니다.

## 구성과 확인 범위

대규모 작업에는 [격리 병렬 작업·일괄 검사 실행 안내](docs/large-tasks.md)를
사용합니다. `large` CLI가 실제 의존성 큐와 작업 공간·CLI 프로세스를 연결하고,
`batch` CLI는 일반 작업에서도 제작 후 검사를 한 번에 모아 실행합니다.
기존 App 단일 작성자 경로와 설정은 유지합니다.

- `adhd.py`, `hook.py`, `adhd/`: CLI, 후크, 컨트롤러와 설치기
- `skills/adhd-*`, `native/agents/`: 하네스 지침과 보조 역할 기본값
- `bundled/skills/`, `config/builtin-skills.json`, `config/mcp-selection.json`: 공개 스킬 50개 전체 파일·라이선스·정확한 출처와 MCP 연결 정의 16개
- `schemas/`, `config/`, `examples/`: 계약 형식과 일반 예시
- `tests/`, `scripts/`: 회귀 검사와 읽기 전용 설치 감사
- `third_party/`, `research/REUSE_MANIFEST.json`: 포함된 구성 요소와 라이선스 출처

[설정 설명](config/README.md)을 참고하세요. Sol·Luna 보조 역할의 기본 추론 수준은 `max`, Astra는 `low`입니다. 역할 파일에 모델이 적혀 있어도 계정에서 실제 호출 가능한지는 별도로 확인해야 합니다. 설치된 위키 라우터가 있다면 자신의 `skill_wiki.py` 실제 경로를 `adhd.py route --configure-wiki-router PATH`에 전달하세요. 문서 도구도 설치 여부와 실제 실행 가능 여부를 구분해 확인해야 합니다.

```powershell
py -3 -m unittest discover -s tests -q
py -3 -m unittest scripts.test_audit_local_install -q
py -3 .\adhd.py --help
```

테스트 통과만으로 계정의 모델 접근, 후크 신뢰, Office·한컴 화면 렌더링 또는 외부 MCP 연결이 증명되지는 않습니다. 설치 후 새 Codex 세션에서 작은 실제 작업을 실행해 확인하세요.

CI는 Windows·Ubuntu와 Python 3.11·3.13에서 실행합니다. 배포 smoke 검사는 실제 ZIP의
checksum을 검증한 뒤 새로운 오프라인 가상 환경에 풀고, CLI 설치와 `doctor`,
설치된 `hooks.json`의 실제 명령을 실행합니다. 합성 후크 이벤트로 설치 파일을 검증하며,
계정 접근이나 후크 신뢰 승인을 대신하지 않습니다.

## 코드 수정 규칙

코드 수정에는 [카파시 원칙을 적용한 짧은 지침](skills/adhd-native/references/coding-discipline.md)을
계획자·구현자·검토자가 함께 읽습니다. 중요한 가정 확인, 기존 코드 재사용, 필요한 부분만 수정,
충분한 검증 뒤 종료를 다룹니다. Git 작업은 시작 전에 CLI로 허용 경로와 기존 수정·스테이징 상태를
기록하고, 후보 제출 때 실제 범위 검사 기록과 변경 파일별 요구사항 연결을 제출해야 합니다.
후크는 명령을 실행하지 않고 파일 해시·목록·기록된 Git 메타데이터를 재확인합니다.
[실행 절차](skills/adhd-native/references/protocol.md#coding-scope)를 참고하세요.
이 검사는 과잉 설계의 의미 판단이나 도구 실행 권한을 대신하지 않습니다.

기존 실행의 계약은 유지됩니다. 이 소스를 설치한 뒤 새 Codex 세션에서 시작하는 코드 작업부터
새 범위 검사가 적용됩니다.

## 개인정보와 라이선스

개인 `config.toml`, `auth.json`, `hooks.json`, `.adhd/`, 생성한 증거 파일과 사적인 프로젝트 기록은 Git에 추가하지 마세요. `.gitignore`가 일반적인 로컬 파일을 제외하지만, 공개 전 `git diff --cached`를 직접 확인하는 것이 안전합니다.

저장소의 `LICENSE`는 Apache-2.0입니다. 기존 Raibit 코드와 문서의 MIT 고지는 `LICENSE-RAIBIT-MIT`에 보존했습니다. 재사용한 구성 요소의 고지는 `THIRD_PARTY_NOTICES.md`와 `third_party/`에 있습니다.
