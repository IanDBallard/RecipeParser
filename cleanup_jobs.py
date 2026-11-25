"""Mark every ingestion job still pending or running as an error.

Run by hand after a restart left jobs orphaned. A refused GET fails the run;
a refused update is reported per job and makes the script exit 1, so it never
claims a job was updated when Supabase said no.
"""
import os
import sys

import httpx

ERROR_MESSAGE = 'System restart interrupted the job. Please try again.'


def cleanup(supabase_url: str, service_key: str) -> int:
    """End the orphaned jobs; return the number Supabase refused to update."""
    headers = {
        'apikey': service_key,
        'Authorization': f'Bearer {service_key}',
        'Content-Type': 'application/json'
    }

    # 1. Fetch orphaned jobs
    url = f"{supabase_url}/rest/v1/ingestion_jobs?status=in.(pending,running)"
    resp = httpx.get(url, headers=headers)
    resp.raise_for_status()
    jobs = resp.json()

    if not jobs:
        print("No orphaned jobs found.")
        return 0

    print(f"Found {len(jobs)} orphaned jobs. Marking as error...")

    refused = 0
    for job in jobs:
        job_id = job['id']
        patch_url = f"{supabase_url}/rest/v1/ingestion_jobs?id=eq.{job_id}"
        payload = {
            'status': 'error',
            'error_message': ERROR_MESSAGE,
            'updated_at': 'now()'
        }
        try:
            httpx.patch(patch_url, headers=headers, json=payload).raise_for_status()
        except httpx.HTTPStatusError as exc:
            refused += 1
            print(f"  Job {job_id} NOT updated: HTTP {exc.response.status_code} {exc.response.text[:200]}")
            continue
        print(f"  Job {job_id} updated to error.")

    if refused:
        print(f"Cleanup finished with {refused} job(s) not updated.")
    else:
        print("Cleanup complete.")
    return refused


def main() -> int:
    # Get credentials from environment
    supabase_url = os.environ.get('SUPABASE_URL', '').rstrip('/')
    service_key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')

    if not supabase_url or not service_key:
        print("Error: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not found in environment.")
        return 1
    return 1 if cleanup(supabase_url, service_key) else 0


if __name__ == '__main__':
    sys.exit(main())
