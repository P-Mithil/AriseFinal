import urllib.request
import json

req = urllib.request.Request('http://127.0.0.1:8000/api/view-timetable?section=CSE-Sem1')
with urllib.request.urlopen(req) as response:
    data = json.loads(response.read().decode())
    prac = next((s for s in data.get('timetable', []) if s['Session Type'] == 'P'), None)
    lec = next((s for s in data.get('timetable', []) if s['Session Type'] == 'L'), None)
    print("Practical:", prac)
    print("Lecture:", lec)
