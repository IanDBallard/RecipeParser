"""
Post-build smoke test: verify the Docker image builds successfully.

Run by CI or locally from the RecipeParser directory:

    python tests/smoke_test_docker.py

Validates:
  1. Docker is available
  2. docker build succeeds (timeout: 5 min)

Exit codes:
    0  — build passed
    1  — Docker unavailable, build failed, or timeout
"""
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if shutil.which("docker") is None:
        print("FAIL: Docker not found in PATH", file=sys.stderr)
        return 1

    # Run from project root (parent of tests/)
    project_root = Path(__file__).resolve().parent.parent

    image_name = "recipeparser-smoke-test"
    print(f"\n=== Smoke test: Docker build ({image_name}) ===\n")

    try:
        result = subprocess.run(
            ["docker", "build", "-t", image_name, "."],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            cwd=project_root,
        )
    except subprocess.TimeoutExpired:
        print("FAIL: Docker build timed out (5 min)", file=sys.stderr)
        return 1

    if result.returncode != 0:
        print("FAIL: Docker build failed", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        return 1

    print("  [PASS] Docker image built successfully")
    subprocess.run(["docker", "rmi", image_name], capture_output=True)
    print("\n=== All smoke tests PASSED ===\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
