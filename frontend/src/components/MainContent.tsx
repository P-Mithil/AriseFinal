import { useCallback, useMemo, useState, type ReactNode } from 'react'
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  TouchSensor,
  pointerWithin,
  useSensor,
  useSensors,
  useDraggable,
  useDroppable,
  type DragStartEvent,
  type DragEndEvent,
  type DragCancelEvent,
} from '@dnd-kit/core'
import type {
  Session,
  LabelsConfig,
  GenerateResponse,
  VerifyError,
  VerifyResponse,
  VerificationRow,
} from '../types'
import './MainContent.css'

const API_BASE = 'http://127.0.0.1:8000'

function slotDroppableId(day: string, slotStart: string): string {
  return `slot|${day}|${slotStart}`
}

function DroppableCellTd({
  droppableId,
  className,
  colSpan,
  children,
}: {
  droppableId: string
  className: string
  colSpan?: number
  children?: ReactNode
}) {
  const { setNodeRef, isOver } = useDroppable({ id: droppableId })
  return (
    <td ref={setNodeRef} colSpan={colSpan} className={`${className}${isOver ? ' drop-target' : ''}`}>
      {children ?? null}
    </td>
  )
}

function DraggableSessionBlock({
  session,
}: {
  session: Session
}) {
  const sessionId =
    (session as any).id ?? `${session.Day}-${session['Start Time']}-${session['Course Code']}`
  const color = getColorForCourse(session['Course Code'])
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({ id: sessionId })
  const style: any = {
    backgroundColor: color,
    // When using DragOverlay, keep the source block in-place and fade it out.
    opacity: isDragging ? 0.15 : 1,
  }
  return (
    <div ref={setNodeRef} className="grid-block" style={style} {...listeners} {...attributes}>
      <div className="grid-block-code">{session['Course Code']}</div>
      {session.Room && <div className="grid-block-room">{session.Room}</div>}
      <div className="grid-block-type">{session['Session Type']}</div>
      <div className="grid-block-time">{session['Start Time']} – {session['End Time']}</div>
    </div>
  )
}

interface MainContentProps {
  timetable: Session[] | null
  firstGeneratedTimetable: Session[] | null
  labels: LabelsConfig | null
  verificationTable: Record<string, VerificationRow[]>
  selectedSection: string | null
  selectedPeriod: string
  generateLoading: boolean
  setGenerateLoading: (val: boolean) => void
  onGenerateFirst: (
    timetable: Session[],
    labels: LabelsConfig,
    verification_table?: Record<string, VerificationRow[]>,
    post_generate_verify?: VerifyResponse,
  ) => void
  onTimetableChange: (updated: Session[]) => void
  verifyErrors: VerifyError[]
  verifySuccess: boolean
  onVerifyResult: (success: boolean, errors: VerifyError[]) => void
  revertToFirst: () => void
  message: string | null
  setMessage: (msg: string | null) => void
}

function normalizePeriod(p: string): string {
  const v = (p || '').toUpperCase()
  if (v === 'PREMID' || v === 'PRE') return 'PRE'
  if (v === 'POSTMID' || v === 'POST') return 'POST'
  return v
}

function parseTimeToMinutes(t: string): number {
  const [hh, mm] = t.split(':').map(Number)
  return hh * 60 + mm
}

function minutesToTime(m: number): string {
  const hh = String(Math.floor(m / 60)).padStart(2, '0')
  const mm = String(m % 60).padStart(2, '0')
  return `${hh}:${mm}`
}



function intervalOverlaps(aStart: number, aEnd: number, bStart: number, bEnd: number): boolean {
  return aStart < bEnd && bStart < aEnd
}

/** Normalize "9:00" or "09:00" to "09:00" for consistent matching with slot headers. */
function normalizeTimeStr(t: string): string {
  if (!t || typeof t !== 'string') return ''
  const parts = t.trim().split(':')
  if (parts.length < 2) return t
  const h = Number.parseInt(parts[0], 10)
  const m = Number.parseInt(parts[1], 10)
  if (Number.isNaN(h) || Number.isNaN(m)) return t
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}

const COURSE_COLORS = [
  '#f97373',
  '#60a5fa',
  '#34d399',
  '#fbbf24',
  '#a855f7',
  '#fb7185',
  '#22c55e',
  '#2dd4bf',
  '#38bdf8',
  '#f97316',
]

export function getColorForCourse(courseCode: string): string {
  const baseCode = (courseCode || '').split('-')[0]
  let hash = 0
  for (let i = 0; i < baseCode.length; i++) {
    hash = (hash * 31 + baseCode.charCodeAt(i)) & 0xffffffff
  }
  return COURSE_COLORS[Math.abs(hash) % COURSE_COLORS.length]
}

export function syncAllMovedRelatedSessions(oldTimetable: Session[], newTimetable: Session[]): Session[] {
  let finalState = [...newTimetable]
  const movedIndices: number[] = []
  for (let i = 0; i < oldTimetable.length; i++) {
    const o = oldTimetable[i]
    const n = newTimetable[i]
    if (o.Day !== n.Day || o['Start Time'] !== n['Start Time'] || o['End Time'] !== n['End Time']) {
      movedIndices.push(i)
    }
  }

  // Build a lookup to detect combined courses by actual data:
  // If the same course code + day + start time + session type exists in ANOTHER section,
  // it's a combined / shared course that needs syncing (regardless of Phase label).
  const combinedLookup = new Map<string, number[]>()
  for (let i = 0; i < oldTimetable.length; i++) {
    const s = oldTimetable[i]
    const key = `${s['Course Code']}|${s.Day}|${s['Start Time']}|${s['Session Type']}`
    if (!combinedLookup.has(key)) combinedLookup.set(key, [])
    combinedLookup.get(key)!.push(i)
  }

  for (const idx of movedIndices) {
    const oldS = oldTimetable[idx]
    const newS = finalState[idx]

    // Phase 3 (Electives): sync across ALL sections of the same semester
    const isPhase3 = oldS.Phase?.startsWith('Phase 3')

    // Detect combined course by data: same course+day+time exists in other sections
    const dataKey = `${oldS['Course Code']}|${oldS.Day}|${oldS['Start Time']}|${oldS['Session Type']}`
    const sharedIndices = combinedLookup.get(dataKey) ?? []
    const uniqueSections = new Set(sharedIndices.map(i => oldTimetable[i].Section))
    const isCombinedByData = uniqueSections.size > 1  // exists in multiple sections

    // Also check Phase label as fallback
    const isPhase4Label = oldS.Phase?.startsWith('Phase 4')
    const isCombined = isCombinedByData || isPhase4Label

    if (!isPhase3 && !isCombined) {
      continue
    }

    finalState = finalState.map((cand, candIdx) => {
      if (candIdx === idx) return cand
      const oldCand = oldTimetable[candIdx]

      const isSameSemester = getSemesterFromSection(oldCand.Section) === getSemesterFromSection(oldS.Section)
      const isSameGroup = getGroupForSection(oldCand.Section) === getGroupForSection(oldS.Section)
      // Only sync sessions that were on the SAME original day.
      // Without this, DS161 on Monday AND DS161 on Thursday (two separate lectures per week)
      // would BOTH get moved to the new slot, causing the Thursday block to vanish.
      const isSameDay = oldCand.Day === oldS.Day

      const shouldSync =
        oldCand['Course Code'] === oldS['Course Code'] &&
        oldCand['Session Type'] === oldS['Session Type'] &&
        oldCand['Start Time'] === oldS['Start Time'] &&  // must be same original time slot
        isSameSemester &&
        isSameDay && // must be same original day — prevents vanishing of other weekly occurrences
        (isPhase3 || isSameGroup) // Phase 3: all sections; Combined: same group only

      if (shouldSync) {
        return {
          ...cand,
          Day: newS.Day,
          'Start Time': newS['Start Time'],
          'End Time': newS['End Time'],
        }
      }
      return cand
    })
  }
  return finalState
}

function getSemesterFromSection(section: string | null): number | null {
  if (!section) return null
  const match = section.match(/Sem(\d+)/i)
  if (!match) return null
  const n = Number.parseInt(match[1], 10)
  return Number.isNaN(n) ? null : n
}

function getGroupForSection(section: string | null): number {
  if (!section) return 1
  const s = section.toUpperCase()
  if (s.includes('CSE-A') || s.includes('CSE-B')) return 1
  if (s.includes('DSAI-A') || s.includes('ECE-A')) return 2
  return 1 // Default
}

function MainContent({
  timetable,
  firstGeneratedTimetable,
  labels,
  verificationTable,
  selectedSection,
  selectedPeriod,
  generateLoading,
  setGenerateLoading,
  onGenerateFirst,
  onTimetableChange,
  verifyErrors,
  verifySuccess,
  onVerifyResult,
  revertToFirst,
  message,
  setMessage,
}: MainContentProps) {
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const [generateSheetsLoading, setGenerateSheetsLoading] = useState(false)

  const selectedSemester = useMemo(() => getSemesterFromSection(selectedSection), [selectedSection])

  const workingDays = labels?.working_days ?? ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
  const dayStart = labels?.day_start ?? '09:00'
  const dayEnd = labels?.day_end ?? '18:00'

  const lunchWindow = useMemo(() => {
    if (!labels || selectedSemester == null) return null
    const win = labels.lunch_windows?.[String(selectedSemester)]
    if (!win || win.length !== 2) return null
    const [start, end] = win
    return { start, end }
  }, [labels, selectedSemester])

  const timeSlots = useMemo(() => {
    const start = parseTimeToMinutes(dayStart)
    const end = parseTimeToMinutes(dayEnd)
    const slots: string[] = []
    for (let m = start; m < end; m += 15) {
      const next = Math.min(m + 15, end)
      slots.push(`${minutesToTime(m)}-${minutesToTime(next)}`)
    }
    return slots
  }, [dayStart, dayEnd])

  const filteredSessions = useMemo(() => {
    if (!timetable || !selectedSection) return []
    return timetable.filter(
      (s) =>
        s.Section === selectedSection &&
        normalizePeriod(s.Period) === normalizePeriod(selectedPeriod),
    )
  }, [timetable, selectedSection, selectedPeriod])

  const summaryRows = useMemo(() => {
    if (!timetable || !selectedSection) return []
    return [...filteredSessions].sort((a, b) => {
      if (a.Day === b.Day) {
        return parseTimeToMinutes(a['Start Time']) - parseTimeToMinutes(b['Start Time'])
      }
      return a.Day.localeCompare(b.Day)
    })
  }, [filteredSessions, timetable, selectedSection])

  const sectionPeriodKey = useMemo(
    () => (selectedSection ? `${selectedSection}-${normalizePeriod(selectedPeriod)}` : null),
    [selectedSection, selectedPeriod],
  )
  const verificationRows = useMemo(() => {
    const rows = (sectionPeriodKey && verificationTable[sectionPeriodKey]) ? verificationTable[sectionPeriodKey] : []
    console.log('[DEBUG] sectionPeriodKey:', sectionPeriodKey, '| VT has key?', sectionPeriodKey ? (sectionPeriodKey in verificationTable) : 'N/A', '| rows:', rows.length)
    return rows
  }, [sectionPeriodKey, verificationTable])

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 120, tolerance: 5 } }),
  )

  const handleGenerateFirst = useCallback(async () => {
    setGenerateLoading(true)
    setMessage(null)
    try {
      const res = await fetch(`${API_BASE}/api/generate`, {
        method: 'POST',
      })
      if (!res.ok) {
        const text = await res.text()
        throw new Error(text || res.statusText)
      }
      const data = (await res.json()) as GenerateResponse
      if (!data.success) {
        throw new Error('Generation failed')
      }
      onGenerateFirst(
        data.timetable,
        data.labels,
        data.verification_table,
        data.post_generate_verify,
      )
    } catch (err: any) {
      setMessage(`Generate failed: ${err.message ?? String(err)}`)
    } finally {
      setGenerateLoading(false)
    }
  }, [onGenerateFirst, setGenerateLoading, setMessage])

  const handleVerify = useCallback(async () => {
    if (!timetable) return
    onVerifyResult(false, [])
    setMessage(null)
    try {
      const res = await fetch(`${API_BASE}/api/verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessions: timetable }),
      })
      if (!res.ok) {
        const text = await res.text()
        throw new Error(text || res.statusText)
      }
      const data = (await res.json()) as VerifyResponse
      onVerifyResult(data.success, data.errors ?? [])
    } catch (err: any) {
      setMessage(`Verify failed: ${err.message ?? String(err)}`)
    }
  }, [timetable, onVerifyResult, setMessage])

  const handleGenerateFromSessions = useCallback(async () => {
    if (!timetable) return
    setGenerateSheetsLoading(true)
    setMessage(null)
    try {
      const res = await fetch(`${API_BASE}/api/generate-from-sessions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessions: timetable }),
      })
      if (!res.ok) {
        const text = await res.text()
        throw new Error(text || res.statusText)
      }
      const data = (await res.json()) as GenerateResponse
      if (!data.success) throw new Error('Sheet generation failed')
      // Update timetable with refreshed data
      onGenerateFirst(
        data.timetable,
        data.labels,
        data.verification_table,
        data.post_generate_verify,
      )
      setMessage(
        data.post_generate_verify?.success
          ? '24 sheets generated successfully from your changes.'
          : 'Sheets regenerated; verification reported issues (see below).',
      )
    } catch (err: any) {
      setMessage(`Generate sheets failed: ${err.message ?? String(err)}`)
    } finally {
      setGenerateSheetsLoading(false)
    }
  }, [timetable, onGenerateFirst, setMessage])


  const applyDrop = useCallback(
    (sessionId: string, day: string, slotStart: string) => {
      if (!timetable || !selectedSection) return
      const slotStartMin = parseTimeToMinutes(slotStart)
      const draggedIndex = timetable.findIndex((s) => (s as any).id === sessionId)
      if (draggedIndex === -1) return
      const dragged = timetable[draggedIndex]
      const duration =
        parseTimeToMinutes(dragged['End Time']) - parseTimeToMinutes(dragged['Start Time'])
      const newStartMin = slotStartMin
      const newEndMin = newStartMin + duration

      // Sessions on this day, same section+period, excluding dragged
      const sameDaySessions = filteredSessions
        .filter(
          (s) =>
            s.Day === day &&
            (s as any).id !== sessionId,
        )
        .map((s) => {
          const idx = timetable.findIndex((t) => (t as any).id === (s as any).id)
          const startMin = parseTimeToMinutes(s['Start Time'])
          const endMin = parseTimeToMinutes(s['End Time'])
          return { session: s, idx, startMin, endMin, duration: endMin - startMin }
        })

      // Conflicts: overlap the new interval (newStartMin, newEndMin)
      const conflicts = sameDaySessions
        .filter((c) => intervalOverlaps(c.startMin, c.endMin, newStartMin, newEndMin))
        .sort((a, b) => a.startMin - b.startMin)

      let updated: Session[] = timetable;

      // Reject if the dropped block would overlap with any existing session
      if (conflicts.length > 0) {
        setMessage(`Cannot move: Overlaps with an existing session in this room/schedule.`)
        return
      }
      
      // Reject if it overlaps with lunch or goes past the end of the day
      if (newEndMin > parseTimeToMinutes(dayEnd)) {
        setMessage(`Cannot move: Session would end past the maximum day time (${dayEnd}).`)
        return
      }
      if (lunchWindow) {
        const lunchStart = parseTimeToMinutes(lunchWindow.start)
        const lunchEnd = parseTimeToMinutes(lunchWindow.end)
        if (intervalOverlaps(newStartMin, newEndMin, lunchStart, lunchEnd)) {
          setMessage(`Cannot move: Session overlaps with the designated lunch break.`)
          return
        }
      }

      setMessage(null) // clear message on valid drop
      // Clear stale verification state — a new verify will run automatically after drop
      onVerifyResult(false, [])
      
      updated = timetable.map((s, idx) =>
        idx === draggedIndex
          ? {
            ...s,
            Day: day,
            'Start Time': minutesToTime(newStartMin),
            'End Time': minutesToTime(newEndMin),
          }
          : s,
      )
      // Update with sync: Phase 3 syncs across all sections, Phase 4 syncs within group
      // Phase 5 & 7 are individual — only the dragged block moves
      const synced = syncAllMovedRelatedSessions(timetable, updated)
      onTimetableChange(synced)

      // Auto-verify after every drop to catch cross-section / faculty / room conflicts
      ;(async () => {
        try {
          const res = await fetch(`${API_BASE}/api/verify`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sessions: synced }),
          })
          if (res.ok) {
            const data = (await res.json()) as VerifyResponse
            onVerifyResult(data.success, data.errors ?? [])
          }
        } catch (_err) {
          // silence — user can still manually verify
        }
      })()
    },
    [timetable, filteredSessions, onTimetableChange, onVerifyResult, selectedSection, dayStart, dayEnd, lunchWindow, setMessage],
  )

  const handleDndDragStart = useCallback((e: DragStartEvent) => {
    setActiveSessionId(String(e.active.id))
  }, [])

  const handleDndDragCancel = useCallback((_e: DragCancelEvent) => {
    setActiveSessionId(null)
  }, [])

  const handleDndDragEnd = useCallback(
    (e: DragEndEvent) => {
      const sessionId = String(e.active.id)
      const overId = e.over?.id ? String(e.over.id) : null
      setActiveSessionId(null)
      if (!overId) return

      // Expected droppable id: slot|<day>|<HH:MM>
      const parts = overId.split('|')
      if (parts.length !== 3 || parts[0] !== 'slot') return
      const [, day, slotStart] = parts
      applyDrop(sessionId, day, slotStart)
    },
    [applyDrop],
  )

  const activeSession = useMemo(() => {
    if (!timetable || !activeSessionId) return null
    return timetable.find((s) => (s as any).id === activeSessionId) ?? null
  }, [timetable, activeSessionId])

  const renderGrid = () => {
    if (!labels) {
      return <div className="main-empty">Click Generate to build the timetable.</div>
    }
    if (!selectedSection) {
      return <div className="main-empty">Select a section from the sidebar.</div>
    }
    if (!timetable || filteredSessions.length === 0) {
      return <div className="main-empty">No sessions for this section/period.</div>
    }

    return (
      <div className="grid-wrapper">
        <DndContext
          sensors={sensors}
          collisionDetection={pointerWithin}
          onDragStart={handleDndDragStart}
          onDragCancel={handleDndDragCancel}
          onDragEnd={handleDndDragEnd}
        >
          <table className="grid-table">
            <thead>
              <tr>
                <th>Day / Time</th>
                {timeSlots.map((slot) => {
                  const [slotStart] = slot.split('-')
                  const isHourStart = slotStart.endsWith(':00')
                  return (
                    <th key={slot} className={`time-col${isHourStart ? ' hour-start' : ''}`}>
                      {slotStart}
                    </th>
                  )
                })}
              </tr>
            </thead>
            <tbody>
              {workingDays.map((day) => {
                const sessionsForDay = filteredSessions
                  .filter((s) => s.Day === day)
                  .sort(
                    (a, b) =>
                      parseTimeToMinutes(a['Start Time']) - parseTimeToMinutes(b['Start Time']),
                  )



                const rowCells: ReactNode[] = []
                let i = 0

                while (i < timeSlots.length) {
                  const slot = timeSlots[i]
                  const [slotStart] = slot.split('-')

                  // Lunch block (permanent, non-draggable, non-droppable)
                  if (lunchWindow && normalizeTimeStr(lunchWindow.start) === slotStart) {
                    const startMin = parseTimeToMinutes(lunchWindow.start)
                    const endMin = parseTimeToMinutes(lunchWindow.end)
                    const span = Math.max(1, Math.ceil((endMin - startMin) / 15))
                    rowCells.push(
                      <td
                        key={`${day}-lunch-${slotStart}`}
                        colSpan={span}
                        className="grid-cell lunch"
                      >
                        <div className="grid-block grid-block-lunch">LUNCH BREAK</div>
                      </td>,
                    )
                    i += span
                    continue
                  }

                  // Does a real session start at this slot? (normalize so "9:00" matches "09:00")
                  const session = sessionsForDay.find(
                    (s) => normalizeTimeStr(s['Start Time']) === slotStart,
                  )

                  if (session) {
                    const startMin = parseTimeToMinutes(session['Start Time'])
                    const endMin = parseTimeToMinutes(session['End Time'])
                    const baseSpan = Math.max(1, Math.ceil((endMin - startMin) / 15))
                    const endTimeStr = normalizeTimeStr(session['End Time'])

                    // Check if the slot right after this session is occupied or is lunch.
                    // If empty, extend the block visually by 1 extra slot so the end time
                    // aligns with its column marker (fixes the "15-minute gap" appearance).
                    const nextSlotHasSession = sessionsForDay.some(
                      (s) => normalizeTimeStr(s['Start Time']) === endTimeStr,
                    )
                    const nextSlotIsLunch =
                      lunchWindow && normalizeTimeStr(lunchWindow.start) === endTimeStr
                    const span =
                      nextSlotHasSession || nextSlotIsLunch ? baseSpan : baseSpan + 1

                    const droppableId = slotDroppableId(day, slotStart)

                    rowCells.push(
                      <DroppableCellTd
                        key={`${day}-${slotStart}`}
                        colSpan={span}
                        className="grid-cell occupied"
                        droppableId={droppableId}
                      >
                        <DraggableSessionBlock session={session} />
                      </DroppableCellTd>,
                    )
                    i += span
                    continue
                  }



                  // Empty slot
                  {
                    const droppableId = slotDroppableId(day, slotStart)
                    rowCells.push(
                      <DroppableCellTd
                        key={`${day}-${slot}`}
                        className="grid-cell empty"
                        droppableId={droppableId}
                        children={null}
                      />,
                    )
                  }
                  i += 1
                }

                return (
                  <tr key={day}>
                    <th>{day}</th>
                    {rowCells}
                  </tr>
                )
              })}
            </tbody>
          </table>

          <DragOverlay>
            {activeSession ? (
              <div
                className="grid-block drag-overlay"
                style={{ backgroundColor: getColorForCourse(activeSession['Course Code']) }}
              >
                <div className="grid-block-code">{activeSession['Course Code']}</div>
                <div className="grid-block-room">{activeSession.Room}</div>
                <div className="grid-block-type">{activeSession['Session Type']}</div>
              </div>
            ) : null}
          </DragOverlay>
        </DndContext>
      </div>
    )
  }

  return (
    <main className="main">
      <header className="main-header">
        <div className="main-header-left">
          <h1 className="main-title">All Timetables</h1>
          {selectedSection && (
            <div className="main-subtitle">
              {selectedSection} — {selectedPeriod === 'PRE' ? 'PreMid' : 'PostMid'}
            </div>
          )}
        </div>
        <div className="main-header-actions">
          <button type="button" onClick={handleGenerateFirst} disabled={generateLoading}>
            {generateLoading ? 'Generating…' : 'Generate'}
          </button>
          <button type="button" onClick={handleVerify} disabled={!timetable}>
            Verify Drag Changes
          </button>
          <button type="button" onClick={revertToFirst} disabled={!firstGeneratedTimetable}>
            Revert to first timetable
          </button>
        </div>
      </header>

      {message && <div className="main-message">{message}</div>}

      {verifySuccess && (
        <div className="main-success">
          <span>✅ Verification passed — timetable is conflict-free!</span>
          <button
            type="button"
            className="btn-generate-sheets"
            onClick={handleGenerateFromSessions}
            disabled={generateSheetsLoading}
          >
            {generateSheetsLoading ? 'Generating sheets…' : '📄 Generate 24 Sheets from Changes'}
          </button>
        </div>
      )}

      {verifyErrors.length > 0 && (
        <div className="main-errors">
          <div className="main-errors-title">Violations</div>
          <ul>
            {verifyErrors.map((e, idx) => (
              <li key={idx}>
                <strong>{e.rule}</strong>: {e.message}
                {e.course_code && ` [${e.course_code}]`}
                {e.section && ` (${e.section})`}
                {e.day && ` ${e.day}`}
                {e.time && ` ${e.time}`}
              </li>
            ))}
          </ul>
        </div>
      )}


      <div className="grid-section">
        {labels && (
          <h2 className="grid-section-title">Timetable grid (15-min slots)</h2>
        )}
        {renderGrid()}
      </div>
      {verificationRows.length > 0 ? (
        <div className="summary-wrapper">
          <div className="summary-title">
            Verification – {selectedSection} ({selectedPeriod === 'PRE' ? 'PreMid' : 'PostMid'})
          </div>
          <table className="summary-table summary-table-verification">
            <thead>
              <tr>
                <th>Code</th>
                <th>Course Name</th>
                <th>Instructor</th>
                <th>LTPSC</th>
                <th>Assigned Room(s)</th>
                <th>Lectures (Req/Sched)</th>
                <th>Tutorials (Req/Sched)</th>
                <th>Labs (Req/Sched)</th>
                <th>Status</th>
                <th>Issues / Conflicts</th>
              </tr>
            </thead>
            <tbody>
              {verificationRows.map((row, idx) => (
                <tr key={`${row.code}-${idx}`}>
                  <td>{row.code}</td>
                  <td>{row.course_name}</td>
                  <td>{row.instructor ?? ''}</td>
                  <td>{row.ltpsc ?? ''}</td>
                  <td>
                    {row.assigned_classroom ? <div>Class: {row.assigned_classroom}</div> : null}
                    {row.assigned_lab ? <div>Lab: {row.assigned_lab}</div> : null}
                  </td>
                  <td>{row.lectures ?? ''}</td>
                  <td>{row.tutorials ?? ''}</td>
                  <td>{row.labs ?? ''}</td>
                  <td className={row.status?.toUpperCase() === 'SATISFIED' ? 'status-satisfied' : 'status-unsatisfied'}>
                    {row.status ?? ''}
                  </td>
                  <td className="issues-cell">
                    {row.time_slot_issues && row.time_slot_issues !== 'None' ? <div className="text-error">{row.time_slot_issues}</div> : null}
                    {row.room_conflicts && row.room_conflicts !== 'None' ? <div className="text-error">{row.room_conflicts}</div> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : summaryRows.length > 0 ? (
        <div className="summary-wrapper">
          <div className="summary-title">
            Scheduled Courses – {selectedSection} ({selectedPeriod === 'PRE' ? 'PreMid' : 'PostMid'})
          </div>
          <table className="summary-table">
            <thead>
              <tr>
                <th>Course</th>
                <th>Room</th>
                <th>Day</th>
                <th>Time</th>
                <th>Type</th>
                <th>Faculty</th>
              </tr>
            </thead>
            <tbody>
              {summaryRows.map((s, idx) => (
                <tr key={`${s['Course Code']}-${s.Day}-${s['Start Time']}-${idx}`}>
                  <td>{s['Course Code']}</td>
                  <td>{s.Room}</td>
                  <td>{s.Day}</td>
                  <td>
                    {s['Start Time']}–{s['End Time']}
                  </td>
                  <td>{s['Session Type']}</td>
                  <td>{s.Faculty}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </main>
  )
}

export default MainContent

