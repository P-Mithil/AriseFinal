# Optional column: Offering_ID (dataset primary key)

For `course_data.xlsx`, you may add **one** of these column names (first match wins):

- `Offering_ID` (recommended)
- `Offering ID`
- `OFFERING_ID`
- `Schedule_Key` / `Schedule Key` / `SCHEDULE_KEY`

## Behavior

1. **If `Offering_ID` is set (non-empty)**  
   Rows that share the same ID are merged for Phase 5 (one timetable structure, multiple departments in `_departments_list`).  
   Use the **same** ID on each department row when the offering is truly the same (same LTPSC, shared schedule).

2. **If the column is missing or empty**  
   Rows are merged only when **Course Code + Semester + Credits + LTPSC** (normalized) all match.  
   Different LTPSC for the same code/semester/credits → **separate** Phase 5 entries (fixes lab-vs-tutorial mismatches vs verification).

## Recommendations

- Add `Offering_ID` when you upload varied datasets and want explicit control (e.g. `CS307-S5-2025-A`).
- Keep LTPSC identical for all rows that share an `Offering_ID`; otherwise the scheduler keeps the first row’s LTPSC and logs a warning.
- **Do not** list the same Course Code twice for the same Department with different LTPSC unless they are truly different offerings; if you must, use distinct `Offering_ID` values. Phase 5 schedules **one** row per (section, course code), matching that section’s department (or the first ambiguous row with a warning).
