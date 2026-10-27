/**
 * @fileoverview Main screen: request a guide, watch progress, read and ask.
 */

import {type FormEvent, useEffect, useRef, useState} from 'react';

import {
  createJob,
  eventsUrl,
  getJob,
  type JobEvent,
  type Level,
  type RecentReport,
} from './api';
import {LevelPicker} from './components/level_picker';
import {ProgressList} from './components/progress_list';
import {QuestionPanel} from './components/question_panel';
import {RecentReports} from './components/recent_reports';
import {ReportView} from './components/report_view';

type Phase = 'idle' | 'running' | 'done' | 'failed';

interface EndEvent {
  status: 'done' | 'failed';
  error: string | null;
}

const INPUT_CLASS =
  'h-12 w-full rounded-lg border border-slate-300 px-3 outline-none ' +
  'focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100';

function jobIdFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get('job');
}

function phaseTitle(phase: Phase): string {
  if (phase === 'done') return '리포트가 준비됐어요';
  if (phase === 'failed') return '작업에 실패했어요';
  return '분석 중…';
}

/** The single-page app. The job id lives in `?job=` so reloads resume. */
export function App() {
  const [url, setUrl] = useState('');
  const [branch, setBranch] = useState('');
  const [level, setLevel] = useState<Level>('beginner');
  const [jobId, setJobId] = useState<string | null>(jobIdFromUrl());
  const [repo, setRepo] = useState<string | null>(null);
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [phase, setPhase] = useState<Phase>(jobId ? 'running' : 'idle');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const source = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    getJob(jobId)
      .then(job => {
        if (cancelled) return;
        setRepo(job.repo);
        setEvents(job.events);
        if (job.status === 'done' || job.status === 'failed') {
          setPhase(job.status);
          setError(job.error);
          return;
        }
        setPhase('running');
        const stream = new EventSource(eventsUrl(jobId));
        source.current = stream;
        // The server replays every event after a (re)connect.
        stream.onopen = () => setEvents([]);
        stream.addEventListener('progress', event => {
          const data = JSON.parse((event as MessageEvent).data) as JobEvent;
          setEvents(previous => [...previous, data]);
        });
        stream.addEventListener('end', event => {
          const end = JSON.parse((event as MessageEvent).data) as EndEvent;
          setPhase(end.status);
          setError(end.error);
          stream.close();
        });
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setPhase('failed');
        setError(err.message);
      });
    return () => {
      cancelled = true;
      source.current?.close();
    };
  }, [jobId]);

  async function start(repoUrl: string, repoBranch: string, repoLevel: Level) {
    setSubmitting(true);
    setError(null);
    try {
      const id = await createJob(repoUrl.trim(), repoBranch, repoLevel);
      window.history.replaceState(null, '', `?job=${id}`);
      setEvents([]);
      setPhase('running');
      setJobId(id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void start(url, branch, level);
  }

  function onPickRecent(report: RecentReport) {
    void start(
      `https://github.com/${report.repo}`,
      report.branch ?? '',
      report.level
    );
  }

  function reset() {
    source.current?.close();
    window.history.replaceState(null, '', window.location.pathname);
    setJobId(null);
    setRepo(null);
    setEvents([]);
    setPhase('idle');
    setError(null);
  }

  return (
    <main className="mx-auto w-full max-w-3xl px-5 py-14">
      <div className="text-xs font-bold tracking-[0.12em] text-indigo-500">
        GITHUB HELPER
      </div>
      <h1 className="mt-4 text-4xl font-bold leading-tight tracking-tight sm:text-5xl">
        낯선 프로젝트를
        <br />
        쉽게 읽어보세요
      </h1>
      <p className="mt-4 leading-relaxed text-slate-500">
        공개 GitHub 저장소를 살펴보고, 어디서부터 읽을지와 실행 방법을 알려주는
        가이드를 만듭니다. 궁금한 점은 코드 근거와 함께 물어볼 수 있어요.
      </p>

      {phase === 'idle' && (
        <>
          <form
            onSubmit={onSubmit}
            className="mt-8 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm"
          >
            <label
              htmlFor="repository"
              className="mb-2 block text-sm font-semibold"
            >
              GitHub 저장소 주소
            </label>
            <input
              id="repository"
              type="url"
              required
              value={url}
              onChange={event => setUrl(event.target.value)}
              placeholder="https://github.com/owner/repository"
              className={INPUT_CLASS}
            />
            <label
              htmlFor="branch"
              className="mb-2 mt-4 block text-sm font-semibold"
            >
              브랜치{' '}
              <span className="font-normal text-slate-400">
                (선택, 비우면 기본 브랜치)
              </span>
            </label>
            <input
              id="branch"
              value={branch}
              onChange={event => setBranch(event.target.value)}
              placeholder="main"
              className={INPUT_CLASS}
            />
            <LevelPicker value={level} onChange={setLevel} />
            {error && <p className="mt-3 text-sm text-red-600">{error}</p>}
            <button
              type="submit"
              disabled={submitting || !url.trim()}
              className="mt-5 h-12 w-full rounded-lg bg-indigo-600 font-medium text-white transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {submitting ? '요청 중…' : '가이드 만들기'}
            </button>
          </form>
          <RecentReports onPick={onPickRecent} />
        </>
      )}

      {phase !== 'idle' && (
        <section className="mt-8 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
          <div className="flex items-center justify-between gap-4">
            <div className="min-w-0">
              <h2 className="font-semibold">{phaseTitle(phase)}</h2>
              {repo && (
                <p className="truncate text-sm text-slate-500">{repo}</p>
              )}
            </div>
            <button
              type="button"
              onClick={reset}
              className="shrink-0 text-sm text-slate-500 hover:text-slate-800"
            >
              새로 시작
            </button>
          </div>
          <ProgressList events={events} finished={phase !== 'running'} />
          {phase === 'failed' && error && (
            <p className="mt-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">
              {error}
            </p>
          )}
          {phase === 'done' && jobId && (
            <>
              <ReportView jobId={jobId} />
              <QuestionPanel jobId={jobId} />
            </>
          )}
        </section>
      )}
    </main>
  );
}
