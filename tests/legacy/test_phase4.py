import json
from modules_v2.phase1_data_validation_v2 import run_phase1
from modules_v2.phase4_combined_classes_v2_corrected import run_phase4_corrected
from config.structure_config import DEPARTMENTS, SECTIONS_BY_DEPT, get_group_for_section
from utils.data_models import Section

courses, classrooms, _ = run_phase1()
sections = []
for dept in DEPARTMENTS:
    for sem in [1,3,5,7]:
        for l in SECTIONS_BY_DEPT.get(dept, []):
            sections.append(Section(dept, get_group_for_section(dept, l), l, sem, 60))

schedule = run_phase4_corrected(courses, sections, classrooms)
sem1 = schedule.get(1, {})
slots = sem1.get('premid', {}).get('slots', [])
ma162_slots = [s for s in slots if s[0] == 'MA162']
print("MA162 slots in Phase 4 return:", len(ma162_slots))
for s in ma162_slots:
    print(s)
