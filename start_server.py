"""
Launcher: starts uvicorn as a fully detached process and exits immediately.
Run with: python start_server.py
The server will keep running after this script exits.
"""
import subprocess
import sys
import os

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

env = os.environ.copy()
env.update({
    "DISABLE_AUTH": "1",
    "TEST_USER_ID": "e7536d0a-c265-46e7-be58-c432cbd7bab9",
})

log_path = os.path.join(os.path.dirname(__file__), "uvicorn.log")

with open(log_path, "w") as log_file:
    proc = subprocess.Popen(
        [
            r"C:\Python313\python.exe", "-m", "uvicorn",
            "recipeparser.adapters.api:app",
            "--host", "0.0.0.0",
            "--port", "8000",
            "--log-level", "info",
        ],
        cwd=os.path.dirname(__file__),
        env=env,
        stdout=log_file,
        stderr=log_file,
        stdin=subprocess.DEVNULL,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )

print(f"Server started with PID {proc.pid}. Logs: {log_path}")
