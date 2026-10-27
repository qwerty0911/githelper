/**
 * @fileoverview Follow-up Q&A about a finished report.
 */

import {type FormEvent, useEffect, useState} from 'react';

import {askQuestion, listQuestions, type Question} from '../api';

const SUGGESTIONS = [
  '이 프로젝트는 어디서부터 실행돼요?',
  '설정값이나 환경 변수는 어디서 읽어요?',
  '테스트는 어떻게 실행해요?',
];
const MAX_QUESTION_LENGTH = 500;

interface QuestionPanelProps {
  jobId: string;
}

/** Lets the user ask questions answered from the repository's code. */
export function QuestionPanel({jobId}: QuestionPanelProps) {
  const [questions, setQuestions] = useState<Question[]>([]);
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listQuestions(jobId)
      .then(items => {
        if (!cancelled) setQuestions(items);
      })
      .catch(() => {
        // History is optional; start empty when it cannot be loaded.
      });
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  async function ask(question: string) {
    const trimmed = question.trim();
    if (!trimmed || pending) return;
    setPending(trimmed);
    setError(null);
    try {
      const answered = await askQuestion(jobId, trimmed);
      setQuestions(previous => [...previous, answered]);
      setDraft('');
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setPending(null);
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void ask(draft);
  }

  return (
    <section className="mt-8 border-t border-slate-200 pt-6">
      <h3 className="font-semibold">코드에 대해 더 물어보기</h3>
      <p className="mt-1 text-sm text-slate-500">
        같은 커밋의 코드를 다시 찾아보고, 근거 라인과 함께 답해요.
      </p>

      {questions.length === 0 && !pending && (
        <div className="mt-4 flex flex-wrap gap-2">
          {SUGGESTIONS.map(suggestion => (
            <button
              key={suggestion}
              type="button"
              onClick={() => void ask(suggestion)}
              className="rounded-full border border-slate-200 px-3 py-1.5 text-sm text-slate-600 hover:border-indigo-300 hover:text-indigo-700"
            >
              {suggestion}
            </button>
          ))}
        </div>
      )}

      <ol className="mt-4 space-y-4">
        {questions.map(item => (
          <li key={item.id} className="rounded-xl bg-slate-50 p-4">
            <p className="font-medium">Q. {item.question}</p>
            <p className="mt-2 whitespace-pre-line text-sm leading-relaxed text-slate-700">
              {item.answer}
            </p>
            {!item.grounded && (
              <p className="mt-2 text-xs text-amber-700">
                코드에서 근거를 확인하지 못한 답변이에요. 참고만 해 주세요.
              </p>
            )}
            {item.citations.length > 0 && (
              <div className="mt-3 text-xs">
                <p className="text-slate-500">근거</p>
                <ul className="mt-1 space-y-1">
                  {item.citations.map(citation => (
                    <li key={citation.url}>
                      <a
                        href={citation.url}
                        target="_blank"
                        rel="noreferrer"
                        className="font-mono text-indigo-600 hover:underline"
                      >
                        {citation.path}:{citation.start_line}-
                        {citation.end_line}
                      </a>
                      {citation.claim && (
                        <span className="text-slate-600">
                          {' '}
                          — {citation.claim}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </li>
        ))}
        {pending && (
          <li className="rounded-xl bg-slate-50 p-4" aria-live="polite">
            <p className="font-medium">Q. {pending}</p>
            <p className="mt-2 animate-pulse text-sm text-slate-500">
              코드를 찾아보는 중…
            </p>
          </li>
        )}
      </ol>

      <form onSubmit={onSubmit} className="mt-4 flex gap-2">
        <label htmlFor="question" className="sr-only">
          질문
        </label>
        <input
          id="question"
          value={draft}
          maxLength={MAX_QUESTION_LENGTH}
          onChange={event => setDraft(event.target.value)}
          placeholder="예: 로그인은 어떤 파일에서 처리해요?"
          className="h-11 min-w-0 flex-1 rounded-lg border border-slate-300 px-3 outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
        />
        <button
          type="submit"
          disabled={!draft.trim() || pending !== null}
          className="h-11 shrink-0 rounded-lg bg-indigo-600 px-4 text-sm font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          질문
        </button>
      </form>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
    </section>
  );
}
