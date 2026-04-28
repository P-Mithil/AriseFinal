# Verification and `time_slot_log` CSV

## Period column

Use **`PRE`** / **`POST`** for half-semester, or **`PreMid`** / **`PostMid`**. All are normalized internally to `PRE` / `POST` so faculty and section overlap checks treat the same half-semester consistently.

## Working hours

Allowed session window is **`DAY_START_TIME`–`DAY_END_TIME`** in [`config/schedule_config.py`](../../config/schedule_config.py) (single source of truth for generation and post-generate verify).

## Multiple lectures same day

Post-generate verification allows up to the **LTPSC lecture slot count** for the same course/section/day/period (aligned with Phase 5 fallback scheduling).

## Faculty conflicts (within a period)

By default, a **faculty clash** is reported when the **same instructor** has **overlapping times on the same day** and both sessions are in the **same normalized period** (`PRE` vs `POST`). That is true even if one course is for **Sem1** and another for **Sem3**—one person still cannot teach two classes at once.

Optional: set **`FACULTY_VERIFY_REQUIRE_SHARED_PROGRAM_SEMESTER`** to **True** in [`config/schedule_config.py`](../../config/schedule_config.py) only if you want to **suppress** reports when the two sessions have **no shared `SemN`** in section labels (special cases / data quirks; not the usual rule).
