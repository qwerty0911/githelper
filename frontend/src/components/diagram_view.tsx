/**
 * @fileoverview Renders an answer's diagram with Mermaid and its evidence.
 */

import {useEffect, useId, useState} from 'react';

import type {Diagram} from '../api';

const KIND_LABELS: Record<Diagram['kind'], string> = {
  langgraph: '코드에서 추출한 LangGraph 흐름',
  services: 'docker-compose에서 추출한 서비스 구성',
  model: 'AI가 코드 근거로 그린 그림',
};

type MermaidApi = typeof import('mermaid').default;
let mermaidLoader: Promise<MermaidApi> | null = null;

/** Loads Mermaid once, on first use, so the main bundle stays small. */
function loadMermaid(): Promise<MermaidApi> {
  mermaidLoader ??= import('mermaid').then(module => {
    const mermaid = module.default;
    // Strict mode escapes labels and disables click handlers and scripts.
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'neutral',
    });
    return mermaid;
  });
  return mermaidLoader;
}

interface DiagramViewProps {
  diagram: Diagram;
}

/** Shows one diagram, a legend, and links from each box to its code. */
export function DiagramView({diagram}: DiagramViewProps) {
  const [svg, setSvg] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const renderId = `diagram-${useId().replace(/[^a-zA-Z0-9-]/g, '')}`;
  const evidence = diagram.nodes.filter(node => node.citation);
  const hasUnverified = diagram.nodes.some(node => !node.verified);

  useEffect(() => {
    let cancelled = false;
    loadMermaid()
      .then(mermaid => mermaid.render(renderId, diagram.mermaid))
      .then(result => {
        if (!cancelled) setSvg(result.svg);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [diagram.mermaid, renderId]);

  return (
    <figure className="mt-3 rounded-lg border border-slate-200 bg-white p-3">
      <figcaption className="text-sm font-medium">{diagram.title}</figcaption>
      <p className="text-xs text-slate-500">{KIND_LABELS[diagram.kind]}</p>
      {failed && (
        <pre className="mt-2 overflow-x-auto text-xs text-slate-600">
          {diagram.mermaid}
        </pre>
      )}
      {!failed && !svg && (
        <p className="mt-2 animate-pulse text-xs text-slate-400">
          그림을 그리는 중…
        </p>
      )}
      {svg && (
        <div
          className="mt-2 overflow-x-auto [&_svg]:mx-auto [&_svg]:max-w-full"
          // The SVG comes from Mermaid in strict mode, rendering source the
          // server built from escaped labels.
          dangerouslySetInnerHTML={{__html: svg}}
        />
      )}
      {hasUnverified && (
        <p className="mt-2 text-xs text-amber-700">
          점선은 코드에서 근거를 확인하지 못한 부분이에요.
        </p>
      )}
      {evidence.length > 0 && (
        <details className="mt-2 text-xs">
          <summary className="cursor-pointer text-slate-500">
            그림의 근거 코드 ({evidence.length})
          </summary>
          <ul className="mt-1 space-y-1">
            {evidence.map(node => (
              <li key={node.id}>
                <span className="text-slate-600">{node.label}</span>{' '}
                <a
                  href={node.citation?.url}
                  target="_blank"
                  rel="noreferrer"
                  className="font-mono text-indigo-600 hover:underline"
                >
                  {node.citation?.path}:{node.citation?.start_line}
                </a>
              </li>
            ))}
          </ul>
        </details>
      )}
    </figure>
  );
}
