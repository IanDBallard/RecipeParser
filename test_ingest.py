import urllib.request
import json
import sys

url = "http://127.0.0.1:8000/ingest/url"
token = "eyJhbGciOiJFUzI1NiIsImtpZCI6IjAyYWMxY2Y0LTk5ZDEtNDQ1Yy05MWI5LTE4NDI0ZjI2ZTcyMSIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJodHRwczovL2h1a2hoaWxtb3BrbWdncXdydHNkLnN1cGFiYXNlLmNvL2F1dGgvdjEiLCJzdWIiOiJlNzUzNmQwYS1jMjY1LTQ2ZTctYmU1OC1jNDMyY2JkN2JhYjkiLCJhdWQiOiJhdXRoZW50aWNhdGVkIiwiZXhwIjoxNzczODY0NzUwLCJpYXQiOjE3NzM4NjExNTAsImVtYWlsIjoiaWFuLmQuYmFsbGFyZEBnbWFpbC5jb20iLCJwaG9uZSI6IiIsImFwcF9tZXRhZGF0YSI6eyJwcm92aWRlciI6ImVtYWlsIiwicHJvdmlkZXJzIjpbImVtYWlsIl19LCJ1c2VyX21ldGFkYXRhIjp7ImVtYWlsX3ZlcmlmaWVkIjp0cnVlfSwicm9sZSI6ImF1dGhlbnRpY2F0ZWQiLCJhYWwiOiJhYWwxIiwiYW1yIjpbeyJtZXRob2QiOiJwYXNzd29yZCIsInRpbWVzdGFtcCI6MTc3Mzg2MTE1MH1dLCJzZXNzaW9uX2lkIjoiMDNlMjUwMDctMzdhMy00ODFlLTk2NDMtNWZiZTBmYmI2YTY0IiwiaXNfYW5vbnltb3VzIjpmYWxzZX0.cBKCRD_bF9uSho6FSFVKcMwQ5GK526-pEdx4sgT-wGa49bTnWDz1OHN6-c9K46_nDzgTYNMK4aZIQ3Q0AWdTxQ"
recipe_url = "https://www.allrecipes.com/recipe/10813/best-chocolate-chip-cookies/"

data = json.dumps({"url": recipe_url}).encode("utf-8")
req = urllib.request.Request(url, data=data, method="POST")
req.add_header("Content-Type", "application/json")
req.add_header("Authorization", f"Bearer {token}")

print(f"Connecting to {url}...")
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        print(f"Response Status: {resp.status}")
        print(resp.read().decode("utf-8"))
except Exception as e:
    print(f"Error: {e}")
    sys.exit(1)
