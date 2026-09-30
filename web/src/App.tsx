import { useCallback, useEffect, useState } from 'react'
import { api, formatLeft, label, type Journey, type JourneyDetail } from './api'
import { AgentTrace } from './components/AgentTrace'
import { SlaBoard } from './components/SlaBoard'
import './App.css'

export default function App() {
  const [journeys, setJourneys] = useState<Journey[]>([])
  const [detail, setDetail] = useState<JourneyDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setJourneys(await api.board())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not reach the API')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function open(id: string) {
    try {
      setDetail(await api.get(id))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load the journey')
    }
  }

  async function received() {
    if (!detail) return
    const pending = detail.missing_facts.filter((f) => !detail.received_facts.includes(f))
    if (pending.length === 0) return
    await api.recordsReceived(detail.journey_id, pending)
    await Promise.all([load(), open(detail.journey_id)])
  }

  return (
    <main>
      <header>
        <h1>Care Journey Agent</h1>
        <p className="muted">Pended prior-authorization cases · CMS-0057-F deadlines (72h expedited, 7 days standard) · agent steps and guards</p>
      </header>
      {error && <p role="alert" className="error">{error}. Start the API with: python scripts/demo_api.py</p>}
      <div className="layout">
        <SlaBoard journeys={journeys} selected={detail?.journey_id ?? null} onSelect={(id) => void open(id)} />
        <section className="card" aria-label="Journey">
          {!detail && <p className="muted">Select a journey to see what the agent did and what it queued.</p>}
          {detail && (
            <>
              <h2>{detail.journey_id} <span className="muted">· {label(detail.policy_id)}</span></h2>
              <p className={`sla ${detail.sla.status}`}>
                {label(detail.sla.status)} · {formatLeft(detail.sla.hours_left)} · due {new Date(detail.sla.due_at).toLocaleString()}
              </p>
              {detail.provider && (
                <p className="small">Ordering provider (NPPES): {detail.provider.valid ? `${detail.provider.name} · NPI ${detail.provider.npi}` : `NPI ${detail.provider.npi} not found`}</p>
              )}
              <h3>Agent trace</h3>
              <AgentTrace tasks={detail.tasks} />
              <h3>Outbox (queued, sent by staff or an integration)</h3>
              <ul className="outbox">
                {detail.outbox.map((m) => (
                  <li key={m.id}><strong>{label(m.kind)}</strong> → {m.recipient}<pre>{m.body}</pre></li>
                ))}
              </ul>
              {detail.status === 'awaiting_records' && (
                <button className="primary" onClick={() => void received()}>Mark requested records received</button>
              )}
            </>
          )}
        </section>
      </div>
    </main>
  )
}
