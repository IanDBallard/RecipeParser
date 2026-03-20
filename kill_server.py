"""Kill the uvicorn server by PID using Windows API."""
import ctypes
import sys

PROCESS_TERMINATE = 0x0001

def kill_pid(pid):
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        print(f"Could not open process {pid} (already dead?)")
        return False
    result = ctypes.windll.kernel32.TerminateProcess(handle, 0)
    ctypes.windll.kernel32.CloseHandle(handle)
    if result:
        print(f"Successfully killed PID {pid}")
        return True
    else:
        err = ctypes.windll.kernel32.GetLastError()
        print(f"TerminateProcess failed for PID {pid}, error={err}")
        return False

if __name__ == "__main__":
    pids = [int(p) for p in sys.argv[1:]] if len(sys.argv) > 1 else [3268]
    for pid in pids:
        kill_pid(pid)
