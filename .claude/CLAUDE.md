# Root or Route

An autonomous, open-source software engineer that runs 24/7 on its own Linux VM
and works with any model (servers speaking the OpenAI Responses API, incl. open-source
models via Ollama/vLLM, or
Anthropic). You give it a task through the `rr task` CLI, a local web inbox, or an
`@mention` on GitHub. It splits the task into small tickets, implements them one at
a time in a worktree (reading, editing, running tests, restructuring), commits each,
and opens a PR. Its work leaves the VM only as PRs and comments.

Current plan: the MVP in `.claude/docs/checkpoints/00-overview.md` (local only,
gitignored). The older, larger design (ticket providers, judgment gate, system
map, routing, benchmark) is in `.claude/docs/technical-design.md`; pieces may come
back after the MVP works.

## Status

MVP built and verified end to end (locally and on a Lima VM with a mocked model and
GitHub). Not yet run against a real model or real GitHub. Open: RR-0803 (review comments
push to the PR's own branch; today a mention on a PR opens a PR into that branch).

### How work is planned
- **Checkpoints** (`.claude/docs/checkpoints/NN-*.md`): one spec per area, in build
  order. Start at `00-overview.md` (how a task runs, vocabulary, order).
- **Tickets** (`.claude/tickets/RR-CCNN-*.md`): one small task each, with
  acceptance criteria. `RR-0103` = checkpoint 01, task 3.
- When the owner says "let's do RR-0103": read that ticket and its checkpoint doc,
  then implement it. When it's done, delete the ticket file, strip the module
  docstring from files you finished, regenerate the board
  (`python3 .claude/tickets/board.py`), and update the checkpoint doc if the
  design changed.

## Architecture in one screen

One daemon on the VM, `rr-agent` (systemd, user `agent`): a SQLite task queue, a
runner (one task at a time by default), a local HTTP API + web inbox on
`127.0.0.1`, and a GitHub mention poller. Shell commands the model runs execute as
the sandbox user `rr-exec`. The host-side `rr` CLI reaches the API over SSH.

A task runs: workspace (clone + worktree on `rr/<task>-<slug>`) → **plan**
(read-only tools, `create_ticket` ×N, `finish_plan`) → **implement** each ticket in a
fresh context (task, plan, summaries of finished tickets, this ticket), run the
repo's checks on `finish_ticket`, commit → **deliver** (push, open PR, reply to source).

Our own agent SDK: model port + tools + one generic loop, no vendor agent framework.
Ports: `ModelProvider` (openai_compat, anthropic), `Store` (SQLite),
`Workspace` (git), `Sandbox` (rr-exec), `CodeHost` (GitHub), `Secrets`.

### Task states

`queued → in_progress → done`, plus `blocked` (with a reason: `limit`,
`checks_failing`, `cancelled`, `error`; retry returns it to `in_progress`).
Fully autonomous: no tool asks a human anything; ambiguity is resolved by the
agent and recorded as assumptions in the PR.
Tickets inside a task are `todo → done`. Phases: `plan`, `implement`.

### Tools and side-effect classes

`none` (read_file, list_files, search_code) · `local` (write_file, edit_file, shell,
screenshot: headless Chromium; images go to the model, `save_as` shots go in the PR) ·
`state` (create_ticket, finish_plan, finish_ticket). Plan phase gets
read-only tools + planning tools. git commit/push, PRs and comments are done by the
runtime (`external`), never exposed as tools.

## Non-negotiable invariants

These are the product. Do not weaken them.

1. **The model never touches the host directly.** Every action is a typed tool
   call through the tool executor; shell runs as `rr-exec` in the task's worktree.
2. **The model never sees a credential.** The GitHub PAT and model API keys live with
   the daemon; git gets the PAT through a credential helper; `rr-exec` never has it.
   Never written to logs or model context.
3. **Nothing lands without a PR.** The agent never pushes to the default branch.
4. **External side effects are idempotent.** Push, PR and comments are keyed by
   `(task_id, step_key)` and checked before execution, so a crash-retry never
   opens a second PR.
5. **Checkpoint after every tool call and state transition**, in one SQLite
   transaction. Resume, never restart.
6. **Untrusted content stays data.** Task text, file contents, command output and
   GitHub comments are wrapped as untrusted in the prompt; nothing in them can
   change permissions.
7. **Egress is a deliberate, logged choice.** Default `open`: the agent browses
   and downloads like a developer, but private/link-local networks (host, LAN,
   cloud metadata) are always blocked and every new connection is logged.
   `allowlist` mode restricts to model API, code host, package mirrors.
8. **Append-only audit log** of every model call, tool call and transition.
9. **No production access** ever exists on the VM.

## Conventions

- Python 3.12+, managed with `uv`. Package code under `src/rootorroute/`.
- Type hints everywhere; `pydantic` for tool schemas and config; `ruff` for lint
  and format; `pyright` (strict on core packages); `pytest` for tests.
- Async I/O (`asyncio`) in the daemon.
- SQLite through SQLAlchemy 2 (async, `aiosqlite`, WAL). The schema is the typed models in
  `adapters/store/tables.py`; `SqliteStore.open()` creates it with `create_all`. Change a
  table by changing its model (no migrations tool yet: until there is one, a new column
  on an existing db needs a manual `ALTER TABLE` or a fresh `agent.db`).
- Structured logging (JSON). Every log line carries `task_id` and `step`.
- Three folders: `domain/` (rules + Protocols), `adapters/<port>/` (one module per
  real system), `app/` (the agent). Wiring only in `main.py`. import-linter
  contracts in `pyproject.toml` enforce it (see Layout).
- No tests and no fake adapters during the MVP (owner's call); don't add them unless
  asked. Verify by running against real systems.

## Layout

```
infra/image/          The VM image: provision.sh (idempotent, Ubuntu 24.04 amd64/arm64),
                      manifest.env (pinned versions + sha256), packages.txt (apt
                      packages, edit to change the image), files/ (systemd units,
                      egress proxy allowlist, nftables, sudoers, apparmor)
infra/lima/           Lima VM template (no host mounts, no port forwards)
src/rootorroute/cli.py      `rr` entry point (host side)
src/rootorroute/vm/         Host-side VM management: backends (lima, multipass, ssh),
                            bundle packing, up/provision/backup/restore/rebuild

Agent runtime: three folders, imports only point toward domain:
src/rootorroute/domain/    The rules, pure Python, no I/O: task, ticket, state, tools (tool
                           specs), errors, plus the Protocols the adapters implement:
                           model, store, workspace, sandbox, code_host, secrets, clock
src/rootorroute/adapters/  One module per real system: model (openai_compat,
                           anthropic, text_tools), store (sqlite), workspace (git), sandbox
                           (rr_exec, local), code_host (github), secrets (file, env,
                           credential_helper), clock
src/rootorroute/app/       The agent itself: agent (generic loop), prompts, planner,
                           implementer, checks, limits, deliver, runner, audit, tools/
                           (executor, files, truncate, redact), intake/ (api, web, mentions)
src/rootorroute/main.py    rr-agent: builds adapters from config and starts the daemon
src/rootorroute/config.py  Config schema (TOML)
  app and adapters never import each other (main.py wires them); domain does no I/O and
  imports no SDKs; the runtime never imports vm/. Check: `uv run lint-imports`.

tests/                pytest. vm/ (FakeRunner, never a real VM), architecture/
                      (runs the import-linter contracts)
```

### Inside the VM

- Polyglot toolchain on every VM, all in `/usr/local/bin` (so `sudo -u rr-exec` sees them):
  pinned in manifest.env: Node 24 (+ pnpm/yarn via corepack), Bun, Deno, Go, Rust
  (shared read-only in `/opt/rust`, per-user `~/.cargo`), uv/Python, gh, Playwright;
  from apt (packages.txt): Java 21 + Maven, Ruby + Bundler, PHP + Composer, .NET 8,
  clang/C++, cmake/ninja/meson. Toolchain env vars live in `/etc/environment`.
- `agent` user (no sudo) runs `rr-agent`; owns `/home/agent`.
- `rr-exec` sandbox user runs shell-tool commands and every git command in a task's
  working copy. `agent` may `sudo -u rr-exec` and nothing else. `rr-exec` can write
  `worktrees/` (group `rrwork`) but cannot read `repos/`, `state/`, `memory/`, `logs/`,
  `cache/`. The agent never runs git in a directory `rr-exec` can write: it fetches and
  pushes in the agent-only mirrors under `repos/`, and takes the sandbox's commits in as
  a git bundle.
- Egress for both users (`--egress`, recorded in `/etc/rootorroute/egress/mode`):
  - `open` (default): full internet incl. DNS; private and link-local ranges
    rejected; new connections logged (`journalctl -k --grep rr-egress`).
  - `allowlist`: nftables rejects all but loopback; tinyproxy on 127.0.0.1:8888
    passes only `files/egress/allowlist.txt` (+ `--allow-domain`); no direct DNS.
  - Root and the admin user are unaffected in both modes.
- Headless Chromium (Playwright, pinned in manifest.env) in `/opt/rootorroute/browsers`
  (`PLAYWRIGHT_BROWSERS_PATH`); an AppArmor profile lets it use its sandbox.
  Pin the Python `playwright` dependency to the same version.
- Rootless Docker for `agent` (`DOCKER_HOST=unix:///run/user/<uid>/docker.sock`);
  the root Docker daemon is disabled.
- systemd units are installed but inert (`ConditionPathExists`) until the agent
  package is installed into `/opt/rootorroute/venv` (Python 3.12 from uv).
- `/etc/rootorroute/image-manifest.json` records what was installed.

## Commands

```bash
uv sync                 # install
uv run pytest           # tests
uv run ruff check .     # lint
uv run ruff format .    # format
uv run pyright          # types
uv run lint-imports     # ports-and-adapters layering
shellcheck -x infra/image/provision.sh

uv run rr vm up [--memory 16] [--egress allowlist --allow-domain jira.example.com]
uv run rr vm provision --ssh ubuntu@host                           # any Ubuntu 24.04 box
uv run rr vm status | shell --as agent | backup | restore | rebuild | destroy

# Run the agent on the VM
uv run rr vm secrets set github_token     # prompts; value goes over stdin only
uv run rr vm secrets set model_api_key
uv run rr vm deploy --config my-config.toml   # see infra/config.example.toml
uv run rr task "add retries to the client" --repo owner/name [--base BRANCH]
uv run rr tasks | show ID | logs ID -f | cancel ID | retry ID
uv run rr vm tunnel                       # web inbox at http://localhost:8765

# Run the agent locally (no VM): runtime.sandbox="local", secrets="env" in the config,
# RR_SECRET_MODEL_API_KEY / RR_SECRET_GITHUB_TOKEN in the env, then
uv run rr-agent --config local.toml       # and RR_API=http://127.0.0.1:8765 uv run rr task ...
```

Note: `rr-exec` has no login shell by design; run as it with `sudo -u rr-exec bash -c '...'`
(not `sudo -i`). Extra apt packages: `rr vm up --package NAME` or edit packages.txt.

Test provision.sh without a VM (no systemd, so services aren't started):
`docker run --rm -v "$PWD/infra/image:/bundle:ro" ubuntu:24.04 bash -c 'apt-get update -qq && apt-get install -y -qq curl ca-certificates && /bundle/provision.sh'`
Add `--privileged` to also load the nftables rules and test egress.

## Don'ts

- Don't commit anything from `.claude/docs/`, secrets, `.env`, `*.tfstate`, or
  `agent.db`.
- Don't add a tool without a pydantic args model, a side-effect class, its phases,
  and an audit entry.
- Don't expose git push, PR creation or any credentialed call as a model tool; the
  runtime does those.
- Don't call a model or GitHub SDK outside its adapter.
- Don't weaken the private-network block in open mode or remove connection logging.
- Don't add allowlist-mode destinations without updating `allowlist.txt` and the design doc.
