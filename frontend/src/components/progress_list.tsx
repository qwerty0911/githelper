/**
 * @fileoverview Live list of job progress events.
 */

import type {JobEvent} from '../api';

interface ProgressListProps {
  events: JobEvent[];
  finished: boolean;
}

/** Shows each progress event, highlighting the step in progress. */
export function ProgressList({events, finished}: ProgressListProps) {
  return (
    <ol className="mt-6 space-y-3" aria-live="polite">
      {events.map((event, index) => {
        const active = !finished && index === events.length - 1;
        const failed = event.step === 'failed';
        let badgeClass = 'bg-emerald-100 text-emerald-600';
        if (failed) badgeClass = 'bg-red-100 text-red-600';
        else if (active) badgeClass = 'bg-indigo-100 text-indigo-600';
        let textClass = 'text-slate-500';
        if (failed) textClass = 'text-red-600';
        else if (active) textClass = 'font-medium';
        return (
          <li key={index} className="flex items-center gap-3 text-sm">
            <span
              className={
                'flex h-6 w-6 shrink-0 items-center justify-center ' +
                `rounded-full text-xs ${badgeClass}`
              }
            >
              {failed && '!'}
              {!failed && active && (
                <span className="h-2 w-2 animate-pulse rounded-full bg-indigo-500" />
              )}
              {!failed && !active && '✓'}
            </span>
            <span className={textClass}>{event.message}</span>
          </li>
        );
      })}
    </ol>
  );
}
