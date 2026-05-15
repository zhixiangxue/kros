<div align="center">

<img src="https://raw.githubusercontent.com/zhixiangxue/kros/main/docs/assets/logo.png" alt="kros" width="120">

[![License](https://img.shields.io/github/license/zhixiangxue/kros)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-linux%2Famd64-lightgrey.svg)](#)
[![Docker](https://img.shields.io/badge/runtime-Docker-blue.svg)](https://www.docker.com/)

**Self-contained. Framework-free. Agent-ready.**

An operating system for LLM agents — shipped as a single Docker image.

</div>

---

## What is KROS?

A Docker image that gives any LLM agent **LLM-friendly CLI superpowers** —
plus a 30-line bash launcher on the host.

- **Inside the image**: structured HTTP, headless browser, universal file reader, persistent memory, audited shell, sandboxed execution, audit log.
- **Outside the image**: `kros run <your-script>` — a thin wrapper around `docker run`.

> No framework lock-in. No SDK to import. Any agent that can run a shell can use kros.

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/zhixiangxue/kros/main/launcher/install.sh | sh
```

Docker is the only runtime dependency.

---

## Quick start

`kros run` drops your shell script into a container that already ships with
7 LLM-friendly CLIs. Your only job is to **let your LLM know they exist** —
the image provides two discovery channels for that:

- `/etc/kros/system-prompt.md` — the full capability manifest, ready to be injected into your LLM's system prompt
- `kros caps` — the same manifest as a CLI command

Minimal skeleton (framework-agnostic):

```bash
# my-agent.sh — entrypoint, runs inside the container
pip install <your-favorite-agent-framework>   # langchain / autogen / chakpy / ...
export KROS_SYSTEM_PROMPT="$(cat /etc/kros/system-prompt.md)"
python /work/agent.py                          # your agent injects $KROS_SYSTEM_PROMPT
                                               # and gives the LLM a shell tool
```

```bash
# on the host
kros run ./my-agent.sh \
    -v "$PWD/agent.py:/work/agent.py:ro" \
    -e DEEPSEEK_API_KEY
```

That's it. Once the LLM sees the manifest in its system prompt, it will
shell out to `kros http`, `kros browse`, `kros file`, ... on its own —
no SDK, no framework integration, no kros-specific code in your agent.

---

## The 7 superpowers

| Command | Purpose |
| --- | --- |
| `kros http`    | Structured HTTP client (semantic exit codes, JSON stdout) |
| `kros browse`  | Headless browser — navigate, click, fill, extract (16 commands) |
| `kros file`    | Read any document (PDF / Word / Excel / images / audio → Markdown) |
| `kros memory`  | Persistent cross-session memory (remember / recall) |
| `kros shell`   | Audited shell execution |
| `kros sandbox` | Isolated execution (CPU, memory, network limits) |
| `kros log`     | Query the audit trail |

Inside the container, run `kros caps` for the complete reference.

---

## How it works

Two layers — that's the whole architecture:

```
host         kros (bash launcher, ~30 LOC)
                │
                ▼   docker run
container    kros (full CLI)
              ├── http / browse / file / memory / ...
              ├── /etc/kros/system-prompt.md   ← capability manifest
              └── /etc/profile.d/kros.sh       ← MOTD on first shell
```

Agents auto-discover capabilities via either:

- **MOTD** — printed on the first shell inside the container
- **`/etc/kros/system-prompt.md`** — for frameworks that inject capabilities into system prompts

No prior knowledge of kros is required by the agent.

---

## Notes

1. **Linux/amd64 only.** The image targets amd64; ARM users can build locally.
2. **Bring your own LLM credentials.** Pass them via `kros run -e KEY`.
3. **`kros run` mounts only the script you specify.** Anything else (code, configs, secrets) must be declared explicitly via `-v` / `-e`. No magic.

---

## Feedback

[https://github.com/zhixiangxue/kros](https://github.com/zhixiangxue/kros)

Issues, PRs, and stars are welcome.

---

## License

MIT © 2026 zx

---

<div align="right"><img src="https://raw.githubusercontent.com/zhixiangxue/kros/main/docs/assets/logo.png" alt="kros" width="120"></div>
