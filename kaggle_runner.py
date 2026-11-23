"""
Profitix Ghost - Kaggle launcher.

Loads Kaggle Secrets into environment variables and keeps worker.py alive.
worker.py runs in a CHILD PROCESS, so every restart starts from a clean slate (fresh event loop,
no stale Telegram clients) and this script also works inside a notebook cell:

    !python kaggle_runner.py          (or)          %run kaggle_runner.py

Kaggle Secrets (Add-ons -> Secrets): DATABASE_URL, API_ID, API_HASH, BOT_TOKEN
Optional: WEBAPP_URL (expiry message button), SUPPORT_USERNAME, ADMIN_ID, OFFLINE_STATUS_CHECK
Notebook settings: Internet = ON.
"""
import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

REQUIRED_SECRETS = ("DATABASE_URL", "API_ID", "API_HASH", "BOT_TOKEN")
OPTIONAL_SECRETS = ("WEBAPP_URL", "SUPPORT_USERNAME", "ADMIN_ID", "OFFLINE_STATUS_CHECK")
PIP_DEPS = {
    "telethon": "telethon>=1.36,<2.0",
    "asyncpg": "asyncpg>=0.29,<1.0",
    "httpx": "httpx>=0.27,<1.0",
}
MIN_BACKOFF = 5
MAX_BACKOFF = 120
STABLE_AFTER = 300  # seconds of uptime after which the backoff resets

_stopping = False
_child: Optional[subprocess.Popen] = None


def log(message: str) -> None:
    print(f"[kaggle_runner {time.strftime('%H:%M:%S')}] {message}", flush=True)


def load_secrets() -> None:
    client = None
    try:
        from kaggle_secrets import UserSecretsClient  # type: ignore

        client = UserSecretsClient()
    except Exception:
        log("kaggle_secrets not available, using existing environment variables")
    if client is not None:
        for name in REQUIRED_SECRETS + OPTIONAL_SECRETS:
            try:
                value = client.get_secret(name)
            except Exception:
                continue
            if value:
                os.environ[name] = str(value).strip()
    missing = [n for n in REQUIRED_SECRETS if not os.environ.get(n)]
    if missing:
        raise SystemExit("Missing required secrets: " + ", ".join(missing))
    log("Secrets loaded: " + ", ".join(n for n in REQUIRED_SECRETS + OPTIONAL_SECRETS if os.environ.get(n)))


def ensure_dependencies() -> None:
    missing = [spec for mod, spec in PIP_DEPS.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    log("Installing: " + ", ".join(missing))
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *missing])
    except subprocess.CalledProcessError:
        raise SystemExit("pip install failed. Turn Internet ON in the notebook settings and retry.")


def find_project_dir() -> Path:
    here = Path(globals().get("__file__", ".")).resolve().parent
    candidates: List[Path] = []
    if os.environ.get("PROJECT_DIR"):
        candidates.append(Path(os.environ["PROJECT_DIR"]))
    candidates += [here, Path.cwd(), Path("/kaggle/working/profitix-ghost"), Path("/kaggle/working")]
    kaggle_input = Path("/kaggle/input")
    if kaggle_input.exists():
        candidates += [p.parent for p in kaggle_input.glob("*/worker.py")]
        candidates += [p.parent for p in kaggle_input.glob("*/*/worker.py")]
    for folder in candidates:
        if all((folder / name).is_file() for name in ("worker.py", "config.py", "database.py")):
            return folder
    raise SystemExit("worker.py / config.py / database.py not found. Upload the project or set PROJECT_DIR.")


def _handle_stop(signum, frame) -> None:  # noqa: ARG001
    global _stopping
    _stopping = True
    if _child is not None and _child.poll() is None:
        _child.terminate()


def _sleep_interruptible(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _stopping and time.monotonic() < end:
        time.sleep(0.5)


def run_forever(project: Path) -> None:
    global _child
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    backoff = MIN_BACKOFF
    attempt = 0
    while not _stopping:
        attempt += 1
        started = time.monotonic()
        log(f"Starting worker.py (run #{attempt})")
        _child = subprocess.Popen([sys.executable, "-u", "worker.py"], cwd=str(project), env=env)
        try:
            code = _child.wait()
        except KeyboardInterrupt:
            _handle_stop(signal.SIGINT, None)
            code = _child.wait()
        _child = None
        if _stopping:
            break
        uptime = time.monotonic() - started
        if uptime >= STABLE_AFTER:
            backoff = MIN_BACKOFF
        log(f"worker.py exited with code {code} after {uptime:.0f}s, restarting in {backoff}s (network blip or crash)")
        _sleep_interruptible(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)
    log("Stopped")


def main() -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_stop)
        except ValueError:  # not in the main thread (some notebook setups)
            pass
    load_secrets()
    ensure_dependencies()
    project = find_project_dir()
    log(f"Project folder: {project}")
    try:
        run_forever(project)
    except KeyboardInterrupt:
        _handle_stop(signal.SIGINT, None)


if __name__ == "__main__":
    main()
