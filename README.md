# GitHub Helper

공개 GitHub 저장소를 분석해 주니어 개발자와 바이브코더를 위한 읽기 가이드를 생성합니다.
에이전트는 저장소 코드를 실행하지 않으며, HTML은 검증된 구조화 결과와 템플릿으로 렌더링합니다.

## 로컬 개발

### 준비

- Python 3.11+
- Node.js 20+
- Docker (MongoDB를 실행하는 경우)
- 보고서 생성을 위한 OpenAI API 키

```bash
cp .env.example .env
# .env에 OPENAI_API_KEY를 입력
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 실행

```bash
docker compose up -d mongo
uvicorn app.main:app --reload     # API
python -m app.worker              # 워커 (별도 터미널)
```

또는 `docker compose up --build`로 mongo, api, worker를 한 번에 실행합니다.

### 잡 API

```bash
curl -X POST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"url":"https://github.com/pallets/click","level":"junior"}'  # -> {"id": "..."}
curl localhost:8000/jobs/<id>          # 상태와 진행 이벤트
curl localhost:8000/jobs/<id>/report   # 완료된 HTML 리포트
curl localhost:8000/jobs/<id>/events   # SSE 진행 스트림
curl -X POST localhost:8000/jobs/<id>/questions -H 'content-type: application/json' \
  -d '{"question":"옵션 파싱은 어디서 해요?"}'      # 근거 라인과 함께 답변
curl localhost:8000/jobs/<id>/questions  # 질문 기록
curl localhost:8000/reports/recent       # 최근 분석된 저장소
```

- `level`: `beginner`(입문자/바이브코더, 기본값) · `junior` · `senior`
- URL은 `https://github.com/owner/repo/tree/<branch>` 형태도 받습니다.
- 같은 `repo + commit_sha + level`은 캐시된 리포트를, 같은 리포트에 같은 질문은 캐시된 답변을 재사용합니다.
- 진행 중인 동일 요청은 새 잡을 만들지 않고 기존 잡 id를 돌려줍니다.
- 워커가 죽어 15분 넘게 멈춘 잡은 다른 워커가 이어받습니다(최대 3회).
- `MONGODB_DB`로 데이터베이스 이름을 바꿀 수 있습니다(기본 `github_helper`).

API 상태 확인: `http://localhost:8000/health`

프론트엔드 화면 — `http://localhost:5180` (포트 고정, Vite가 `/api/*`를 `localhost:8000`으로 프록시합니다):

```bash
cd frontend
npm install
npm run dev
```

### CLI 리포트 생성

```bash
python -m app.agent.cli https://github.com/owner/repository
python -m app.agent.cli https://github.com/owner/repository --branch main --output reports/guide.html
```

GitHub API 익명 호출 한도에 걸리는 경우 `.env`에 `GITHUB_TOKEN`을 지정하세요. 토큰은 공개 저장소 읽기 요청에만 사용합니다.

### 테스트와 코드 스타일

코드는 Google 스타일 가이드([Python](https://google.github.io/styleguide/pyguide.html), [TypeScript](https://google.github.io/styleguide/tsguide.html))를 따릅니다.

```bash
pytest
ruff format --check . && ruff check .      # 80칸, Google docstring, import 규칙
cd frontend && npm run lint                # tsc strict + prettier(gts 설정)
```

## 현재 구현 범위

- GitHub 저장소 트리·파일 읽기·코드 검색·스택 탐지 도구
- LangGraph 에이전트: 파일 선택 → 읽기 → 추가 탐색 → 구조화 리포트
- 리포트: 요약, 바로 실행해 보기, 읽기 순서, 설명 섹션, 용어 사전
- 근거 검증: 인용 라인은 실제로 읽은 줄만, 읽기 순서는 트리에 있는 파일만, 실행 명령은 매니페스트나 파일에 그대로 있는 것만 남김
- 인용은 분석한 커밋의 GitHub 라인 링크로 연결
- 독자 수준(입문자/주니어/숙련자)별 리포트
- 레포 Q&A: 툴콜링 에이전트가 같은 커밋의 코드를 검색·읽기하고 근거 라인과 함께 답변
  - 근거마다 "이 줄이 보여주는 사실"을 붙이고, 검사 모델이 뒷받침하는 근거만 남긴 뒤 답변을 근거 범위로 다시 씀 (`OPENAI_QA_CHECK_MODEL`로 검사 모델 지정)
  - 아키텍처·워크플로우 질문에는 그림을 그림: LangGraph 그래프와 docker-compose 서비스는 코드에서 정확히 추출, 그 밖의 구조는 모델이 근거와 함께 그리고 확인 안 된 부분은 점선
- 비동기 잡 + 워커 + SSE 진행 상태, 최근 분석 목록

개선 내역과 검증 결과는 [docs/IMPROVEMENT_REPORT.md](docs/IMPROVEMENT_REPORT.md)에 있습니다.

## 추적 (Langfuse / LangSmith)

둘 다 선택 사항이며, 키를 `.env`에 넣으면 CLI 실행 시 자동으로 트레이스가 전송됩니다.

- Langfuse: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`
- LangSmith: `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`

구현은 [app/agent/tracing.py](app/agent/tracing.py)에 있습니다.
