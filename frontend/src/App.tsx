import { FormEvent, useEffect, useRef, useState } from "react";
import { createJob, eventsUrl, getJob, type Job, type JobEvent } from "./api";
import ProgressList from "./components/ProgressList";
import ReportView from "./components/ReportView";

type Phase = "idle" | "running" | "done" | "failed";

const jobIdFromUrl = () => new URLSearchParams(window.location.search).get("job");

export default function App() {
  const [url, setUrl] = useState("");
  const [branch, setBranch] = useState("");
  const [jobId, setJobId] = useState<string | null>(jobIdFromUrl());
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [phase, setPhase] = useState<Phase>(jobId ? "running" : "idle");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const source = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    getJob(jobId)
      .then((job: Job) => {
        if (cancelled) return;
        setEvents(job.events);
        if (job.status === "done" || job.status === "failed") {
          setPhase(job.status);
          setError(job.error);
          return;
        }
        setPhase("running");
        const es = new EventSource(eventsUrl(jobId));
        source.current = es;
        // The server replays every event after a (re)connect, so start from empty.
        es.onopen = () => setEvents([]);
        es.addEventListener("progress", (e) =>
          setEvents((prev) => [...prev, JSON.parse((e as MessageEvent).data)]),
        );
        es.addEventListener("end", (e) => {
          const end = JSON.parse((e as MessageEvent).data);
          setPhase(end.status);
          setError(end.error);
          es.close();
        });
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setPhase("failed");
        setError(err.message);
      });
    return () => {
      cancelled = true;
      source.current?.close();
    };
  }, [jobId]);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const id = await createJob(url.trim(), branch);
      window.history.replaceState(null, "", `?job=${id}`);
      setEvents([]);
      setPhase("running");
      setJobId(id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  function reset() {
    source.current?.close();
    window.history.replaceState(null, "", window.location.pathname);
    setJobId(null);
    setEvents([]);
    setPhase("idle");
    setError(null);
  }

  return (
    <main className="mx-auto w-full max-w-3xl px-5 py-14">
      <div className="text-xs font-bold tracking-[0.12em] text-indigo-500">GITHUB HELPER</div>
      <h1 className="mt-4 text-4xl font-bold leading-tight tracking-tight sm:text-5xl">
        낯선 프로젝트를
        <br />
        쉽게 읽어보세요
      </h1>
      <p className="mt-4 leading-relaxed text-slate-500">
        공개 GitHub 저장소를 살펴보고, 처음 보는 개발자도 이해할 수 있는 프로젝트 가이드를 만듭니다.
      </p>

      {phase === "idle" && (
        <form onSubmit={onSubmit} className="mt-8 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
          <label htmlFor="repository" className="mb-2 block text-sm font-semibold">
            GitHub 저장소 주소
          </label>
          <input
            id="repository"
            type="url"
            required
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://github.com/owner/repository"
            className="h-12 w-full rounded-lg border border-slate-300 px-3 outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
          />
          <label htmlFor="branch" className="mb-2 mt-4 block text-sm font-semibold">
            브랜치 <span className="font-normal text-slate-400">(선택, 비우면 기본 브랜치)</span>
          </label>
          <input
            id="branch"
            value={branch}
            onChange={(e) => setBranch(e.target.value)}
            placeholder="main"
            className="h-12 w-full rounded-lg border border-slate-300 px-3 outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
          />
          {error && <p className="mt-3 text-sm text-red-600">{error}</p>}
          <button
            type="submit"
            disabled={submitting || !url.trim()}
            className="mt-5 h-12 w-full rounded-lg bg-indigo-600 font-medium text-white transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? "요청 중…" : "가이드 만들기"}
          </button>
        </form>
      )}

      {phase !== "idle" && (
        <section className="mt-8 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
          <div className="flex items-center justify-between">
            <h2 className="font-semibold">
              {phase === "done" ? "리포트가 준비됐어요" : phase === "failed" ? "작업에 실패했어요" : "분석 중…"}
            </h2>
            <button onClick={reset} className="text-sm text-slate-500 hover:text-slate-800">
              새로 시작
            </button>
          </div>
          <ProgressList events={events} status={phase} />
          {phase === "failed" && error && (
            <p className="mt-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>
          )}
          {phase === "done" && jobId && <ReportView jobId={jobId} />}
        </section>
      )}
    </main>
  );
}
