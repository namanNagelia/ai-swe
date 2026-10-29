# Runbook: running Root or Route

Root or Route is an AI engineer that lives on its own VM. You give it a task; it plans
the work into tickets, writes the code, runs the checks, and opens a pull request.

Run every command below from this folder (`Root or Route`). Paste each one as a single line.

---

## 1. One-time setup (5 minutes)

**Install the tools**
```
brew install uv lima gh
gh auth login
```

**Pick a repo for the agent.** Use a copy you don't mind getting PRs on. To copy one
of your repos (here `airplaneDataAnalysis`) into `rr-playground`:
```
gh repo create namanNagelia/rr-playground --public
git clone --bare https://github.com/namanNagelia/airplaneDataAnalysis.git /tmp/copy.git
git -C /tmp/copy.git push --mirror https://github.com/namanNagelia/rr-playground.git
```

**Get two keys** (keep them in a password manager, not a file)

| Key | Where | Settings |
|---|---|---|
| GitHub token | https://github.com/settings/personal-access-tokens/new | Repository access: **only** `rr-playground`. Permissions: **Contents**, **Pull requests**, **Issues** = Read and write. Copy the `github_pat_...` value. |
| Model key | OpenAI: https://platform.openai.com/api-keys · Anthropic: https://console.anthropic.com/settings/keys | Create a key, copy it. |

---

## 2. Deploy

```
./deploy.sh
```

It asks four things and does the rest:

1. **Repo**: e.g. `namanNagelia/rr-playground`
2. **Provider**: `1` OpenAI, `2` Anthropic, `3` other server (e.g. Ollama)
3. **Model**: press Enter for the default, or type one
4. **Keys**: paste the GitHub token, then the model key (typing is hidden)

The first run creates the VM, which takes 10-20 minutes. After that, a deploy takes about a minute.
Your answers are saved in `agent.toml` (no keys in it). Keys go only to the VM.

---

## 3. Give it a task

```
uv run rr task "Refactor i.py into small functions with a main(), load the CSV from the local file, add pandas to the Pipfile, add pytest tests, and a README" --repo namanNagelia/rr-playground
```

It prints a task id like `t-20261028-091500-ab12`. Follow it live:
```
uv run rr logs t-20261028-091500-ab12 -f
```
When it finishes it prints the PR link. Review and merge the PR on GitHub like any other.

Or use the web page: run `uv run rr vm tunnel` and open http://localhost:8765.

GitHub `@mentions` as a way to give tasks only work with a separate bot account (the agent ignores
comments written by its own account, which is you if the token is yours). Use `rr task` for now.

---

## Everyday commands

| What | Command |
|---|---|
| List tasks | `uv run rr tasks` |
| Details of one task (tickets, PR, why it stopped) | `uv run rr show <id>` |
| Follow a task | `uv run rr logs <id> -f` |
| Stop a task | `uv run rr cancel <id>` |
| Continue a stopped task | `uv run rr retry <id>` |
| Change repo or model | `RR_RECONFIGURE=1 ./deploy.sh` |
| New keys | `RR_RESET_SECRETS=1 ./deploy.sh` |
| After pulling new code | `./deploy.sh` |
| Shell on the VM | `uv run rr vm shell --as agent` |
| Agent's own log | `uv run rr vm shell` then `sudo journalctl -u rr-agent -f` |
| Stop / start the VM | `limactl stop rr-agent` / `./deploy.sh` |

---

## When something goes wrong

| You see | Do this |
|---|---|
| `command not found: rr` | Put `uv run` in front: `uv run rr ...` (or `source .venv/bin/activate` once). |
| A task shows `blocked (error)` | `uv run rr logs <id>`; the `why:` line says what failed. Fix it, then `uv run rr retry <id>`. |
| `blocked (limit)` | It ran out of steps or tokens. Make the task smaller, or retry. |
| `blocked (checks_failing)` | The repo's tests kept failing. `uv run rr show <id>`, look at the PR branch, retry or rephrase. |
| Model errors (401, model not found) | Wrong key or model name: `RR_RECONFIGURE=1 RR_RESET_SECRETS=1 ./deploy.sh`. |
| GitHub errors (403, 404) | The token can't reach the repo: make a new token with the settings in step 1, then `RR_RESET_SECRETS=1 ./deploy.sh`. |
| Anything else | `uv run rr vm shell` then `sudo journalctl -u rr-agent -n 100`. |

---

## Safety notes

- The agent never pushes to `main`; every change arrives as a PR for you to review.
- Commands it runs execute as a separate sandbox user that can't read your keys.
- Keys live only in `/home/agent/state/secrets` on the VM. Never put them in `agent.toml`
  or any other file. If a key ever ends up in a file or a chat, revoke it and make a new one.
