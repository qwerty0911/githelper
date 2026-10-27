/**
 * @fileoverview List of recently analyzed repositories.
 */

import {useEffect, useState} from 'react';

import {getRecentReports, LEVEL_LABELS, type RecentReport} from '../api';

interface RecentReportsProps {
  onPick: (report: RecentReport) => void;
}

/**
 * Shows repositories other people analyzed recently. Picking one reuses the
 * cached report, so it opens almost instantly.
 */
export function RecentReports({onPick}: RecentReportsProps) {
  const [reports, setReports] = useState<RecentReport[]>([]);

  useEffect(() => {
    let cancelled = false;
    getRecentReports()
      .then(items => {
        if (!cancelled) setReports(items);
      })
      .catch(() => {
        // The list is optional; hide it when it cannot be loaded.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (reports.length === 0) return null;
  return (
    <section className="mt-8">
      <h2 className="text-sm font-semibold text-slate-600">
        최근 분석된 저장소
      </h2>
      <ul className="mt-3 grid gap-3 sm:grid-cols-2">
        {reports.map(report => (
          <li key={report.repo}>
            <button
              type="button"
              onClick={() => onPick(report)}
              className="w-full rounded-xl border border-slate-200 bg-white p-4 text-left shadow-sm transition hover:border-indigo-300"
            >
              <div className="truncate font-medium">{report.repo}</div>
              {report.summary && (
                <p className="mt-1 line-clamp-2 text-sm text-slate-500">
                  {report.summary}
                </p>
              )}
              <div className="mt-2 text-xs text-slate-400">
                {LEVEL_LABELS[report.level]} · {report.commit_sha.slice(0, 7)}
              </div>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
