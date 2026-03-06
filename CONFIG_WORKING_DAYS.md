## Configuring Working Days

The timetable engine now takes its list of working days from a single
configuration module: `config/schedule_config.py`.

### Where to change days

In `config/schedule_config.py` you will find:

```python
WORKING_DAYS: List[str] = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
]
```

Edit this list to control which days are used everywhere in the pipeline.
Examples:

- Mon–Thu + Sat (no Friday):
  ```python
  WORKING_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Saturday"]
  ```
- Mon–Sat:
  ```python
  WORKING_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
  ```

After changing `WORKING_DAYS`, re-run `generate_and_verify.py`. All phases and
writers that iterate over days will automatically adapt.

### What is affected

The following behavior now follows `WORKING_DAYS`:

- Core course scheduling (Phase 5) time-slot generation and section overlap
  resolution.
- Remaining ≤2-credit courses (Phase 7) day search space.
- Combined classes (Phase 4) base days used when placing synchronized slots.
- Elective basket scheduling (Phase 3) – selection of the 3 basket days is now
  derived from indices into `WORKING_DAYS`, not hardcoded Mon/Wed/Fri, etc.
- Base 15-minute grid and lunch validation logic (Phase 2) for all days.
- Timetable Excel writers for sections and faculty.
- Conflict resolvers that build lunch blocks per day (faculty and rooms).
- Minor timetable generator: the `DAYS` row labels are derived from
  `WORKING_DAYS` (with standard abbreviations like Mon, Tue, Sat, etc.).

### What is NOT yet configurable here

This configuration **only** controls which days of the week are considered
working days. Other assumptions remain hardcoded for now, for example:

- Daily working hours are fixed to 09:00–18:00.
- Lunch start/end times per semester (Phase 2/5/7/4) remain the same and are
  still anchored on the local lunch window for each semester.
- Branch list and sections per branch (CSE-A/B, DSAI-A, ECE-A) are unchanged.

These can be made configurable later, but are outside the scope of the
`WORKING_DAYS` configuration.

