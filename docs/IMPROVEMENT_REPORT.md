# 개선 및 검증 보고서

작성일: 2026-10-27 · 기준 커밋: `5eac4d4` (작업 내용은 아직 커밋하지 않음)

## 1. 요약

- 리포트를 "한 번 읽고 끝"에서 **"읽고 → 따라 하고 → 물어보는"** 흐름으로 바꿨습니다.
  - 새 기능: 레포 Q&A, 읽기 순서, 바로 실행해 보기, 용어 사전, GitHub 라인 링크, 독자 수준 선택, 최근 분석 목록
- 운영 안정성: 같은 요청 합치기, 죽은 워커의 잡 재처리, 커밋 고정 버그 수정
- 기존 코드 전체를 Google 스타일 가이드(Python / TypeScript)에 맞춰 정리했고, 린터로 강제합니다.
- 검증: 단위·통합 테스트 **29개 → 56개 모두 통과**. 실제 GitHub·OpenAI·MongoDB로 두 저장소를 돌린 E2E 검사 **40개 중 39개 통과**(미통과 1개는 의도된 동작, 4.2 참고). 브라우저 UI도 확인.

## 2. 어떤 기능을 왜 골랐나

대상 사용자(주니어, 바이브코더)가 낯선 레포에서 실제로 막히는 지점을 기준으로 골랐습니다.

| 사용자가 막히는 지점 | 추가한 기능 | 우선순위 근거 |
|---|---|---|
| 리포트를 읽고 나서 "그럼 X는 어디 있어?"가 생김 | **레포 Q&A** (근거 라인 포함) | 다시 찾아오게 만드는 기능. 요구사항 7단계 |
| 어떤 파일부터 열어야 할지 모름 | **읽기 순서** (트리에 있는 파일만) | 입문자에게 가장 직접적인 도움 |
| 일단 돌려보고 싶음 | **바로 실행해 보기** (검증된 명령만) | 바이브코더의 첫 행동 |
| 인용된 `path:10-20`을 확인하기 번거로움 | **GitHub 라인 링크** (분석 커밋 고정) | 신뢰도 상승, 구현 비용 작음 |
| 설명이 너무 쉽거나 어려움 | **독자 수준 3단계** | 요구사항 7단계, 캐시 키에 포함 |
| 모르는 용어 | **용어 사전** | 입문자 이탈 방지 |
| 뭘 넣어볼지 모름 | **최근 분석된 저장소** (누르면 캐시로 즉시 열림) | 첫 방문자 체험, 비용 0 |
| 브라우저 주소창의 `/tree/main` URL을 붙여넣으면 거부됨 | **`/tree/<branch>` URL 지원** | 실제 사용 패턴 |

시도하지 않은 것: mermaid 다이어그램(리포트 iframe이 스크립트를 막아 렌더링 불가), private 레포·로그인(MVP 제외 범위).

## 3. 변경 내역

### 3.1 새 기능

**레포 Q&A** — [app/agent/qa.py](../app/agent/qa.py), `POST/GET /jobs/{id}/questions`
- 리포트를 만든 **같은 커밋**에서 질문에 필요한 파일을 고르고 → 코드 검색 → 읽은 내용만으로 답변
- 인용은 실제로 읽은 줄만 남김. 근거가 하나도 안 남으면 `grounded=false`로 표시하고 UI에 "근거를 확인하지 못한 답변" 경고
- 같은 리포트의 같은 질문(대소문자·공백 무시)은 캐시 답변 재사용. 질문 기록은 잡 단위로 분리(다른 사용자 질문이 섞이지 않음)
- 질문 500자 제한, 동시 처리 4개, 120초 타임아웃. 에러는 HTTP 상태로 구분(GitHub 502, 키 없음 503, 시간 초과 504)

**리포트 확장** — [app/agent/report.py](../app/agent/report.py), [templates/report.html](../app/agent/templates/report.html)
- 스키마에 `reading_order`, `quick_start`, `glossary` 추가(기본값이 있어 예전 리포트도 그대로 읽힘)
- `verify_report()`로 모두 검증:
  - 읽기 순서: 트리에 있는 경로만, 중복 제거, 최대 8개
  - 실행 명령: 매니페스트에서 감지했거나 읽은 파일에 **그대로 적힌** 명령만 (지어낸 명령 차단)
  - 용어: 최대 12개
- 인용과 읽기 순서를 `github.com/.../blob/<commit>/path#L1-L5` 링크로 렌더링. iframe은 스크립트를 계속 막고 링크용 팝업만 허용

**독자 수준** — [app/agent/audience.py](../app/agent/audience.py)
- `beginner` / `junior` / `senior`별 프롬프트. 리포트 캐시 키가 `repo + commit + level`이 됨
- 기존 `(repo, commit_sha)` 유니크 인덱스는 시작 시 자동으로 삭제하고 새 인덱스로 교체

**프론트엔드** — 수준 선택, 최근 분석 목록, Q&A 패널(추천 질문 버튼 포함), 진행 화면에 레포 이름 표시

### 3.2 안정성·버그 수정

| 문제 | 수정 |
|---|---|
| 워커가 커밋을 고정한 뒤 에이전트가 브랜치를 **다시** 조회 → 그 사이 커밋이 바뀌면 리포트 내용과 캐시 키가 어긋남 | 워커가 고정한 커밋을 에이전트에 넘김 |
| 워커가 죽으면 잡이 영원히 `running` | 15분간 진행 이벤트가 없으면 다른 워커가 이어받음. 3회 넘으면 실패 처리 |
| 같은 요청을 여러 번 누르면 잡이 중복 생성 | 진행 중인 동일 요청(레포·브랜치·수준)은 기존 잡 id 반환 |
| 추가 탐색 중 검색 결과 파일을 못 읽으면 잡 전체 실패 | 해당 파일만 건너뜀 |
| `search_code`가 파일을 하나씩 순서대로 다운로드 | 최대 8개 동시 다운로드, 결과 순서는 그대로 |
| GitHub 429 응답을 일반 오류로 표시 | 403과 함께 "rate limit" 안내로 표시 |
| 리포트 본문의 줄바꿈이 사라짐 | `white-space: pre-line` |
| 파일 선택이 tests/examples로 새는 문제(이전 메모의 알려진 문제) | 프롬프트에 "실제 소스와 설정 파일 우선" 추가 |

### 3.3 Google 스타일 정리

**Python** ([Google Python Style Guide](https://google.github.io/styleguide/pyguide.html))
- 모든 모듈·공개 함수·클래스에 Google 형식 docstring(Args / Returns / Raises)
- 한 줄 80칸, 4칸 들여쓰기
- import는 모듈 단위만(`from app.agent import github` → `github.Repository`). 예외는 가이드가 허용하는 `typing`, `collections.abc`뿐
- import 한 줄에 하나, 섹션별 정렬
- 프롬프트를 [app/agent/prompts.py](../app/agent/prompts.py)로 분리(검토와 비교가 쉬움)
- 중복 코드 정리: GitHub 오류 처리 통합, 매니페스트 파싱을 `_collect_stack()`으로 분리, 예산 관리 함수를 `budget.py`로 이동
- `pyproject.toml`의 ruff 설정으로 강제: `D`(google docstring), `N`(이름 규칙), `I`(force-single-line), `E501`(80칸)

**TypeScript** ([Google TypeScript Style Guide](https://google.github.io/styleguide/tsguide.html))
- default export 제거 → named export만 사용
- 객체 타입은 `interface`, 공개 심볼에 JSDoc, 파일마다 `@fileoverview`
- 파일 이름 snake_case (`App.tsx` → `app.tsx` 등, `git mv`로 이력 유지)
- non-null 단언(`!`) 제거, `any` 없음
- Prettier를 Google `gts` 설정(작은따옴표, `{a}` 공백 없음, es5 trailing comma)으로 적용, `npm run format` / `npm run lint` 추가
- tsconfig에 `noUnusedLocals`, `noUnusedParameters`, `noImplicitReturns` 등 추가

## 4. 검증

### 4.1 무엇을 검증했나

| # | 검증 항목 | 방법 | 결과 |
|---|---|---|---|
| 1 | 기존 기능이 깨지지 않았는가 | 기존 테스트를 새 구조에 맞춰 이전 후 실행 | ✅ 통과 |
| 2 | 새 기능의 로직 | 테스트 27개 추가 (아래) | ✅ 56/56 통과 (1.3초) |
| 3 | Python 스타일 | `ruff format --check`, `ruff check` (google 규칙) | ✅ 위반 0 |
| 4 | Python 스타일 (Google 공식 도구) | Google `styleguide` 저장소의 공식 `pylintrc`로 pylint 3.3.9 실행. 먼저 일부러 틀린 파일로 실제로 잡아내는지 확인 | ✅ `app/` 위반 0, `tests/` 위반 0 (docstring 규칙 제외) |
| 5 | import 규칙 | 모든 `from X import Y`가 모듈인지 스크립트로 검사 | ✅ `typing`·`collections.abc` 예외만 남음 |
| 6 | TypeScript | `tsc -b` (strict + 추가 규칙), `prettier --check` | ✅ 통과 |
| 7 | 프론트 빌드 | `npm run build` | ✅ 155 KB (gzip 50 KB) |
| 8 | 배포 패키지 | wheel 빌드 후 새 모듈·템플릿 포함 확인 (Dockerfile이 `pip install .` 사용) | ✅ 포함됨 |
| 9 | 실제 서비스 E2E | 아래 4.2 | ✅ 39/40 |
| 10 | 브라우저 UI | 아래 4.3 | ✅ (iframe은 일부 확인 불가) |
| 11 | 운영 DB 인덱스 마이그레이션 | 실행 중이던 API(`--reload`)가 새 코드를 불러와 기존 DB에 적용됨 | ✅ 기존 `repo_1_commit_sha_1` 삭제, 새 인덱스 생성 확인 |

**새로 추가한 테스트 (27개)**
- URL: `/tree/main`, `/tree/feature/x`, 명시한 브랜치 우선, `/tree`만 있는 URL 거부
- GitHub: 커밋 고정 시 브랜치 재조회 안 함, 동시 검색이 순서 유지 + 못 읽는 파일 건너뜀, 429 안내, 하위 폴더 매니페스트 무시, 라인 링크 생성
- 리포트: 지어낸 읽기 순서·명령 제거, 용어 개수 제한, 예전 리포트 호환, 새 섹션의 XSS 이스케이프, 커밋 링크, 인용 중복 제거
- 에이전트: 수준별 프롬프트 반영, 확장 필드 검증 후 링크 렌더링
- Q&A: 읽은 줄만 인용, 근거 없으면 `grounded=false`, 검색 결과를 문맥에 포함, 계획이 비면 상위 파일 읽기, 빈/긴 질문 거부
- 잡: 멈춘 잡 재처리, 3회 초과 시 실패, 워커가 커밋·수준 전달, 수준별 캐시 분리, 예전 인덱스 삭제, 중복 요청 합치기, 최근 목록, 질문 캐시·기록·HTTP 에러 매핑

### 4.2 실제 서비스 E2E

사용자가 띄워 둔 서버와 섞이지 않도록 **별도 DB(`github_helper_verify`)** 에 API(8001)·워커·Vite(5174)를 따로 띄워 실행했습니다. 실제 GitHub API, OpenAI(`gpt-4o-mini`), MongoDB 사용.

| 검사 | pallets/click (`/tree/main`, 주니어) | sindresorhus/slugify (입문자) |
|---|---|---|
| 리포트 생성 | ✅ 38초 | ✅ 17초 |
| 동일 요청 중복 방지 | ✅ 같은 id 반환 | ✅ |
| `/tree/main` → branch=main | ✅ | – |
| SSE 단계 (queued → … → done) | ✅ 10단계 | ✅ 10단계 |
| 섹션 / 읽기 순서 / 용어 사전 | ✅ 3 / 8개 / 있음 | ✅ 3 / 4개 / 있음 |
| 바로 실행해 보기 | ⚠️ 없음 (아래 설명) | ✅ `npm install @sindresorhus/slugify`, `npm run test` |
| 링크가 모두 분석 커밋에 고정 | ✅ 17개 | ✅ 11개 |
| **링크된 파일·라인이 GitHub에 실제로 존재** (raw 파일 줄 수와 대조) | ✅ 17/17 | ✅ 11/11 |
| 질문 2개 답변 | ✅ 10초, 13초 | ✅ 6초, 10초 |
| 답변 근거 있음 + 링크 유효 | ✅ 2/2 | ✅ 2/2 |
| 같은 질문 재요청 캐시 | ✅ 0.02초 | ✅ 0.01초 |

그 외: 같은 레포·수준 재요청 시 캐시 재사용(2초) ✅, github.com 아닌 URL 422 ✅, 없는 레포는 "not found" 메시지로 실패 ✅, 최근 분석 목록 ✅, 워커 로그 에러 0건.

**⚠️ click의 "바로 실행해 보기"가 비어 있던 이유**: click은 라이브러리라 실행 스크립트가 없고, 모델이 제안한 명령이 읽은 파일에 그대로 적혀 있지 않아 검증 단계에서 모두 제거됐습니다. 지어낸 명령을 보여주지 않는 의도된 동작이며, 섹션은 자동으로 숨겨집니다. 개선 여지는 5장 참고.

**답변 예시** (click, "명령줄 옵션 파싱은 어디서 처리해요?")
> `src/click/parser.py`의 `_OptionParser` 클래스에서 처리됩니다. `parse_args` 메서드가 `_ParsingState`를 만들고 … — 근거: `parser.py:224`, `:298`, `:316`, `:327` (모두 링크 유효)

### 4.3 브라우저 UI

- ✅ 첫 화면: 수준 선택(3개), 최근 분석된 저장소 카드 표시
- ✅ 최근 카드 클릭 → 캐시된 리포트로 즉시 완료, 레포 이름 표시
- ✅ 리포트 페이지 직접 열기: 실행 명령, 읽기 순서(링크), 섹션별 근거 링크, 용어 사전 렌더링
- ✅ Q&A: 추천 질문 클릭 → "코드를 찾아보는 중…" → 답변과 근거 링크 표시
- ✅ 모바일(375px): 가로 스크롤 없음, 수준 버튼 세로 배치
- ✅ 콘솔 에러 없음
- ⚠️ **확인 못 한 것**: 앱 안의 리포트 iframe. 검증에 쓴 내장 브라우저가 iframe 로드를 자체적으로 막았습니다(`ERR_BLOCKED_BY_CLIENT`). 서버 응답(200, 5.8 KB)과 리포트 렌더링은 확인했지만, 일반 Chrome에서 iframe 안의 링크가 새 탭으로 열리는지는 직접 한 번 확인이 필요합니다.

## 5. 남은 일 / 다음 개선 후보

1. **실행 중인 워커 재시작 필요** — 기존 워커(12:36 시작)는 예전 코드로 돌고 있습니다. 재시작하지 않으면 새 잡이 수준 구분 없이 예전 방식으로 처리됩니다. API는 `--reload`로 이미 새 코드입니다.
2. 라이브러리형 레포의 실행 명령: README의 `pip install` 코드 블록을 감지해 검증된 명령 후보로 추가
3. 큰 파일 인용 품질: 모델이 `readme.md:1-315`처럼 파일 전체를 인용하는 경우가 있어, 인용 범위 상한(예: 40줄)을 검증 단계에 추가
4. 평가용 레포 3~5개 비교(요구사항 8단계) — `e2e` 스크립트를 저장소에 넣어 회귀 검사로 쓰는 방법 추천
5. Q&A를 잡 큐로 옮기면 동시 사용자가 많아도 API 프로세스가 버팀 (지금은 API에서 바로 실행, 동시 4개 제한)

## 6. 검증 재현 방법

```bash
pytest
ruff format --check . && ruff check .
cd frontend && npm run lint && npm run build
```

격리된 E2E 환경은 `.claude/launch.json`의 `verify-api`(8001, DB `github_helper_verify`)와 `verify-web`(5174) 설정, 그리고 `MONGODB_DB=github_helper_verify python -m app.worker`로 띄울 수 있습니다.
