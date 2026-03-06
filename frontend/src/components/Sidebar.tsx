import type { LabelsConfig } from '../types'
import './Sidebar.css'

interface SidebarProps {
  labels: LabelsConfig | null
  selectedSection: string | null
  selectedPeriod: string
  onSelectSection: (section: string | null) => void
  onSelectPeriod: (period: string) => void
}

function Sidebar({ labels, selectedSection, selectedPeriod, onSelectSection, onSelectPeriod }: SidebarProps) {
  const programs = labels?.programs ?? []
  const sectionLabels = labels?.section_labels ?? []

  const handleSectionClick = (section: string) => {
    if (selectedSection === section) {
      onSelectSection(null)
    } else {
      onSelectSection(section)
    }
  }

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <div className="sidebar-title">Timetable Pro</div>
        <div className="sidebar-subtitle">IIIT Dharwad</div>
      </div>

      <div className="sidebar-section">
        <div className="sidebar-section-title">Period</div>
        <div className="sidebar-period-toggle">
          <button
            type="button"
            className={selectedPeriod === 'PRE' ? 'sidebar-pill active' : 'sidebar-pill'}
            onClick={() => onSelectPeriod('PRE')}
          >
            PreMid
          </button>
          <button
            type="button"
            className={selectedPeriod === 'POST' ? 'sidebar-pill active' : 'sidebar-pill'}
            onClick={() => onSelectPeriod('POST')}
          >
            PostMid
          </button>
        </div>
      </div>

      <div className="sidebar-section">
        <div className="sidebar-section-title">Sections</div>
        {programs.length === 0 ? (
          <div className="sidebar-empty">Click Generate to load programs and sections.</div>
        ) : (
          <div className="sidebar-programs">
            {programs.map((program) => (
              <div key={program.id} className="sidebar-program">
                <div className="sidebar-program-name">{program.name}</div>
                <div className="sidebar-section-list">
                  {sectionLabels
                    .filter((s) => s.program === program.id)
                    .map((s) => (
                      <button
                        type="button"
                        key={s.section}
                        className={
                          selectedSection === s.section ? 'sidebar-section-item active' : 'sidebar-section-item'
                        }
                        onClick={() => handleSectionClick(s.section)}
                      >
                        {s.label} ({s.section})
                      </button>
                    ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </aside>
  )
}

export default Sidebar

