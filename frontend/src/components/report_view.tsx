/**
 * @fileoverview Sandboxed view of a generated report.
 */

import {reportUrl} from '../api';

interface ReportViewProps {
  jobId: string;
}

/** Embeds the report HTML in an iframe that cannot run scripts. */
export function ReportView({jobId}: ReportViewProps) {
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
      {/*
        The report is generated from untrusted repository content, so the
        iframe gets no script or same-origin access. Popups are allowed only
        so citation links can open github.com in a new tab.
      */}
      <iframe
        title="리포트"
        src={reportUrl(jobId)}
        sandbox="allow-popups allow-popups-to-escape-sandbox"
        className="h-[70vh] w-full rounded-xl border border-slate-200 bg-white"
      />
    </div>
  );
}
