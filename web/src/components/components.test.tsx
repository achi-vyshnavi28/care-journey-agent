import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { formatLeft, type Journey, type Task } from '../api'
import { AgentTrace } from './AgentTrace'
import { SlaBoard, countBy } from './SlaBoard'

const j = (id: string, status: Journey['sla']['status'], hours: number): Journey => ({
  journey_id: id, policy_id: 'ncd_240_4_cpap', urgency: 'expedited', pend_reason: 'missing_documentation',
  missing_facts: ['ahi'], received_facts: [], provider: null, status: 'awaiting_records',
  sla: { urgency: 'expedited', due_at: '2026-10-02T12:00:00Z', hours_left: hours, status, rule: 'CMS-0057-F' },
})

describe('SLA board', () => {
  it('counts journeys by SLA status and shows time left', () => {
    const js = [j('A', 'breached', -5), j('B', 'at_risk', 10), j('C', 'on_track', 60)]
    expect(countBy(js)).toEqual({ breached: 1, at_risk: 1, on_track: 1, closed: 0 })
    render(<SlaBoard journeys={js} selected={null} onSelect={() => {}} />)
    expect(screen.getByText('5h overdue')).toBeInTheDocument()
    expect(screen.getByText('2d 12h left')).toBeInTheDocument()
  })

  it('opens a journey when clicked', async () => {
    const onSelect = vi.fn()
    render(<SlaBoard journeys={[j('MTS-1308', 'at_risk', 10)]} selected={null} onSelect={onSelect} />)
    await userEvent.click(screen.getByRole('button', { name: /MTS-1308/ }))
    expect(onSelect).toHaveBeenCalledWith('MTS-1308')
  })

  it('formats hours', () => {
    expect(formatLeft(30)).toBe('30h left')
    expect(formatLeft(0)).toBe('0h overdue')
  })
})

describe('Agent trace', () => {
  it('shows guard blocks distinctly from successful steps', () => {
    const tasks: Task[] = [
      { SK: 'TASK#0001', tool: 'request_records', args: {}, result: { error: 'verify the ordering provider before requesting records' }, ok: false, at: '' },
      { SK: 'TASK#0002', tool: 'verify_provider', args: {}, result: { valid: true, name: 'A ATAT PROGRESSIVE PULMONARY & SLEEP MEDICINE PLLC', taxonomy: 'Internal Medicine, Pulmonary Disease' }, ok: true, at: '' },
    ]
    render(<AgentTrace tasks={tasks} />)
    expect(screen.getByText('blocked by guard')).toBeInTheDocument()
    expect(screen.getByText(/A ATAT PROGRESSIVE PULMONARY/)).toBeInTheDocument()
  })
})
