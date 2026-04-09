import json, requests
data = json.load(open('data/multistage_exit/ms_exit_dtss.json'))
r = requests.post('https://web-production-e3025.up.railway.app/api/exit-grind/dtss/upload-multistage', json=data, timeout=60)
print(r.status_code, r.text)
