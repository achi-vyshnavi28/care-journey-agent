// Typed client for the Care Journey Agent API (journey/api.py).

export type SlaStatus = 'on_track' | 'at_risk' | 'breached' | 'closed'

export interface Sla {
  urgency: 'standard' | 'expedited'
  due_at: string
  hours_left: number
  status: SlaStatus
  rule: string
}

export interface Journey {
  journey_id: string
  policy_id: string
  urgency: 'standard' | 'expedited'
  pend_reason: 'missing_documentation' | 'criteria_not_met' | null
  missing_facts: string[]
  received_facts: string[]
  provider: { name?: string; npi: string; valid: boolean; taxonomy?: string; fax?: string } | null
  status: string
  sla: Sla
}

export interface Task {
  SK: string
  tool: string
  args: Record<string, unknown>
  result: Record<string, unknown>
  ok: boolean
  at: string
}

export interface OutboxMessage {
  id: number
  kind: 'records_request' | 'member_update' | 'clinician_escalation'
  recipient: string
  body: string
  status: string
}

export interface JourneyDetail extends Journey {
  tasks: Task[]
  outbox: OutboxMessage[]
}

// local dev: Vite proxies /api; deployed: VITE_API_URL, or VITE_API_HOST injected by the Render blueprint
const HOST = import.meta.env.VITE_API_HOST
const BASE = import.meta.env.VITE_API_URL ?? (HOST ? `https://${HOST}` : '/api')

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init)
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(typeof body.detail === 'string' ? body.detail : `${res.status} ${res.statusText}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  board: () => request<Journey[]>('/journeys'),
  get: (id: string) => request<JourneyDetail>(`/journeys/${encodeURIComponent(id)}`),
  recordsReceived: (id: string, facts: string[]) =>
    request<JourneyDetail>(`/journeys/${encodeURIComponent(id)}/records-received`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ facts }),
    }),
}

export const label = (s: string) => s.replace(/_/g, ' ')

export function formatLeft(hours: number): string {
  if (hours <= 0) return `${Math.abs(Math.round(hours))}h overdue`
  if (hours < 48) return `${Math.round(hours)}h left`
  return `${Math.floor(hours / 24)}d ${Math.round(hours % 24)}h left`
}
