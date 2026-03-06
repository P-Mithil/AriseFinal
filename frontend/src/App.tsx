import { useState, useCallback } from 'react'
import Sidebar from './components/Sidebar'
import MainContent from './components/MainContent'
import type { Session, LabelsConfig, VerificationRow } from './types'
import './App.css'

function attachSessionIds(sessions: Session[]): Session[] {
  const now = Date.now()
  return sessions.map((s, idx) => {
    if (s.id) {
      return s
    }
    return {
      ...s,
      id: `sess-${now}-${idx}`,
    }
  })
}

function App() {
  const [labels, setLabels] = useState<LabelsConfig | null>(null)
  const [firstGeneratedTimetable, setFirstGeneratedTimetable] = useState<Session[] | null>(null)
  const [currentTimetable, setCurrentTimetable] = useState<Session[] | null>(null)
  const [selectedSection, setSelectedSection] = useState<string | null>(null)
  const [selectedPeriod, setSelectedPeriod] = useState<string>('PRE')
  const [generateLoading, setGenerateLoading] = useState(false)
  const [verifyErrors, setVerifyErrors] = useState<Array<{ rule: string; message: string; course_code?: string; section?: string; day?: string; time?: string }>>([])
  const [verifySuccess, setVerifySuccess] = useState(false)
  const [showConfirmReflow, setShowConfirmReflow] = useState(false)
  const [reflowLoading, setReflowLoading] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [verificationTable, setVerificationTable] = useState<Record<string, VerificationRow[]>>({})

  const onGenerateFirst = useCallback((
    timetable: Session[],
    newLabels: LabelsConfig,
    verification_table?: Record<string, VerificationRow[]>,
  ) => {
    const withIds = attachSessionIds(timetable)
    setFirstGeneratedTimetable(withIds)
    setCurrentTimetable(withIds)
    setLabels(newLabels)
    setVerificationTable(verification_table ?? {})
    setVerifyErrors([])
    setVerifySuccess(false)
    setShowConfirmReflow(false)
    setMessage('Timetable generated. Select a section from the sidebar.')
  }, [])

  const onTimetableChange = useCallback((updated: Session[]) => {
    setCurrentTimetable(updated)
    setVerifyErrors([])
    setVerifySuccess(false)
    setShowConfirmReflow(false)
  }, [])

  const onVerifyResult = useCallback((success: boolean, errors: Array<{ rule: string; message: string; course_code?: string; section?: string; day?: string; time?: string }>) => {
    setVerifySuccess(success)
    setVerifyErrors(errors)
    if (success) {
      setMessage('Timetable updated.')
      setShowConfirmReflow(false)
    } else {
      setShowConfirmReflow(true)
      setMessage(null)
    }
  }, [])

  const onReflowResult = useCallback(
    (success: boolean, notPossible: boolean, newTimetable?: Session[]) => {
      if (success && newTimetable) {
        const withIds = attachSessionIds(newTimetable)
        setCurrentTimetable(withIds)
        setFirstGeneratedTimetable(withIds)
        setVerifyErrors([])
        setVerifySuccess(true)
        setShowConfirmReflow(false)
        setMessage('Timetable reflowed and updated.')
      } else if (notPossible) {
        setCurrentTimetable(firstGeneratedTimetable)
        setVerifyErrors([])
        setShowConfirmReflow(false)
        setMessage('Not possible. Reverted to first-generated timetable.')
      }
    },
    [firstGeneratedTimetable],
  )

  const revertToFirst = useCallback(() => {
    if (firstGeneratedTimetable) {
      setCurrentTimetable(firstGeneratedTimetable)
      setVerifyErrors([])
      setShowConfirmReflow(false)
      setMessage('Reverted to first-generated timetable.')
    }
  }, [firstGeneratedTimetable])

  return (
    <div className="app">
      <Sidebar
        labels={labels}
        selectedSection={selectedSection}
        selectedPeriod={selectedPeriod}
        onSelectSection={setSelectedSection}
        onSelectPeriod={setSelectedPeriod}
      />
      <MainContent
        timetable={currentTimetable}
        firstGeneratedTimetable={firstGeneratedTimetable}
        labels={labels}
        verificationTable={verificationTable}
        selectedSection={selectedSection}
        selectedPeriod={selectedPeriod}
        generateLoading={generateLoading}
        setGenerateLoading={setGenerateLoading}
        onGenerateFirst={onGenerateFirst}
        onTimetableChange={onTimetableChange}
        verifyErrors={verifyErrors}
        verifySuccess={verifySuccess}
        showConfirmReflow={showConfirmReflow}
        reflowLoading={reflowLoading}
        setReflowLoading={setReflowLoading}
        onVerifyResult={onVerifyResult}
        onReflowResult={onReflowResult}
        revertToFirst={revertToFirst}
        message={message}
        setMessage={setMessage}
      />
    </div>
  )
}

export default App
