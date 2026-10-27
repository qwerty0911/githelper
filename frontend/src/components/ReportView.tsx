import { reportUrl } from "../api";

export default function ReportView({ jobId }: { jobId: string }) {
  return (
    <div className="mt-6">
      <div className="mb-3 flex justify-end">
        <a
          href={reportUrl(jobId)}
          target="_blank"
          rel="noreferrer"
          className="text-sm text-indigo-600 hover:underline"
        >
          새 탭에서 열기 ↗
        </a>
      </div>
      {/* Report HTML is generated from untrusted repo content: sandbox it with no permissions. */}
      <iframe
        title="리포트"
        src={reportUrl(jobId)}
        sandbox=""
        className="h-[70vh] w-full rounded-xl border border-slate-200 bg-white"
      />
    </div>
  );
}
