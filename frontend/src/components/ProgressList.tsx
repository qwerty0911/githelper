import type { JobEvent } from "../api";

type Props = { events: JobEvent[]; status: string };

export default function ProgressList({ events, status }: Props) {
  const finished = status === "done" || status === "failed";
  return (
    <ol className="mt-6 space-y-3" aria-live="polite">
      {events.map((event, index) => {
        const active = !finished && index === events.length - 1;
        const failed = event.step === "failed";
        return (
          <li key={index} className="flex items-center gap-3 text-sm">
            <span
              className={
                "flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs " +
                (failed
                  ? "bg-red-100 text-red-600"
                  : active
                    ? "bg-indigo-100 text-indigo-600"
                    : "bg-emerald-100 text-emerald-600")
              }
            >
              {failed ? "!" : active ? <span className="h-2 w-2 animate-pulse rounded-full bg-indigo-500" /> : "✓"}
            </span>
            <span className={failed ? "text-red-600" : active ? "font-medium" : "text-slate-500"}>
              {event.message}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
