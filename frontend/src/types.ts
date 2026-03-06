export interface Session {
  // Optional stable identifier used by the UI for drag-and-drop.
  id?: string;
  Phase: string;
  'Course Code': string;
  Section: string;
  Day: string;
  'Start Time': string;
  'End Time': string;
  Room: string;
  Period: string;
  'Session Type': string;
  Faculty: string;
}

export interface ProgramLabel {
  id: string;
  name: string;
  sections: string[];
}

export interface SectionLabel {
  section: string;
  program: string;
  semester: number;
  label: string;
}

export interface LabelsConfig {
  working_days: string[];
  day_start: string;
  day_end: string;
  lunch_windows: Record<string, string[]>;
  programs: ProgramLabel[];
  section_labels: SectionLabel[];
}

export interface VerificationRow {
  code: string;
  course_name: string;
  instructor?: string;
  ltpsc?: string;
  assigned_lab?: string;
  assigned_classroom?: string;
  lectures?: string;
  tutorials?: string;
  labs?: string;
  status: string;
  time_slot_issues?: string;
  room_conflicts?: string;
}

export interface GenerateResponse {
  success: boolean;
  timetable: Session[];
  labels: LabelsConfig;
  verification_table?: Record<string, VerificationRow[]>;
  log_timestamp?: string;
}

export interface VerifyError {
  rule: string;
  message: string;
  course_code?: string;
  section?: string;
  day?: string;
  time?: string;
}

export interface VerifyResponse {
  success: boolean;
  errors: VerifyError[];
}

export interface ReflowResponse {
  success: boolean;
  not_possible?: boolean;
  timetable?: Session[];
}
