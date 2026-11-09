"""Real Docker deployment check; no registry push, no privileged container."""
import argparse
import json
import subprocess
import time
from urllib.request import urlopen
import uuid


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, text=True, capture_output=True, **kwargs).stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="pynext-example:local")
    args = parser.parse_args()
    name = "pynext-smoke-" + uuid.uuid4().hex[:10]
    started = time.monotonic()
    try:
        docker("run", "-d", "--name", name, "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
               "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--pids-limit=128",
               "-p", "127.0.0.1::3000", args.image)
        port = docker("port", name, "3000/tcp").rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while True:
            try:
                with urlopen(base + "/readyz", timeout=2) as response:
                    assert response.read() == b"ready"
                break
            except OSError:
                state = json.loads(docker("inspect", "--format", "{{json .State}}", name))
                if state["Status"] == "exited":
                    raise RuntimeError(f"Container exited before readiness: {state['ExitCode']}")
                if time.monotonic() >= deadline:
                    raise RuntimeError("Container did not become ready within 60s")
                time.sleep(0.1)
        cold_start = time.monotonic() - started
        assert docker("exec", name, "python", "-c", "import os; print(os.geteuid())") == "10001"
        readonly = docker("exec", name, "python", "-c", (
            "import os; "
            "print(bool(os.statvfs('/app').f_flag & os.ST_RDONLY))"))
        assert readonly == "True"
        for path in ("/healthz", "/", "/about", "/api/users", "/styles/site.css"):
            with urlopen(base + path, timeout=5) as response:
                assert response.status == 200
                assert response.read()
        with urlopen(base + "/api/stream", timeout=5) as response:
            content = response.read()
            assert content  # the streaming endpoint finishes without writable app files
        docker("stop", "--time", "40", name, timeout=45)
        state = json.loads(docker("inspect", "--format", "{{json .State}}", name))
        assert state["ExitCode"] == 0, state
        assert not state["OOMKilled"]
        print(json.dumps({"image": args.image, "cold_start_seconds": round(cold_start, 3),
                          "uid": 10001, "read_only": True, "sigterm_exit_code": state["ExitCode"]}))
    except Exception:
        try:
            print(docker("logs", name))
        except subprocess.CalledProcessError:
            pass
        raise
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True)


if __name__ == "__main__":
    main()
