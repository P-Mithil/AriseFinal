import urllib.request
import json

req = urllib.request.Request('http://127.0.0.1:8000/api/view-timetable?section=DSAI-A-Sem1')
with urllib.request.urlopen(req) as response:
    data = json.loads(response.read().decode())
    ds161_sessions = [s for s in data.get('timetable', []) if s['Course Code'] == 'DS161']
    for s in ds161_sessions:
        print(f"{s['Session Type']}: {s['Start Time']}-{s['End Time']} ({s['Day']})")
