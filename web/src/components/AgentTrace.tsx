import { label, type Task } from '../api'

function summary(t: Task): string {
  const r = t.result as Record<string, unknown>
  if (!t.ok) return String(r.error ?? 'blocked')
  if (t.tool === 'verify_provider') return r.valid ? `${r.name} · ${r.taxonomy ?? ''} · fax ${r.fax ?? 'n/a'}` : 'not found in NPPES'
  if (t.tool === 'coverage_policy') return `NCD ${r.ncd}: ${r.title}`
  if (t.tool === 'request_records') return `requested ${(r.facts as string[] | undefined)?.map(label).join(', ')}${r.queued ? '' : ' (already queued)'}`
  return r.queued === false ? 'already queued' : 'queued'
}

export function AgentTrace({ tasks }: { tasks: Task[] }) {
  return (
    <ol className="trace" aria-label="Agent trace">
      {tasks.map((t) => (
        <li key={t.SK} className={t.ok ? 'ok' : 'blocked'}>
          <strong>{label(t.tool)}</strong>
          {!t.ok && <span className="badge">blocked by guard</span>}
          <div className="small">{summary(t)}</div>
        </li>
      ))}
    </ol>
  )
}
