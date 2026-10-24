# shellcheck shell=sh
# Login-shell environment for every user on the agent VM.
# Toolchains are in /usr/local/bin; per-user installs (cargo, go, pipx) go here.
export PATH="$HOME/.cargo/bin:$HOME/go/bin:$HOME/.local/bin:$PATH"

# In allowlist egress mode the agent and sandbox users must go through the proxy.
if [ "$(cat /etc/rootorroute/egress/mode 2>/dev/null)" = allowlist ]; then
  case "$(id -un)" in
    agent|rr-exec)
      export HTTPS_PROXY=http://127.0.0.1:8888 HTTP_PROXY=http://127.0.0.1:8888
      export https_proxy=$HTTPS_PROXY http_proxy=$HTTP_PROXY
      export NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1
      ;;
  esac
fi
