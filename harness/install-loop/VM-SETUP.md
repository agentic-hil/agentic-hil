# VM setup — install-loop test (VMware Workstation, Ubuntu 24.04 Server)

A clean, disposable Ubuntu VM to run the install-prompt loop against Claude Code,
Codex, and opencode. Hardware-free — this is NOT the hardware HIL harness (that
needs USB passthrough + OpenOCD and is a separate, later setup).

Why a VM: the agents run with `--dangerously-*` flags so they may actually
install things. A throwaway VM keeps that off your real machine and gives a
clean, snapshottable environment.

---

## 1. Download Ubuntu Server ISO
Ubuntu 24.04.x LTS **Server** (not Desktop — headless is enough):
https://ubuntu.com/download/server  →  `ubuntu-24.04.x-live-server-amd64.iso`

## 2. Create the VM (VMware Workstation Pro)
1. **File ▸ New Virtual Machine ▸ Typical**.
2. **Installer disc image (ISO)** → pick the ISO. (If VMware offers "Easy Install",
   you may fill user/password there; otherwise install manually in step 3.)
3. Guest OS: **Linux ▸ Ubuntu 64-bit**.
4. Name it (e.g. `ahil-install-loop`), choose a location.
5. Disk: **30 GB**, "Store as a single file".
6. **Customize Hardware**: 2 CPUs, 4096 MB RAM. Finish.

## 3. Install Ubuntu Server
Boot the VM, then in the installer accept defaults, with:
- Language / keyboard: your choice.
- Network: DHCP (default).
- Storage: "Use entire disk" → Done → Continue.
- Profile: create a user, e.g. **`tester`**, with a password (remember it).
- **Install OpenSSH server**: optional (handy for pasting long commands).
- Skip all "Featured server snaps".
Let it finish → **Reboot Now**. VMware removes the ISO automatically; log in as `tester`.

## 4. Base packages + VMware tools
```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  git curl ca-certificates build-essential python3 python3-venv open-vm-tools
```

## 5. Node.js 22 (the three agent CLIs are Node)
```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
node --version        # expect v22.x
```

## 6. Install the three agent CLIs
```bash
# Claude Code
npm install -g @anthropic-ai/claude-code
claude --version

# Codex  (scoped @openai/codex -- NOT the unrelated bare "codex" package)
npm install -g @openai/codex
codex --version

# opencode
curl -fsSL https://opencode.ai/install | bash
opencode --version
```
If `opencode` is not on PATH after the curl installer, add its bin dir
(usually `~/.opencode/bin`) to PATH: `echo 'export PATH="$HOME/.opencode/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc`.

## 7. Snapshot "agents-ready"
Power off (`sudo poweroff`), then in VMware take a snapshot named **`agents-ready`**.
Revert to it anytime you want a pristine agent environment.

## 8. Get the harness (the branch)
```bash
git clone -b feature/smooth-installation \
  https://github.com/agentic-hil/agentic-hil ~/agentic-hil
cd ~/agentic-hil
```

## 9. Authenticate (env vars — the runner redirects HOME, so file-based login is not seen)
```bash
# Claude Code: get a token (opens a URL, paste the code back)
claude setup-token          # prints a token
export CLAUDE_CODE_OAUTH_TOKEN=<the token>

# Codex
export OPENAI_API_KEY=<your key>          # or: export CODEX_API_KEY=<key>

# opencode (key for whatever OPENCODE_MODEL you use; default is an anthropic model)
export ANTHROPIC_API_KEY=<your key>
```

## 10. Run the loop
```bash
bash harness/install-loop/run-install-prompt.sh claude
# then:   ... codex     |     ... opencode
```
Read `harness/install-loop/transcripts/install-<agent>-*.log`. Each run uses a
fresh `$HOME`, so agentic-hil installs from nothing every time. The prompt targets
`git+...@feature/smooth-installation`, so the agent fetches the branch docs/code.

## 11. Iterate
- Improve `AI_AGENT_QUICKSTART.md` on the branch (on your host), commit, push.
- Doc edits take effect on the **next run without pulling** in the VM — the agent
  fetches the branch fresh over the network each run.
- Only if the runner **script** itself changed: `git -C ~/agentic-hil pull`.

## Notes
- The `--dangerously-*` flags are safe here because the VM is disposable.
- For repeated clean runs you can also just revert the `agents-ready` snapshot.
- To later turn this into the hardware HIL harness, add USB passthrough for the
  ST-Link and run `harness/provision-golden.sh` — see `harness/README.md`.
