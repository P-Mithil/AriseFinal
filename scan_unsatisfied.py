import os

import openpyxl


def main() -> None:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(base_dir, "DATA", "OUTPUT")

    # Find the most recent IIITDWD_24_Sheets_v2 file
    latest_path = None
    latest_mtime = -1.0

    for name in os.listdir(output_dir):
        if not name.startswith("IIITDWD_24_Sheets_v2_") or not name.endswith(".xlsx"):
            continue
        full_path = os.path.join(output_dir, name)
        try:
            mtime = os.path.getmtime(full_path)
        except OSError:
            continue
        if mtime > latest_mtime:
            latest_mtime = mtime
            latest_path = full_path

    if not latest_path:
        print("No IIITDWD_24_Sheets_v2_*.xlsx files found.")
        return

    print(f"Scanning workbook: {latest_path}")
    wb = openpyxl.load_workbook(latest_path, data_only=True)

    unsatisfied_rows = []

    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            if not row:
                continue
            if any(isinstance(cell, str) and "UNSATISFIED" in cell for cell in row):
                unsatisfied_rows.append((ws.title, row))

    print(f"Total UNSATISFIED rows: {len(unsatisfied_rows)}")
    for sheet, row in unsatisfied_rows:
        print(f"Sheet: {sheet}  Row: {row}")


if __name__ == "__main__":
    main()

