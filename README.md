# ngeupdate

Remote MCP server for production deploys. Stateless. Python standard library only — no `pip install`, no Node, no Go.

Client talks to it through MCP Streamable HTTP at `POST /mcp`. There are two tools:

- `deploy` — validates synchronously, then kicks the pipeline off in a background thread and returns **immediately** with a job id. Even a `--no-cache` build that takes 20 minutes never blocks the MCP call (Cloudflare's free tier kills any request over 100 s with a 524 — this design sidesteps that entirely).
- `deploy_status({job_id})` — long-polls up to 25 s. Returns the final `✅ DONE` / `❌ FAILED` output if the deploy finished in that window, otherwise a `⏳ RUNNING` line with the current step and elapsed time. Loop until you get the final text.

The pipeline on the VPS runs:

```
git pull --ff-only
docker compose build [--no-cache]            # clean mode only adds --no-cache
docker compose run --rm -T <service> <migrate argv>   # if migrate=true
docker compose run --rm -T <service> <seed argv>      # if seed=true
docker compose up -d [--force-recreate]      # clean mode only adds --force-recreate
```

…stopping at the first failing step, then polling `docker compose ps -a` until every service is healthy twice in a row (or `HEALTH_TIMEOUT` elapses).

## Layout

| File | Purpose |
| --- | --- |
| `server.py` | The MCP server (stdlib only). |
| `test_server.py` | Unit + HTTP smoke tests, run with `python3 -m unittest`. |
| `Dockerfile`, `docker-compose.yaml` | Option A: container behind Traefik. |
| `.env.example` | uid/gid values for `docker-compose.yaml` (copy to `.env`). |
| `ngeupdate.service` | Option B: systemd unit (`/etc/systemd/system/`). |
| `ngeupdate.env.example` | Server config (copy to `ngeupdate.env` or `/etc/ngeupdate.env`, chmod 600). |

Config is env-only (no CLI flags, so the key never shows in `ps`): `NGEUPDATE_KEY`, `ALLOWED_ROOT`, `HOST` (default `127.0.0.1`), `PORT` (default `8787`), `DEPLOY_TIMEOUT` (seconds per step, default `1800`), `HEALTH_TIMEOUT` (seconds to wait for services after `up -d`, default `90`), `MAX_CONCURRENT_DEPLOYS` (default `2`).

## Deploy to the VPS

Prerequisite for both options: a host user `deploy` that is in the `docker` group and can `git pull` every repo under `/srv/apps` (SSH deploy key in `/home/deploy/.ssh`, `known_hosts` already populated).

### Option A: Docker + Traefik (recommended)

```bash
# 1. Put this folder OUTSIDE ALLOWED_ROOT (see notes)
git clone <this repo> /opt/ngeupdate   # or scp the folder
cd /opt/ngeupdate

# 2. Server config
cp ngeupdate.env.example ngeupdate.env
sed -i "s/^NGEUPDATE_KEY=.*/NGEUPDATE_KEY=$(openssl rand -hex 32)/" ngeupdate.env
chmod 600 ngeupdate.env

# 3. uid/gid for the container
cp .env.example .env
id deploy                                 # -> DEPLOY_UID / DEPLOY_GID
getent group docker | cut -d: -f3         # -> DOCKER_GID
vi .env

# 4. Edit Traefik labels in docker-compose.yaml: Host(`...`), entrypoints,
#    certresolver and the external network name must match your Traefik setup.

# 5. Start
docker compose up -d --build
docker compose logs -f

# 6. Sanity checks
docker compose exec ngeupdate docker compose version
docker compose exec ngeupdate git -C /srv/apps/myapp pull     # must work without prompts
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://deploy.example.com/mcp   # 401
```

Upgrade later: `git pull && docker compose up -d --build` in `/opt/ngeupdate` (run it by hand, not through the tool).

Why the compose file looks the way it does:

- **Same path inside and out (`/srv/apps:/srv/apps`).** `docker compose` sends bind-mount paths to the host daemon. If the container path differs, your apps' volumes end up mounted from the wrong place.
- **Runs as the `deploy` uid, not root.** A root `git pull` leaves root-owned files in the repo and trips git's "dubious ownership" check. `/etc/passwd` and `/etc/group` are mounted because ssh refuses to run for a uid with no passwd entry.
- **docker.sock is root on the host.** That's the cost of running compose from a container. The bearer key and `ALLOWED_ROOT` are what guard it.
- **Keep ngeupdate outside `ALLOWED_ROOT`.** If ngeupdate deploys itself, `up -d --force-recreate` kills the container that's serving the request.
- **No published port.** It's only reachable through Traefik (HTTPS). Traefik has no backend response timeout by default — and even if there were one, `deploy` returns a job id in milliseconds. The actual long-running work happens in a background thread that the client follows via `deploy_status`. So Cloudflare's 100 s 524 cap is a non-issue, even with the record proxied (orange cloud).
- **Long polls also stay under 100 s.** `deploy_status` long-polls for at most 25 s before returning `⏳ RUNNING`. The agent simply calls it again.

### Option B: systemd on the host (needs `python3` on the host)

```bash
scp server.py ngeupdate.service ngeupdate.env.example user@vps:/tmp/

# on the VPS
sudo mkdir -p /opt/ngeupdate
sudo mv /tmp/server.py /opt/ngeupdate/
sudo mv /tmp/ngeupdate.service /etc/systemd/system/
sudo mv /tmp/ngeupdate.env.example /etc/ngeupdate.env
sudo sed -i "s/^NGEUPDATE_KEY=.*/NGEUPDATE_KEY=$(openssl rand -hex 32)/" /etc/ngeupdate.env
sudo chmod 600 /etc/ngeupdate.env

sudo systemctl daemon-reload
sudo systemctl enable --now ngeupdate
journalctl -u ngeupdate -f

curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8787/mcp   # 401
```

The service listens on `127.0.0.1:8787`. To expose it through Traefik, add a file-provider route to `http://<host-ip>:8787` (and set `HOST` so Traefik can reach it), or just use Option A.

Upgrade later: `scp` the new `server.py`, then `sudo systemctl restart ngeupdate`.

## How the team uses it

1. Admin hands out the bearer key over a secure channel (password manager). Never paste it in chat.
2. Each teammate sets it once, e.g. in `~/.zshrc`:

   ```sh
   export NGEUPDATE_KEY=...
   ```

3. At the root of each project, add (or merge) `.mcp.json`:

   ```json
   {
     "mcpServers": {
       "ngeupdate": {
         "type": "http",
         "url": "https://deploy.example.com/mcp",
         "headers": {
           "Authorization": "Bearer ${NGEUPDATE_KEY}",
           "X-Project-Path": "/srv/apps/myapp"
         }
       }
     }
   }
   ```

   Safe to commit — the key stays in each teammate's env.

4. Open Claude Code in that repo. Approve the `ngeupdate` MCP server once. Verify with `/mcp` (status should be `connected`).

5. Just talk:

   | You say | Mode |
   | --- | --- |
   | "pull build production" | `fast` |
   | "pull build production no cache" / "force recreate" | `clean` |
   | "deploy production path /srv/apps/other" | `fast`, different path (overrides header) |

   Claude calls `deploy` and gets a job id back in milliseconds, then `deploy_status(job_id=...)` in a tight loop until the final `✅ DONE` or `❌ FAILED`. That's the line you show to the user, plus any `⚠️` / `ℹ️` notice lines.

   Example conversation while a `--no-cache` build runs:

   ```
   > claude: I'll run a clean rebuild now. (calls deploy)
     MCP: 🚀 STARTED job 9f2a08 — /srv/apps/myapp (clean). Panggil
          deploy_status(job_id="9f2a08") sampai DONE/FAILED.
   > claude: (polls deploy_status)
     MCP: ⏳ RUNNING job 9f2a08 — step 2/4: docker compose build --no-cache (47s)
   > claude: (polls deploy_status)
     MCP: ⏳ RUNNING job 9f2a08 — step 2/4: docker compose build --no-cache (1m32s)
   > claude: (polls deploy_status)
     MCP: ⏳ RUNNING job 9f2a08 — step 3/4: docker compose up -d --force-recreate (8s)
   > claude: (polls deploy_status)
     MCP: ✅ DONE — /srv/apps/myapp deployed (clean) in 2m14s
          step 1: git pull --ff-only      [ok, 0.4s]
          step 2: docker compose build --no-cache [ok, 1m38s]
          step 3: docker compose up -d --force-recreate [ok, 8s]
          step 4: healthcheck             [ok: 2x consecutive all-services-healthy polls, 22s]
   ```

### Overriding the path

Path resolution order:

1. The `path` argument to the tool (absolute or relative to `ALLOWED_ROOT`).
2. The `X-Project-Path` header from `.mcp.json`.
3. Error: `"path wajib"`.

Relative paths are joined to `ALLOWED_ROOT` and must contain a recognised compose file (`compose.yaml` / `compose.yml` / `docker-compose.{yml,yaml}`). Paths outside the root, `..` traversal, and symlink escapes are all rejected.

## Migrate & seed (on demand)

The deploy tool can also run migrations and seeders between `docker compose build` and `docker compose up`. Both flags are **opt-in per call** — the project has to opt in via headers in `.mcp.json`, AND the agent (or you) has to ask for them in the deploy call. Without a flag, the deploy is identical to the old behaviour.

Five optional headers in `.mcp.json` per project (plus `X-Project-Path` from before):

| Header | Required when | What it does |
| --- | --- | --- |
| `X-Migrate-Cmd` | `migrate=true` | argv list run via `docker compose run --rm -T <service>` between build and up. Split with `shlex.split`, so quote args like normal. |
| `X-Seed-Cmd` | `seed=true` | same, but for seeders. |
| `X-Compose-Service` | `migrate=true` or `seed=true` | the compose service to run the command in. Must match `[A-Za-z0-9][A-Za-z0-9_.-]*` (no leading `-`, so `--privileged` is rejected). |
| `X-Migrate-Paths` | informational | comma-separated directory prefixes (e.g. `database/migrations`). Used to detect *whether* the pull brought migrations; never executed. |
| `X-Seed-Paths` | informational | same, for seeders (e.g. `database/seeders`). |

Validation runs **before `git pull`**: if a flag is requested but the matching header is missing / unparseable / has an unclosed quote, the call errors immediately and nothing runs.

### Example: Laravel

```json
{
  "mcpServers": {
    "ngeupdate": {
      "type": "http",
      "url": "https://deploy.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${NGEUPDATE_KEY}",
        "X-Project-Path": "/srv/apps/myapp",
        "X-Compose-Service": "app",
        "X-Migrate-Cmd": "php artisan migrate --force",
        "X-Migrate-Paths": "database/migrations",
        "X-Seed-Cmd": "php artisan db:seed --force",
        "X-Seed-Paths": "database/seeders"
      }
    }
  }
}
```

Then ask the agent — or call yourself:

| You say | Flags |
| --- | --- |
| "deploy production" / "pull build production" | — |
| "deploy + migrate" / "pull + migrate" / "apply migration" / "db migrate" | `migrate=true` |
| "deploy + seed" / "pull + seed" / "db seed" | `seed=true` |
| "deploy + migrate + seed" | `migrate=true, seed=true` |

### Example: Drizzle

```json
{
  "headers": {
    "X-Compose-Service": "app",
    "X-Migrate-Cmd": "npx drizzle-kit migrate",
    "X-Migrate-Paths": "drizzle",
    "X-Seed-Cmd": "node dist/seed.js",
    "X-Seed-Paths": "src/db/seed"
  }
}
```

Note: `drizzle-kit` must be present in the production image (install with `--production=false` or include it in a separate `migrate` stage).

### Example: Go + bun (uptrace/bun)

Migrations usually go through the app binary itself. Expose a `db migrate` / `db seed` sub-command and embed the SQL via `//go:embed`:

```json
{
  "headers": {
    "X-Compose-Service": "api",
    "X-Migrate-Cmd": "/app/server db migrate",
    "X-Migrate-Paths": "cmd/migrations",
    "X-Seed-Cmd": "/app/server db seed",
    "X-Seed-Paths": "internal/db/seeds"
  }
}
```

Notes:

- `cmd/migrations` must be `//go:embed`-ed into the binary, or copied to `/app/migrations` in the image, so the command can find the `.sql` files at runtime.
- Use `migrate.NewMigrator` from `github.com/uptrace/bun` (or your tool of choice) and register both `migrate` and `seed` as cobra sub-commands on the same binary.

### Notice output

Even when `migrate=true` / `seed=true` is **not** set, the deploy output attaches a notice so any agent sees whether the pull moved migration / seeder files:

- `⚠️ Pull ini membawa perubahan di database/migrations (3 file) tapi migrate TIDAK dijalankan. Kalau perlu: deploy ulang dengan migrate=true.` — pull moved migration files but the agent didn't ask for a migration. Re-deploy with `migrate=true` in mode `fast` (pull is a no-op, build hits the cache, only the migrate step actually runs).
- `ℹ️ Project ini support migrate/seed; tidak dijalankan.` — project is configured for migrate / seed, but this pull didn't touch those paths. No action needed; just FYI.

These lines appear after the per-step output whether the pipeline succeeded or failed. No notice is printed for a flag you **did** pass (`migrate=true` / `seed=true`). The step list already shows whether it ran, so you won't get a misleading "TIDAK dijalankan" after a seed that actually ran. If `git pull` itself failed (no network, dirty tree), the diff is `[]` and only the ℹ️ line prints.

### Snippet for `AGENTS.md` / `CLAUDE.md` / `GEMINI.md`

Drop this into the project's agent file so any agent picks up the convention without needing to be told:

````markdown
## deploy

`deploy` runs `git pull --ff-only` → `docker compose build` → optional
migrate → optional seed → `docker compose up -d` → healthcheck.

The call is **asynchronous**: `deploy` returns a job id in milliseconds.
Immediately call `deploy_status(job_id=...)` in a loop until you get
the final `✅ DONE ...` or `❌ FAILED ...` (or `job tidak ditemukan ...`).

Only pass `migrate=true` or `seed=true` when the user asked for it
("deploy + migrate", "db seed", "apply migration", …). Both flags need
matching headers in `.mcp.json` (`X-Migrate-Cmd` / `X-Seed-Cmd` and
`X-Compose-Service`); otherwise the call errors out before `git pull`.

Always show the final deploy-status output's first line (`✅ DONE ...`
or `❌ FAILED ...`) plus any `⚠️` / `ℹ️` notice lines that follow.
````

## Security model (short version)

- `Authorization: Bearer <key>` is checked with `hmac.compare_digest` before the body is parsed. Wrong / missing key → `401`.
- The server refuses to start without `NGEUPDATE_KEY`.
- `subprocess.run([...], shell=False, cwd=project_path, timeout=...)` — tool arguments are only ever the cwd. The only header-supplied argv is `X-Migrate-Cmd` / `X-Seed-Cmd`, and it runs inside the app container (see below).
- `NGEUPDATE_KEY` is stripped from the env of every child process (git, docker compose).
- `migrate` / `seed` must be real JSON booleans; `"false"` as a string is rejected.
- Per-path lock rejects concurrent deploys to the same project.
- Steps run with no stdin and `GIT_TERMINAL_PROMPT=0`, so a credential prompt fails fast instead of hanging until the timeout.
- Body size capped at 1 MiB.
- `Origin` header present → `403` (MCP spec, against DNS rebinding). CLI/IDE clients don't send it; browsers always do.
- The server refuses a key shorter than 32 chars.
- 30s socket timeout per read/write, so a slowloris client trickling headers gets dropped instead of pinning a thread.
- At most `MAX_CONCURRENT_DEPLOYS` (default 2) deploys at once across all projects, plus one per path, so parallel builds can't starve the box.
- Binds `127.0.0.1` by default (`0.0.0.0` inside the container, with no published port). Only Traefik/HTTPS exposes it.

### Anti-DDoS / brute force (Traefik, Option A)

`docker-compose.yaml` attaches four Traefik middlewares to the router, in this order:

| Middleware | Setting | Why |
| --- | --- | --- |
| `ngeupdate-cf` | `ipallowlist` on Cloudflare's IPv4 + IPv6 ranges | Origin lock: anyone hitting the VPS IP directly (bypassing Cloudflare) gets 403 at the edge. See the "Cloudflare" section below. |
| `ratelimit` | 30 req/min per IP, burst 20 (keyed on `CF-Connecting-IP`) | Floods and key guessing die at the edge. One MCP connect is ~4 requests, so the team never hits this. |
| `inflightreq` | 10 per IP (keyed on `CF-Connecting-IP`) | Caps open connections from a single client. |
| `buffering` | body ≤ 1 MiB | Big bodies get rejected before they reach Python. |

**Strongest option:** if the team has a fixed office IP or a VPN, turn on the commented `ipallowlist` middleware. Then the endpoint doesn't exist at all for the rest of the internet.

The rate limit + inflight keys on `CF-Connecting-IP` (not the raw TCP source), so the limits apply per real client. That's safe because the `ngeupdate-cf` middleware already enforces that only Cloudflare can reach the router — the header itself can't be forged past the lock. A real volumetric (network-level) DDoS can't be stopped by the app or by Traefik. That takes your VPS provider's DDoS protection.

The container runs `read_only`, with `cap_drop: ALL`, `no-new-privileges`, and limits on pids and memory. The systemd unit uses the equivalent `Protect*` / `MemoryMax` settings.

### Cloudflare (proxied, origin-locked)

The `deploy.example.com` DNS record is proxied (orange cloud), not DNS-only. Three things go together:

1. **Record is proxied.** Long-running deploys no longer trip Cloudflare's 100-second hard cap on the free plan: `deploy` returns a job id in milliseconds and the real work happens in a background thread; `deploy_status` long-polls for at most 25 seconds and the agent loops. Nothing sits in front of Cloudflare's edge for more than 25 s.

2. **Origin is locked to Cloudflare.** The `ngeupdate-cf` Traefik middleware has `ipallowlist.sourcerange` set to every IPv4 + IPv6 range from [cloudflare.com/ips](https://www.cloudflare.com/ips/). Anything reaching the VPS IP directly — bypassing the proxy — gets `403 Forbidden` at Traefik. Verify with:

   ```bash
   curl --resolve deploy.example.com:443:<VPS_IP> \
        --connect-to deploy.example.com:443:<VPS_IP> \
        https://deploy.example.com/mcp   # → 403
   ```

   (The `--resolve` / `--connect-to` pair forces the connection directly to the origin even if the record is proxied.)

   The range list comes from the canonical JSON in the link above. **Re-check it once a year**, because Cloudflare occasionally adds a new range. Updating one Traefik label is far cheaper than a leaked origin.

   About firewalls: `ufw` does NOT protect ports that Docker publishes, because the packets hit `iptables` via the `DOCKER` chain, not the `ufw-user` chain. The `ngeupdate-cf` middleware gives you origin lock at the application layer; for host-level lock, filter Traefik's published port (443, not 8787, which is never published) in the `DOCKER-USER` chain with an ipset of the CF ranges:

   ```bash
   ipset create cf hash:net
   for r in $(curl -s https://www.cloudflare.com/ips-v4); do ipset add cf $r; done
   iptables -I DOCKER-USER -i eth0 -p tcp --dport 443 -m set ! --match-set cf src -j DROP
   ```

   That applies to **every** app behind this Traefik, so only do it if all of them are proxied through Cloudflare. IPv6 needs the same thing with `ip6tables` and a `hash:net family inet6` set.

   **IPv6 gotcha:** if Traefik's ports are published by Docker with the default `userland-proxy`, IPv6 clients (and Cloudflare connects over IPv6 when the origin has an AAAA record) show up with the Docker gateway IP. `ngeupdate-cf` then answers 403 to real Cloudflare traffic. Fix it with `network_mode: host` for Traefik, by disabling `userland-proxy`, or by dropping the AAAA record on the origin.

3. **Rate limit keyed on the real client IP.** The `ratelimit` and `inflightreq` middlewares use `requestheadername=CF-Connecting-IP`, so a single bad client is capped at 30 req/min even if all of them arrive from Cloudflare's IP. This is safe precisely because step 2 has already proven that only Cloudflare is reaching us — an attacker forging `CF-Connecting-IP` from a non-Cloudflare IP never gets past `ngeupdate-cf`.

**Optional next layer:** put a Cloudflare Access service token in `.mcp.json` (`Cf-Access-Client-Id` / `Cf-Access-Client-Secret` headers) and require an Access policy on the `deploy.` zone. Anyone without a Cloudflare identity gets bounced by Access before they reach Traefik. Useful if the team uses Cloudflare Tunnel too.

### Migrate / seed: expanded trust

The bearer key now also lets its holder run **any** command declared in `X-Migrate-Cmd` / `X-Seed-Cmd` of any project that opted in. The command runs **inside the project's container**, not on the host, and only when an agent (or the user) actively asked for `migrate=true` / `seed=true` on that specific call. The header is parsed with `shlex.split`, never through a shell, and the compose service name must start with a letter/digit so it can't be smuggled in as a `docker compose run` option (e.g. `--privileged`). The blast radius is still bounded by `ALLOWED_ROOT` and `NGEUPDATE_KEY`, but a leaked key is now strictly more dangerous than before — treat it accordingly.

### Data safety during deploy

- **For several seconds, the old code runs against the new schema.** Migrate runs *after* `docker compose build` and *before* `docker compose up -d`, so the existing containers serve traffic on the old image while the new image is being migrated against. Roll out destructive migrations (column drops, renames) as **expand → deploy → contract**: add the new column first, deploy so both old and new code see it, then drop the old one in a later release.
- **No automatic rollback.** If the migrate / seed step fails, the pipeline stops and the previous containers keep running on the old image. Roll back manually (`git revert` → re-deploy, or restore from DB backup).
- **Make seeders idempotent.** Use `updateOrCreate` / `onConflictDoNothing` / `INSERT ... ON CONFLICT DO NOTHING` so re-running `seed=true` against the same release doesn't duplicate rows.
- **`run --rm -T` overrides `CMD` but not `ENTRYPOINT`.** The image has to use an exec-form entrypoint (`exec "$@"`) so the migrate argv reaches PID 1 inside the container. A shell-form entrypoint (`CMD ["sh", "-c", "…"]`) means the override is appended as additional shell args and never runs.
- **Run-after-pull timing.** The pre-pull `HEAD` is captured before `git pull`, the post-pull `HEAD` after it succeeds. If `git pull` itself fails, both heads are equal and only the ℹ️ notice prints. `⚠️` means the pull brought migration/seed files but they were **not** run: the new code is now live on the **old** schema. Re-deploy with `migrate=true` right away (or roll back) if that code needs the new schema.

## Run tests locally

```bash
python3 -m unittest -v
```

Manual smoke run (in a temporary tree):

```bash
mkdir -p /tmp/ngeupdate-test/myapp && touch /tmp/ngeupdate-test/myapp/compose.yaml
NGEUPDATE_KEY=devkey ALLOWED_ROOT=/tmp/ngeupdate-test python3 server.py

# in another shell
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8787/mcp   # 401

curl -s -X POST http://127.0.0.1:8787/mcp \
  -H "Authorization: Bearer devkey" \
  -H "X-Project-Path: /tmp/ngeupdate-test/myapp" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

End-to-end async smoke test (deploy → deploy_status loop → final FAILED because docker isn't on your Mac):

```bash
# returns a job id in ~ms
curl -s -X POST http://127.0.0.1:8787/mcp \
  -H "Authorization: Bearer devkey" \
  -H "X-Project-Path: /tmp/ngeupdate-test/myapp" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"deploy","arguments":{"path":"/tmp/ngeupdate-test/myapp","mode":"fast"}}}'

# poll until final output
curl -s -X POST http://127.0.0.1:8787/mcp \
  -H "Authorization: Bearer devkey" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"deploy_status","arguments":{"job_id":"<paste id here>"}}}'
```

## Not yet (planned)

- SSE streaming of live logs.
- Per-user keys + structured audit log.
