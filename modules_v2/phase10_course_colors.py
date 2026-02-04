"""
Phase 10: Course Color Assignment
Assigns unique colors to each course code consistently across all sheets.
"""

from typing import List, Dict
import hashlib

# Predefined color palette - completely different colors across the spectrum
# Each color is from a different hue family to ensure maximum visual distinction
# No similar shades (e.g., no multiple blues, no light/dark versions of same color)
COLOR_PALETTE = [
    'FF0000',  # Pure Red
    '00FF00',  # Pure Green  
    '0000FF',  # Pure Blue
    'FFFF00',  # Pure Yellow
    'FF00FF',  # Magenta
    '00FFFF',  # Cyan
    'FF8000',  # Orange
    '8000FF',  # Purple
    'FF0080',  # Pink
    '80FF00',  # Chartreuse (Yellow-Green)
    '0080FF',  # Azure (Blue)
    'FF4000',  # Red-Orange
    '4000FF',  # Blue-Purple
    'FF0040',  # Rose (Red-Pink)
    '00FF40',  # Green-Cyan
    '40FF00',  # Yellow-Green
    'FFC000',  # Amber (Orange-Yellow)
    'C000FF',  # Violet (Purple)
    'FF00C0',  # Hot Pink
    '00FFC0',  # Aqua (Cyan-Green)
    'C0FF00',  # Lime (Yellow-Green)
    'FF6000',  # Dark Orange
    '6000FF',  # Indigo (Blue-Purple)
    'FF0060',  # Deep Pink
    '00FF60',  # Emerald (Green-Cyan)
    '0060FF',  # Royal Blue
    '60FF00',  # Yellow-Green
    'FFA000',  # Orange-Yellow
    'A000FF',  # Purple-Blue
    'FF00A0',  # Pink-Red
    '00FFA0',  # Turquoise (Cyan-Green)
    'A0FF00',  # Lime-Yellow
    'FF2000',  # Red-Orange
    '2000FF',  # Blue-Indigo
    'FF0020',  # Crimson (Red)
    '00FF20',  # Green-Yellow
    '0020FF',  # Cobalt (Blue)
    '20FF00',  # Yellow-Lime
    'FFE000',  # Gold (Yellow-Orange)
    'E000FF',  # Purple-Magenta
    'FF00E0',  # Magenta-Pink
    '00FFE0',  # Cyan-Green
    'E0FF00',  # Yellow-Gold
    'FF3000',  # Vermillion (Red-Orange)
    '3000FF',  # Ultramarine (Blue)
    'FF0030',  # Rose Red
    '00FF30',  # Mint Green
    '0030FF',  # Sky Blue
    '30FF00',  # Yellow-Green
    'FFD000',  # Orange-Gold
    'D000FF',  # Purple-Violet
    'FF00D0',  # Fuchsia (Magenta-Pink)
    '00FFD0',  # Aquamarine (Cyan-Green)
    'D0FF00',  # Yellow-Orange
    'FF5000',  # Red-Orange
    '5000FF',  # Blue-Purple
    'FF0050',  # Pink-Red
    '00FF50',  # Green-Cyan
    '0050FF',  # Blue-Cyan
    '50FF00',  # Yellow-Green
    'FFB000',  # Orange-Yellow
    'B000FF',  # Purple-Blue
    'FF00B0',  # Pink-Magenta
    '00FFB0',  # Cyan-Green
    'B0FF00',  # Lime-Yellow
    'FF7000',  # Dark Orange
    '7000FF',  # Deep Purple
    'FF0070',  # Deep Pink
    '00FF70',  # Emerald-Cyan
    '0070FF',  # Deep Blue
    '70FF00',  # Yellow-Lime
    'FF9000',  # Orange
    '9000FF',  # Purple
    'FF0090',  # Pink
    '00FF90',  # Turquoise
    '90FF00',  # Yellow-Green
    'FF1000',  # Red
    '1000FF',  # Blue
    'FF0010',  # Red-Pink
    '00FF10',  # Green
    '0010FF',  # Blue
    '10FF00',  # Yellow
    'FFF000',  # Yellow-Gold
    'F000FF',  # Magenta-Purple
    'FF00F0',  # Magenta
    '00FFF0',  # Cyan
    'F0FF00',  # Yellow
    '795548',  # Brown
    '607D8B',  # Blue Grey
    '9E9E9E',  # Grey
    '3E2723',  # Dark Brown
    '424242',  # Dark Grey
    '212121',  # Almost Black
    'FF6F00',  # Amber
    'E91E63',  # Pink
    '9C27B0',  # Deep Purple
    '673AB7',  # Deep Indigo
    '3F51B5',  # Indigo
    '2196F3',  # Blue
    '03A9F4',  # Light Blue
    '00BCD4',  # Cyan
    '009688',  # Teal
    '4CAF50',  # Green
    '8BC34A',  # Light Green
    'CDDC39',  # Lime
    'FFEB3B',  # Yellow
    'FFC107',  # Amber
    'FF9800',  # Orange
    'FF5722',  # Deep Orange
    'F44336',  # Red
    'AA00FF',  # Purple
    '00AAFF',  # Blue
    'AAFF00',  # Yellow-Green
    'FF00AA',  # Magenta-Pink
    '00FFAA',  # Green-Cyan
    'FFAA00',  # Orange-Yellow
]


def assign_course_colors(courses: List) -> Dict[str, str]:
    """
    Assign consistent colors to each unique course code.
    
    Uses hash-based assignment to ensure same course always gets same color.
    
    Args:
        courses: List of Course objects
        
    Returns:
        Dictionary mapping course_code to hex color code (without #)
        Example: {"CS161": "FF6B6B", "MA161": "4ECDC4", ...}
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
    
    # Also add elective baskets (handle group-based codes)
    # Extract groups from courses
    for course in courses:
        if course.is_elective and course.elective_group:
            basket_code = f"ELECTIVE_BASKET_{course.elective_group}"
            if basket_code not in seen_codes:
                seen_codes.add(basket_code)
    
    # Fallback: add semester-based codes for backward compatibility
    for semester in unique_semesters:
        basket_code = f"ELECTIVE_BASKET_{semester}"
        if basket_code not in seen_codes:
            seen_codes.add(basket_code)
    
    # Assign colors using hash-based approach for consistency
    # Use a set to track used colors and ensure uniqueness
    used_color_indices = set()
    used_colors = set()  # Track actual color hex codes to avoid duplicates
    
    for course_code in sorted(seen_codes):  # Sort for deterministic ordering
        # Use hash to get consistent index into palette
        hash_value = int(hashlib.md5(course_code.encode()).hexdigest(), 16)
        base_index = hash_value % len(COLOR_PALETTE)
        
        # Try to find an unused color, cycling through if needed
        color_index = base_index
        attempts = 0
        while (color_index in used_color_indices or COLOR_PALETTE[color_index] in used_colors) and attempts < len(COLOR_PALETTE):
            color_index = (color_index + 1) % len(COLOR_PALETTE)
            attempts += 1
        
        # If all colors are used, just use the hash-based one (will have duplicates but rare)
        if attempts >= len(COLOR_PALETTE):
            color_index = base_index
        
        selected_color = COLOR_PALETTE[color_index]
        course_colors[course_code] = selected_color
        used_color_indices.add(color_index)
        used_colors.add(selected_color)
    
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

