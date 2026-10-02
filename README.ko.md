# ADHD — Autonomous Delegation Harness Director

ADHD v0.1.3은 Codex 앱과 CLI에서 실행하는 로컬 하네스입니다. Codex 후크를 통해 계획, 실행 근거, 검토 단계를 연결합니다. 기존 인증, 선택 모델, 라우터, 플러그인, 스킬, 개인 설정을 설치기가 덮어쓰지 않도록 설계했습니다.

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

## 구성과 확인 범위

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
