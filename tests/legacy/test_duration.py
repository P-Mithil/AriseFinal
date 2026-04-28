import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from modules_v2.phase1_data_validation_v2 import run_phase1
from modules_v2.phase5_core_courses import run_phase5
from config.structure_config import DEPARTMENTS, SECTIONS_BY_DEPT, STUDENTS_PER_SECTION
from utils.data_models import Section

courses, classrooms, elective_groups = run_phase1()

sections = []
unique_semesters = sorted(set(c.semester for c in courses if c.department in DEPARTMENTS))
for dept in DEPARTMENTS:
    for sem in unique_semesters:
        for sec_label in SECTIONS_BY_DEPT.get(dept, []):
            group = 1
            if 'DSAI' in dept or 'ECE' in dept: group = 2
            sections.append(Section(dept, group, sec_label, sem, STUDENTS_PER_SECTION))

scheduled_sessions, phase5_courses = run_phase5(courses, sections, classrooms, {})

for sess in scheduled_sessions:
    if sess.course_code == 'DS161' and sess.section == 'DSAI-A-Sem1':
        print(f"DS161 {sess.kind}: {sess.block.start}-{sess.block.end}")
