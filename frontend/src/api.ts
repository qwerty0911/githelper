export type JobEvent = { at: string; step: string; message: string };

export type Job = {
  id: string;
  repo: string;
  branch: string | null;
  status: "queued" | "running" | "done" | "failed";
  events: JobEvent[];
  error: string | null;
  commit_sha: string | null;
  cached: boolean;
  report_ready: boolean;
};

export async function createJob(url: string, branch: string): Promise<string> {
  const response = await fetch("/api/jobs", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ url, branch: branch.trim() || null }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(typeof body?.detail === "string" ? body.detail : "요청에 실패했어요.");
  }
  return (await response.json()).id;
}

export async function getJob(id: string): Promise<Job> {
  const response = await fetch(`/api/jobs/${id}`);
  if (!response.ok) throw new Error("작업을 찾을 수 없어요.");
  return response.json();
}

export const eventsUrl = (id: string) => `/api/jobs/${id}/events`;
export const reportUrl = (id: string) => `/api/jobs/${id}/report`;
