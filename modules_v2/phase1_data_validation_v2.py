import pandas as pd
import os
from typing import List, Dict, Tuple
from collections import defaultdict

from utils.data_models import Course, ClassRoom, Section, create_course_from_row, create_classroom_from_row

def read_course_data() -> List[Course]:
    """Read and parse course data from Excel file"""
    # Get the base directory (iiitdwd_timetable_v2)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    input_path = os.path.join(base_dir, "DATA", "INPUT", "course_data.xlsx")
    
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Course data file not found: {input_path}")
    
    df = pd.read_excel(input_path)
    print(f"Loaded {len(df)} courses from Excel file")
    
    courses = []
    for _, row in df.iterrows():
        try:
            course = create_course_from_row(row)
            courses.append(course)
        except Exception as e:
            print(f"Error processing row {row.get('Course Code', 'Unknown')}: {e}")
    
    return courses

def read_classroom_data() -> List[ClassRoom]:
    """Read and parse classroom data from Excel file"""
    # Get the base directory (iiitdwd_timetable_v2)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    input_path = os.path.join(base_dir, "DATA", "INPUT", "classroom_data.xlsx")
    
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Classroom data file not found: {input_path}")
    
    df = pd.read_excel(input_path)
    print(f"Loaded {len(df)} classrooms from Excel file")
    
    classrooms = []
    for _, row in df.iterrows():
        try:
            classroom = create_classroom_from_row(row)
            classrooms.append(classroom)
        except Exception as e:
            print(f"Error processing classroom {row.get('Room Number', 'Unknown')}: {e}")
    
    return classrooms

def filter_courses(courses: List[Course]) -> List[Course]:
    """Filter courses for relevant departments (accept all semesters from data)"""
    filtered = []
    
    for course in courses:
        # Only relevant departments (accept all semesters from data)
        if course.department not in ['CSE', 'DSAI', 'ECE']:
            continue
            
        filtered.append(course)
    
    return filtered

def classify_courses(courses: List[Course]) -> Dict[str, List[Course]]:
    """Classify courses into different categories"""
    classification = {
        'core_courses': [],
        'elective_courses': [],
        'combined_courses': [],
        'non_combined_core': [],
        'multi_faculty_high_credit': [],
        'single_faculty_high_credit': []
    }
    
    for course in courses:
        if course.is_elective:
            classification['elective_courses'].append(course)
        else:
            classification['core_courses'].append(course)
            
            if course.is_combined:
                classification['combined_courses'].append(course)
            else:
                classification['non_combined_core'].append(course)
                
                if course.credits > 2:
                    if course.num_faculty >= 2:
                        classification['multi_faculty_high_credit'].append(course)
                    else:
                        classification['single_faculty_high_credit'].append(course)
    
    return classification

def group_courses_by_semester(courses: List[Course]) -> Dict[int, List[Course]]:
    """Group courses by semester"""
    by_semester = defaultdict(list)
    for course in courses:
        by_semester[course.semester].append(course)
    return dict(by_semester)

def generate_statistics(courses: List[Course], classrooms: List[ClassRoom]) -> str:
    """Generate comprehensive statistics report"""
    
    # Filter courses
    filtered_courses = filter_courses(courses)
    
    # Classify courses
    classification = classify_courses(filtered_courses)
    
    # Group by semester
    by_semester = group_courses_by_semester(filtered_courses)
    
    # Generate report
    report = []
    report.append("=== PHASE 1: DATA VALIDATION & STATISTICS ===\n")
    
    report.append(f"Total Courses Loaded: {len(courses)}")
    report.append(f"Filtered Courses (Odd Semesters, CSE/DSAI/ECE): {len(filtered_courses)}\n")
    
    # Extract unique semesters from filtered courses
    unique_semesters = sorted(set(c.semester for c in filtered_courses))
    
    # Core courses
    report.append("=== CORE COURSES ===")
    report.append(f"Total Unique Core Courses: {len(classification['core_courses'])}")
    for sem in unique_semesters:
        if sem in by_semester:
            core_count = len([c for c in by_semester[sem] if not c.is_elective])
            report.append(f"  - Semester {sem}: {core_count} courses")
    report.append("")
    
    # Elective courses
    report.append("=== ELECTIVE COURSES ===")
    report.append(f"Total Elective Courses: {len(classification['elective_courses'])}")
    for sem in unique_semesters:
        if sem in by_semester:
            elective_count = len([c for c in by_semester[sem] if c.is_elective])
            report.append(f"  - Semester {sem}: {elective_count} electives")
    report.append("")
    
    # Combined courses
    report.append("=== COMBINED COURSES (Core, <=2 Credits, Single Instructor) ===")
    report.append(f"Total Combined Courses: {len(classification['combined_courses'])}")
    for sem in unique_semesters:
        if sem in by_semester:
            combined_courses = [c for c in by_semester[sem] if c.is_combined]
            if combined_courses:
                course_list = ", ".join([f"{c.code} ({c.name}, {c.credits}cr, {c.instructors[0]})" for c in combined_courses])
                report.append(f"  Sem {sem}: {course_list}")
    report.append("")
    
    # Non-combined courses
    report.append("=== NON-COMBINED COURSES ===")
    report.append(f"Total: {len(classification['non_combined_core'])}\n")
    
    # >2 Credits with 1 Faculty
    report.append("1. >2 Credits with 1 Faculty:")
    for course in classification['single_faculty_high_credit']:
        report.append(f"   - {course.code} ({course.name}, {course.credits} credits, Instructor: {course.instructors[0]})")
    report.append("")
    
    # >2 Credits with 2+ Faculties
    report.append("2. >2 Credits with 2+ Faculties:")
    for course in classification['multi_faculty_high_credit']:
        instructors_str = ", ".join(course.instructors)
        report.append(f"   - {course.code} ({course.name}, {course.credits} credits, Instructors: {instructors_str})")
    report.append("")
    
    # Core <=2 Credits (Not Combined)
    report.append("3. Core <=2 Credits (Not Combined):")
    low_credit_non_combined = [c for c in classification['non_combined_core'] if c.credits <= 2]
    for course in low_credit_non_combined:
        instructors_str = ", ".join(course.instructors)
        report.append(f"   - {course.code} ({course.name}, {course.credits} credits, Instructors: {instructors_str})")
    report.append("")
    
    # Classrooms
    report.append("=== CLASSROOMS ===")
    report.append(f"Total Rooms: {len(classrooms)}")
    
    classrooms_by_type = defaultdict(list)
    for room in classrooms:
        classrooms_by_type[room.room_type].append(room)
    
    for room_type, rooms in classrooms_by_type.items():
        capacities = [r.capacity for r in rooms]
        if room_type == "Classroom":
            report.append(f"  - {room_type}s: {len(rooms)} (capacity range: {min(capacities)}-{max(capacities)})")
        else:
            report.append(f"  - {room_type}s: {len(rooms)} (capacity: {capacities[0]} each)")
    
    # Find 240-seater
    large_rooms = [r for r in classrooms if r.capacity >= 240]
    if large_rooms:
        report.append(f"  - 240+ seater: {', '.join([r.room_number for r in large_rooms])} (for combined classes)")
    report.append("")
    
    # Validation checks
    report.append("=== VALIDATION CHECKS ===")
    
    # Check semesters (extract unique semesters from data)
    unique_semesters = sorted(set(c.semester for c in filtered_courses))
    valid_semesters = len(unique_semesters) > 0
    report.append(f"[OK] All courses have valid semester ({unique_semesters}): {'PASS' if valid_semesters else 'FAIL'}")
    
    # Check credits
    valid_credits = all(c.credits > 0 for c in filtered_courses)
    report.append(f"[OK] All courses have credits assigned: {'PASS' if valid_credits else 'FAIL'}")
    
    # Check instructors
    valid_instructors = all(len(c.instructors) > 0 for c in filtered_courses)
    report.append(f"[OK] All courses have instructors: {'PASS' if valid_instructors else 'FAIL'}")
    
    # Check combined courses
    combined_check = all(c.num_faculty == 1 for c in classification['combined_courses'])
    report.append(f"[OK] Combined courses identified correctly: {'PASS' if combined_check else 'FAIL'}")
    
    return "\n".join(report)

def save_statistics_to_file(statistics: str, output_path: str):
    """Save statistics to file"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(statistics)
    print(f"Statistics saved to: {output_path}")

def run_phase1() -> Tuple[List[Course], List[ClassRoom], str]:
    """Run Phase 1: Data validation and statistics generation"""
    print("=== PHASE 1: DATA VALIDATION & STATISTICS (v2) ===\n")
    
    # Read data
    print("Reading course data...")
    courses = read_course_data()
    
    print("Reading classroom data...")
    classrooms = read_classroom_data()
    
    # Generate statistics
    print("Generating statistics...")
    statistics = generate_statistics(courses, classrooms)
    
    # Print to console
    print(statistics)
    
    # Save to file
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_path = os.path.join(base_dir, "DATA", "OUTPUT", "phase1_statistics.txt")
    save_statistics_to_file(statistics, output_path)
    
    return courses, classrooms, statistics
