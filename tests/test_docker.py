import subprocess
import pytest
import shutil

@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker not installed")
def test_docker_build():
    """
    Verify that the Docker image builds successfully.
    """
    image_name = "recipeparser-test"
    
    # Attempt to build the image
    result = subprocess.run(
        ["docker", "build", "-t", image_name, "."],
        capture_output=True,
        text=True
    )
    
    # Check if build was successful
    assert result.returncode == 0, f"Docker build failed: {result.stderr}"
    
    # Clean up: remove the test image
    subprocess.run(["docker", "rmi", image_name], capture_output=True)

if __name__ == "__main__":
    # Allow running the test directly
    test_docker_build()
