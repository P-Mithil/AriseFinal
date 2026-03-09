"""
Phase 10: Course Color Assignment
Assigns unique colors to each course code consistently across all sheets.
"""

from typing import List, Dict

# Color palette mapped exactly to the frontend UI's hashing algorithm
COLOR_PALETTE = [
  'f97373',
  '60a5fa',
  '34d399',
  'fbbf24',
  'a855f7',
  'fb7185',
  '22c55e',
  '2dd4bf',
  '38bdf8',
  'f97316',
]

def get_color_for_course_code(code: str) -> str:
    base = (code or '').split('-')[0]
    hash_val = 0
    for char in base:
        hash_val = ((hash_val * 31) + ord(char)) & 0xFFFFFFFF
    return COLOR_PALETTE[hash_val % len(COLOR_PALETTE)]

def assign_course_colors(courses: List) -> Dict[str, str]:
    """
    Assign consistent colors to each unique course code using the same hash logic as the UI.
    
    Args:
        courses: List of Course objects
        
    Returns:
        Dictionary mapping course_code to hex color code (without #)
    """
    course_colors = {}
    seen_codes = set()
    
    # Collect all unique course codes
    for course in courses:
        if hasattr(course, 'code') and course.code:
            course_code = course.code.strip()
            if course_code and course_code not in seen_codes:
                seen_codes.add(course_code)
    
    # Extract unique semesters from courses for elective baskets
    unique_semesters = sorted(set(c.semester for c in courses if hasattr(c, 'semester')))
    
    for course in courses:
        if course.is_elective and getattr(course, 'elective_group', None):
            basket_code = f"ELECTIVE_BASKET_{course.elective_group}"
            seen_codes.add(basket_code)
            
    for semester in unique_semesters:
        basket_code = f"ELECTIVE_BASKET_{semester}"
        seen_codes.add(basket_code)
        
    for code in sorted(seen_codes):
        course_colors[code] = get_color_for_course_code(code)
    
    return course_colors


def run_phase10(courses: List) -> Dict[str, str]:
    """
    Main entry point for Phase 10.
    
    Args:
        courses: List of all Course objects
        
    Returns:
        Dictionary mapping course_code to hex color code
    """
    print("Phase 10: Course Color Assignment")
    course_colors = assign_course_colors(courses)
    print(f"  Assigned colors to {len(course_colors)} unique courses")
    return course_colors
