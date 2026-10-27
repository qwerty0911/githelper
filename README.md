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
uvicorn app.main:app --reload
```

API 상태 확인: `http://localhost:8000/health`

프론트엔드 화면:

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

### 테스트

```bash
pytest
```

## 현재 구현 범위

- FastAPI health check 및 MongoDB 개발 환경 구성
- GitHub 저장소 트리·파일 읽기·코드 검색·스택 탐지 도구
- LangGraph 기반 파일 선택 및 Pydantic 구조화 가이드 생성
- 인용 경로/라인 검증 및 Jinja2 HTML 렌더링
- 프론트엔드 초기 화면

비동기 작업 API, 워커, SSE 진행 상태, 프론트엔드에서 실제로 리포트를 요청하는 흐름은 다음 단계에서 구현합니다.
# githelper
