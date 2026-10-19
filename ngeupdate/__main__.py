"""Entry point: `python3 -m ngeupdate` starts the server.

Reads env via `load_config`, wires the shared state onto the handler
class (`config`, `deploy_lock`, `job_store`), and serves until SIGINT.
"""
import sys

from .config import load_config
from .handler import NgeupdateHandler, ThreadingHTTPServer
from .jobs import DeployLock, JobStore


def main():
    config = load_config()
    handler = NgeupdateHandler
    handler.config = config
    handler.deploy_lock = DeployLock(config.max_deploys)
    handler.job_store = JobStore()
    server = ThreadingHTTPServer((config.host, config.port), handler)
    sys.stderr.write(
        "ngeupdate listening on http://%s:%d (root=%s, timeout=%ds, health=%ds)\n"
        % (
            config.host, config.port, config.allowed_root,
            config.deploy_timeout, config.health_timeout,
        )
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("shutting down\n")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()