import shutil
import subprocess


def docker_available() -> bool:
    """Integration tests need a daemon; Claude Code web sessions lack one."""
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0
