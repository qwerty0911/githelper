export default function App() {
  return (
    <main>
      <div className="eyebrow">GITHUB HELPER</div>
      <h1>낯선 프로젝트를<br />쉽게 읽어보세요</h1>
      <p>
        공개 GitHub 저장소를 살펴보고, 처음 보는 개발자도 이해할 수 있는
        프로젝트 가이드를 만듭니다.
      </p>
      <form>
        <label htmlFor="repository">GitHub 저장소 주소</label>
        <div className="input-row">
          <input
            id="repository"
            type="url"
            placeholder="https://github.com/owner/repository"
            disabled
          />
          <button type="button" disabled>준비 중</button>
        </div>
        <small>현재는 에이전트 CLI와 리포트 생성 기능을 먼저 준비하고 있어요.</small>
      </form>
    </main>
  );
}
