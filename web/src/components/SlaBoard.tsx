import { formatLeft, label, type Journey, type SlaStatus } from '../api'

const ORDER: SlaStatus[] = ['breached', 'at_risk', 'on_track', 'closed']

export function countBy(journeys: Journey[]): Record<SlaStatus, number> {
  const out = { breached: 0, at_risk: 0, on_track: 0, closed: 0 }
  journeys.forEach((j) => { out[j.sla.status] += 1 })
  return out
}

interface Props {
  journeys: Journey[]
  selected: string | null
  onSelect: (id: string) => void
}

export function SlaBoard({ journeys, selected, onSelect }: Props) {
  const counts = countBy(journeys)
  return (
    <section className="card" aria-label="SLA board">
      <div className="counts">
        {ORDER.map((s) => (
          <span key={s} className={`count ${s}`}>{label(s)} <strong>{counts[s]}</strong></span>
        ))}
      </div>
      <ul className="board">
        {journeys.map((j) => (
          <li key={j.journey_id}>
            <button className={`row ${selected === j.journey_id ? 'on' : ''}`} onClick={() => onSelect(j.journey_id)}>
              <span className={`dot ${j.sla.status}`} aria-label={j.sla.status} />
              <strong>{j.journey_id}</strong>
              <span className="muted"> {j.urgency}</span>
              <span className={`left ${j.sla.status}`}>{formatLeft(j.sla.hours_left)}</span>
              <div className="muted small">{label(j.status)} · {j.missing_facts.length ? j.missing_facts.map(label).join(', ') : label(j.pend_reason ?? '')}</div>
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}
