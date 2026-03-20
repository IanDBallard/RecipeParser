import httpx
import os
import sys

# Get credentials from environment
supabase_url = os.environ.get('SUPABASE_URL', '').rstrip('/')
service_key = os.environ.get('SUPABASE_SERVICE_KEY', '')

if not supabase_url or not service_key:
    print("Error: SUPABASE_URL or SUPABASE_SERVICE_KEY not found in environment.")
    sys.exit(1)

headers = {
    'apikey': service_key,
    'Authorization': f'Bearer {service_key}',
    'Content-Type': 'application/json'
}

# 1. Fetch orphaned jobs
url = f"{supabase_url}/rest/v1/ingestion_jobs?status=in.(pending,running)"
resp = httpx.get(url, headers=headers)
jobs = resp.json()

if not jobs:
    print("No orphaned jobs found.")
    sys.exit(0)

print(f"Found {len(jobs)} orphaned jobs. Marking as error...")

for job in jobs:
    job_id = job['id']
    patch_url = f"{supabase_url}/rest/v1/ingestion_jobs?id=eq.{job_id}"
    payload = {
        'status': 'error',
        'error_message': 'System restart interrupted the job. Please try again.',
        'updated_at': 'now()'
    }
    httpx.patch(patch_url, headers=headers, json=payload)
    print(f"  Job {job_id} updated to error.")

print("Cleanup complete.")
