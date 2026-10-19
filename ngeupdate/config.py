"""Process-level config and shared tunables.

`load_config` is the single env-reading function (no CLI flags so the
key never leaks via `ps`). `MAX_BODY_BYTES` / `SOCKET_TIMEOUT` /
`MIN_KEY_LEN` are tunables used by the HTTP layer and the loader.

`STEP_ENV` is the env passed to every child subprocess (`git`, `docker
compose`): bearer key stripped, git credential prompt disabled. Used by
`deploy` and `health`.
"""
import os
import sys
import types


MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB
MIN_KEY_LEN = 32  # `openssl rand -hex 32` gives 64
SOCKET_TIMEOUT = 30  # seconds a client may stall on any single read/write (slowloris)
TAIL_CHARS = 4000  # per-step stdout+stderr tail


# No stdin + no git prompts: a credential/host-key prompt fails fast instead
# of hanging until DEPLOY_TIMEOUT. The bearer key is dropped so git / compose
# (and compose's ${VAR} interpolation) never see it.
STEP_ENV = {k: v for k, v in os.environ.items() if k != "NGEUPDATE_KEY"}
STEP_ENV["GIT_TERMINAL_PROMPT"] = "0"


def load_config(env=None):
    """Read config from env only (no CLI flags: a key in argv leaks via `ps`).
    Server refuses to start without a key."""
    env = os.environ if env is None else env
    cfg = types.SimpleNamespace(
        host=env.get("HOST", "127.0.0.1"),
        port=int(env.get("PORT", "8787")),
        key=env.get("NGEUPDATE_KEY", ""),
        allowed_root=env.get("ALLOWED_ROOT", ""),
        deploy_timeout=int(env.get("DEPLOY_TIMEOUT", "1800")),
        max_deploys=int(env.get("MAX_CONCURRENT_DEPLOYS", "2")),
        health_timeout=int(env.get("HEALTH_TIMEOUT", "90")),
    )

    if not cfg.key:
        sys.stderr.write("NGEUPDATE_KEY is required; refusing to start\n")
        sys.exit(2)
    if len(cfg.key) < MIN_KEY_LEN:
        sys.stderr.write(
            "NGEUPDATE_KEY too short (< %d chars); use `openssl rand -hex 32`\n" % MIN_KEY_LEN
        )
        sys.exit(2)
    if not cfg.allowed_root:
        sys.stderr.write("ALLOWED_ROOT is required; refusing to start\n")
        sys.exit(2)

    cfg.allowed_root = os.path.realpath(cfg.allowed_root)
    if not os.path.isdir(cfg.allowed_root):
        sys.stderr.write("ALLOWED_ROOT is not a directory: %s\n" % cfg.allowed_root)
        sys.exit(2)

    return cfg