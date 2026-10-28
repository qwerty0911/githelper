/**
 * @fileoverview Typed client for the GitHub Helper HTTP API.
 */

/** Reader level a guide is written for. */
export type Level = 'beginner' | 'junior' | 'senior';

/** Display labels for each reader level. */
export const LEVEL_LABELS: Record<Level, string> = {
  beginner: '입문자 / 바이브코더',
  junior: '주니어 개발자',
  senior: '숙련 개발자',
};

/** One progress event of a job. */
export interface JobEvent {
  at: string;
  step: string;
  message: string;
}

/** Lifecycle state of a job. */
export type JobStatus = 'queued' | 'running' | 'done' | 'failed';

/** A report job as returned by `GET /jobs/:id`. */
export interface Job {
  id: string;
  repo: string;
  branch: string | null;
  level: Level;
  status: JobStatus;
  events: JobEvent[];
  error: string | null;
  commit_sha: string | null;
  cached: boolean;
  report_ready: boolean;
}

/** A recently analyzed repository. */
export interface RecentReport {
  repo: string;
  branch: string | null;
  level: Level;
  commit_sha: string;
  project_name: string | null;
  summary: string | null;
  created_at: string;
}

/** A line range that supports an answer, with a github.com link. */
export interface Citation {
  path: string;
  start_line: number;
  end_line: number;
  url: string;
  /** What the cited lines show. Missing on answers from older versions. */
  claim?: string;
}

/** A box in a diagram, with the code line it was read from. */
export interface DiagramNode {
  id: string;
  label: string;
  /** False when the code does not confirm it; drawn dashed. */
  verified: boolean;
  citation: Citation | null;
}

/** A diagram made while answering a question. */
export interface Diagram {
  title: string;
  /** Read from LangGraph code, a compose file, or proposed by the model. */
  kind: 'langgraph' | 'services' | 'model';
  /** Mermaid source generated and escaped by the server. */
  mermaid: string;
  nodes: DiagramNode[];
}

/** A follow-up question and its verified answer. */
export interface Question {
  id: string;
  question: string;
  answer: string;
  grounded: boolean;
  cached: boolean;
  citations: Citation[];
  /** Missing on answers from older versions. */
  diagrams?: Diagram[];
  created_at: string;
}

async function errorMessage(
  response: Response,
  fallback: string
): Promise<string> {
  const body: unknown = await response.json().catch(() => null);
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as {detail: unknown}).detail;
    if (typeof detail === 'string') return detail;
  }
  return fallback;
}

async function requestJson<T>(
  path: string,
  fallbackError: string,
  init?: RequestInit
): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    throw new Error(await errorMessage(response, fallbackError));
  }
  return (await response.json()) as T;
}

/**
 * Queues a report job, or reuses an identical job already in progress.
 * @return The job id.
 */
export async function createJob(
  url: string,
  branch: string,
  level: Level
): Promise<string> {
  const body = await requestJson<{id: string}>(
    '/api/jobs',
    '요청에 실패했어요.',
    {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({url, branch: branch.trim() || null, level}),
    }
  );
  return body.id;
}

/** Fetches a job's status and progress events. */
export function getJob(id: string): Promise<Job> {
  return requestJson<Job>(`/api/jobs/${id}`, '작업을 찾을 수 없어요.');
}

/** Lists recently analyzed repositories. */
export function getRecentReports(): Promise<RecentReport[]> {
  return requestJson<RecentReport[]>(
    '/api/reports/recent',
    '최근 분석 목록을 불러오지 못했어요.'
  );
}

/** Lists the questions asked about a job's report. */
export function listQuestions(jobId: string): Promise<Question[]> {
  return requestJson<Question[]>(
    `/api/jobs/${jobId}/questions`,
    '질문 기록을 불러오지 못했어요.'
  );
}

/** Asks a follow-up question about a finished report. */
export function askQuestion(
  jobId: string,
  question: string
): Promise<Question> {
  return requestJson<Question>(
    `/api/jobs/${jobId}/questions`,
    '답변을 만들지 못했어요.',
    {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({question}),
    }
  );
}

/** URL of the job's Server-Sent Events progress stream. */
export function eventsUrl(id: string): string {
  return `/api/jobs/${id}/events`;
}

/** URL of the job's rendered HTML report. */
export function reportUrl(id: string): string {
  return `/api/jobs/${id}/report`;
}
