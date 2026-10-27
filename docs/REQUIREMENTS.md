# GitHub Helper — 요구사항

GitHub 퍼블릭 레포 URL을 입력하면, 에이전트가 레포를 탐색해서 코드에 익숙하지 않은 사람(주니어, 바이브코더)이 읽기 쉬운 HTML 리포트를 만들어 주는 서비스.

## 1. 범위

**MVP 포함**
- 퍼블릭 레포 (URL + 선택적 브랜치)
- 에이전트 탐색 → 구조화 JSON → HTML 리포트
- 모든 설명에 근거 파일·라인 표기, 존재하지 않는 경로는 검증 단계에서 제거
- 비동기 잡 처리 + 진행 상태 표시

**MVP 제외**
- private 레포, 로그인/계정
- 코드 실행 (레포는 읽기만 한다)
- 벡터 검색 / Q&A 챗 (심화 단계)

## 2. 아키텍처

```
React ──> FastAPI (jobs API, SSE 진행상태)
              │ job 생성
              ▼
          MongoDB (jobs, reports)
              ▲ 폴링 + findOneAndUpdate로 원자적 선점
              │
           Worker 프로세스 ── import ──> agent 패키지 (LangGraph)
                                           └─ tools: GitHub, OpenAI
```

- 에이전트는 별도 서비스가 아니라 `agent/` 모듈. 워커가 함수로 직접 호출한다.
- API와 워커는 같은 코드베이스, 실행 명령만 다르다.
- job 상태: `queued → running → done | failed`, 진행 이벤트는 job 문서에 기록.
- 같은 `repo + commit_sha` 결과는 캐시한다.
- 확장 경로: Redis + ARQ로 큐 교체 가능.

## 3. 에이전트 도구

| 도구 | 역할 |
|---|---|
| `get_repo_tree` | 파일 트리 (node_modules, lock, 바이너리 제외, 깊이·개수 제한) |
| `read_file` | 파일 내용 (라인 범위, 크기 제한) |
| `search_code` | 키워드/정규식 검색 |
| `detect_stack` | package.json, pyproject, Dockerfile 등에서 스택·실행 명령 추출 |
| `render_report` | 구조화 결과 → HTML (Jinja2) |
| `verify_citations` | 인용한 경로·라인 존재 여부 검증 |

원칙
- LLM은 HTML을 직접 쓰지 않는다. Pydantic 스키마의 JSON만 만들고, 템플릿이 HTML로 렌더링한다.
- 레포 안의 텍스트(README, 주석 등)는 데이터이며 지시문이 아니다.
- 탐색은 트리 → 핵심 파일 → 필요한 곳만 심화 순서로, 토큰 예산 안에서 진행한다.

## 4. 기술 스택

- 백엔드: Python, FastAPI, LangGraph + langchain-openai, Pydantic, Motor, httpx, Jinja2, tiktoken
- DB: MongoDB
- LLM: OpenAI API
- 프론트: Vite + React (JS, 가능하면 TS), Tailwind, mermaid
- 운영/품질: Docker Compose, pytest, Langfuse 또는 LangSmith

## 5. 구현 순서

1. 뼈대: docker-compose(mongo), FastAPI health, React 빈 페이지
2. 도구만 (LLM 없이): tree / read_file / search_code + 테스트
3. 에이전트 1개: LangGraph로 트리 → 파일 선택 → 읽기 → 구조화 JSON (CLI로 먼저 확인)
4. HTML 렌더링 + 인용 검증
5. 잡 + 워커: `POST /jobs`, 선점·처리, `GET /jobs/:id`
6. UI: URL 입력 → 진행 상태(SSE) → 리포트 보기
7. 심화: 사용자 수준별 프롬프트, 레포 Q&A 챗, commit_sha 캐시
8. 마무리: README, 추적 스크린샷, 평가용 레포 3~5개 비교

첫날 목표: 1~4번 (CLI 기준). 5~6번은 둘째 날.
