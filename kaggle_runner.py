"""
Profitix Ghost - Kaggle 30GB RAM Worker Launcher.
Loads Kaggle Secrets into environment variables and keeps worker.py running with auto-restart.
"""
import asyncio
import logging
import os
import time

log = logging.getLogger("kaggle_runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")

REQUIRED_SECRETS = ("DATABASE_URL", "API_ID", "API_HASH", "BOT_TOKEN")
OPTIONAL_SECRETS = ("WEBAPP_URL", "SUPPORT_USERNAME", "ADMIN_ID")


def load_kaggle_secrets() -> None:
    try:
        from kaggle_secrets import UserSecretsClient
    except ImportError:
        log.info("Not running inside Kaggle environment; using existing os.environ")
        return

    client = UserSecretsClient()
    for key in REQUIRED_SECRETS:
        val = client.get_secret(key)
        if not val:
            raise RuntimeError(f"Missing required Kaggle Secret: {key}")
        os.environ[key] = str(val).strip()

    for key in OPTIONAL_SECRETS:
        try:
            val = client.get_secret(key)
            if val:
                os.environ[key] = str(val).strip()
        except Exception:
            pass


def main() -> None:
    load_kaggle_secrets()
    os.environ["ENABLE_WORKER"] = "true"

    import worker

    while True:
        try:
            log.info("Launching Profitix Ghost 30GB Worker...")
            asyncio.run(worker.run_standalone())
            break
        except KeyboardInterrupt:
            break
        except Exception:
            log.exception("Worker crashed; restarting in 5 seconds...")
            time.sleep(5)


if __name__ == "__main__":
    main()
