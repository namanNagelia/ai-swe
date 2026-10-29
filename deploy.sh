#!/usr/bin/env bash
# Deploy Root or Route to its VM. Asks for everything it needs; no files to edit.
#
#   ./deploy.sh                     # first run: asks repo, model and keys, then deploys
#   ./deploy.sh                     # later runs: just redeploys (after code changes)
#   RR_RECONFIGURE=1 ./deploy.sh    # change the repo or model
#   RR_RESET_SECRETS=1 ./deploy.sh  # enter new keys
#   RR_VM=rr-test ./deploy.sh       # a different VM (default: rr-agent)
#
# Answers are saved in agent.toml (gitignored, no keys in it). Keys are typed hidden
# and go straight to the VM; they're never written to a file on this machine.
# To skip the key prompts, export RR_SECRET_GITHUB_TOKEN and RR_SECRET_MODEL_API_KEY.
set -euo pipefail
cd "$(dirname "$0")"

CONFIG=${RR_CONFIG_FILE:-agent.toml}
VM=${RR_VM:-rr-agent}

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
ask() {
  local reply
  read -r -p "    $1 [$2]: " reply </dev/tty
  printf '%s' "${reply:-$2}"
}
rr() { uv run --quiet rr "$@" </dev/null; }

command -v uv >/dev/null || die "uv is not installed: https://docs.astral.sh/uv/"

if [[ ! -f $CONFIG || ${RR_RECONFIGURE:-} == 1 ]]; then
  say "Setup (saved to $CONFIG; nothing secret goes in it)"
  repo=$(ask "GitHub repo for the agent (owner/name)" "namanNagelia/rr-playground")
  [[ $repo =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "repo must look like owner/name"
  echo "    Model provider:  1) OpenAI   2) Anthropic   3) other OpenAI-compatible server"
  choice=$(ask "Pick 1, 2 or 3" "1")
  base_url=""
  case $choice in
    1) model=$(ask "OpenAI model" "gpt-5") ;;
    2) model=$(ask "Anthropic model" "claude-opus-5-5") ;;
    3) base_url=$(ask "Server URL ending in /v1" "http://127.0.0.1:11434/v1")
       model=$(ask "Model name on that server" "qwen3-coder") ;;
    *) die "pick 1, 2 or 3" ;;
  esac
  if [[ $choice != 2 && $model == claude* ]]; then
    die "claude models need provider 2 (Anthropic)"
  fi
  if [[ $choice == 2 && $model != claude* ]]; then
    die "Anthropic model names start with claude-"
  fi
  {
    echo "# Written by deploy.sh. Change it with: RR_RECONFIGURE=1 ./deploy.sh"
    echo "repo = \"$repo\""
    echo "model = \"$model\""
    if [[ -n $base_url ]]; then echo "base_url = \"$base_url\""; fi
  } >"$CONFIG"
fi

cfg_out=$(uv run --quiet python - "$CONFIG" <<'PY'
import sys
from pathlib import Path
from rootorroute.config import load
c = load(Path(sys.argv[1]))
print(c.github.repos[0])
print(c.model.provider)
print(c.model.model)
PY
) || die "$CONFIG is not valid (see above). Redo it with: RR_RECONFIGURE=1 ./deploy.sh"
{ read -r repo; read -r provider; read -r model; } <<<"$cfg_out"
say "Repo $repo, model $model"

state=$(rr vm status --name "$VM" 2>/dev/null | awk '/^state:/ {print $2}' || true)
case $state in
  running) say "VM $VM is running" ;;
  stopped|paused|suspended) say "Starting VM $VM"; rr vm start --name "$VM" ;;
  *) say "Creating VM $VM (first time takes 10-20 minutes)"; rr vm up --name "$VM" ;;
esac

have=$(rr vm secrets list --name "$VM" 2>/dev/null || true)

set_secret() {  # name, prompt, env var holding a value to use instead of prompting
  local name=$1 prompt=$2 env_var=$3 value
  if [[ ${RR_RESET_SECRETS:-} != 1 ]] && grep -qx "$name" <<<"$have"; then
    return 0
  fi
  value=${!env_var:-}
  if [[ -z $value ]]; then
    read -r -s -p "    $prompt (hidden, then Enter): " value </dev/tty
    echo
  fi
  value=${value//[[:space:]]/}
  [[ -n $value ]] || die "nothing entered for $name"
  printf '%s\n' "$value" | uv run --quiet rr vm secrets set --name "$VM" "$name" >/dev/null 2>&1 ||
    die "couldn't store $name on the VM"
  say "Stored $name on the VM"
}

set_secret github_token "Paste your GitHub token (github_pat_...)" RR_SECRET_GITHUB_TOKEN
case $provider in
  anthropic) set_secret model_api_key "Paste your Anthropic key (sk-ant-...)" RR_SECRET_MODEL_API_KEY ;;
  *) set_secret model_api_key "Paste your model API key (local server: type none)" RR_SECRET_MODEL_API_KEY ;;
esac

say "Installing and starting the agent"
rr vm deploy --name "$VM" --config "$CONFIG" | grep -E "==>|Active:" || true

cat <<EOF

$(say "Ready. Give it a task:")
  uv run rr task "what you want done" --repo $repo --name $VM
  uv run rr logs <task-id> -f --name $VM     # follow it; prints the PR link at the end
  uv run rr tasks --name $VM                 # everything it has done
  uv run rr vm tunnel --name $VM             # web inbox at http://localhost:8765
EOF
