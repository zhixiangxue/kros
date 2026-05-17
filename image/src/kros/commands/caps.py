"""`kros caps` — output the full capability manifest for LLM agent consumption.

This is the "desktop" of Kros as an Agent OS: one command, complete picture.
An agent framework calls ``kros caps`` once on startup, injects the output
into the system prompt, and the agent never needs to call ``--help`` again.

Design choices:
- All metadata is hand-written here (single source of truth, easy to update).
- We do NOT introspect typer at runtime — the manifest must be precise,
  curated, and example-rich, which auto-generation can never match.
- Default output is Markdown (LLM-native). ``--format json`` for programmatic use.
"""

from __future__ import annotations

import json
import sys
from importlib.metadata import version as pkg_version
from typing import Any

import typer

__all__ = ["register", "caps_app"]

# ---------------------------------------------------------------------------
# The manifest — one entry per top-level subcommand
# ---------------------------------------------------------------------------

_VERSION = pkg_version("kros")

_GLOBAL_RULES = """\
## Rules (apply to ALL commands)

1. stdout is the primary output channel — structured text or JSON depending on the command.
2. stderr is for human-readable error messages and debug info; never parse it.
3. Exit code 0 always means success. Non-zero codes are semantic (see each command).
4. All commands are stateless across invocations unless noted (browser tabs persist).
5. Flags use GNU long-form (--timeout, --json). Short aliases (-t, -H) exist for the most common ones.
"""

# Each entry: command signature, summary, body lines (actions/flags/exit codes/examples)
_COMMANDS: list[dict[str, Any]] = [
    {
        "name": "kros http <method> <url>",
        "summary": "LLM-friendly HTTP client. Semantic exit codes + single JSON stdout.",
        "body": """\
Methods: get, post, put, delete, patch, head, options

Key flags:
  --json <str>       Request body as JSON string (validated upfront; exit 4 on invalid)
  --header/-H <str>  Add request header (repeatable). Format: "Name: Value"
  --timeout <int>    Total request timeout in seconds (default 30)
  --follow           Follow redirects (default: stop at 3xx and report Location)
  --quiet            Omit response body from stdout JSON (only status/headers/meta)

Exit codes:
  0  HTTP 2xx (success)
  1  HTTP 4xx (client error)
  2  HTTP 5xx (server error)
  3  Network error or timeout (DNS, connection refused, TLS, read timeout)
  4  Local validation error (bad --json, missing scheme, etc.)

stdout JSON shape (always valid JSON, even on error):
  {"ok", "status", "headers", "elapsed_ms", "url_final", "body", "body_path", "body_truncated", "body_encoding", "error"}

Examples:
  kros http get https://api.example.com/users --timeout 10
  kros http post https://api.example.com/data --json '{"name":"Alice"}' -H "Authorization: Bearer $TOKEN"
  kros http get https://example.com/redirect --follow""",
    },
    {
        "name": "kros browse <command> [options]",
        "summary": "Agent-first headless browser. Tabs with implicit 'current'; 16 commands.",
        "body": """\
Tab model: open creates a new tab (auto-current); all other commands target the current tab by default.
Use --tab N to target a specific tab without switching.

Navigation & lifecycle:
  open <url> [--timeout N]         Open URL in a new tab, return full page snapshot
  close [--tab N | --all]          Close tab(s)
  info [--tab N]                   Show tab state (alive/url/title/driver)

Reading & locating:
  read [--tab N]                   Snapshot current page (URL, title, markdown, elements)
  find --role X [--name Y]         Locate elements by ARIA role/name
  wait --selector "css" [--timeout-ms N]  Block until element appears in DOM
  eval --script "js code"          Execute JavaScript, return stringified result

Interaction:
  click --ref N [--timeout N]      Click element by ref, return updated page
  fill --ref N --value "text"      Type into input field
  press --key "Enter" [--ref N]    Send keypress
  hover --ref N                    Hover over element
  select --ref N --value "opt"     Pick dropdown option
  check --ref N --checked true     Set checkbox/radio state
  scroll --ref N | --y N           Scroll element into view or page by pixels

Tab management:
  list                             List all live tabs (* = current)
  switch --tab N                   Change current tab pointer

Exit codes:
  0  Success
  1  Generic driver error
  2  No active session (run `open` first)
  3  Session already exists (run `close` first)
  4  Bad input (missing scheme, invalid ref)
  5  Navigation timeout (retryable with larger --timeout)

Examples:
  kros browse open "https://example.com"
  kros browse find --role link --name "Sign in"
  kros browse click --ref 42
  kros browse fill --ref 15 --value "hello@example.com"
  kros browse eval --script "document.title"
  kros browse close --all""",
    },
    {
        "name": "kros shell run <cmd>",
        "summary": "Execute a shell command via /bin/bash -c. Streams output, records audit.",
        "body": """\
Key flags:
  --cwd <path>       Working directory (default: inherit from caller)
  --env/-e KEY=VAL   Extra environment variable (repeatable)
  --timeout/-t <sec> Kill after N seconds (SIGTERM → 3s grace → SIGKILL)
  --no-unwrap        Don't optimize nested `kros ...` calls (force subprocess)

Exit codes:
  Mirrors the executed command's exit code exactly.
  124 = killed by --timeout (GNU timeout convention)
  126 = --cwd not a directory
  127 = /bin/bash not found

Special behavior:
  If <cmd> starts with "kros", it is dispatched in-process (same audit line,
  no subprocess overhead). Use --no-unwrap to force subprocess execution.

Examples:
  kros shell run "ls -la /workspace"
  kros shell run "python3 train.py" --timeout 3600 --cwd /project
  kros shell run 'curl -s $API_URL' --env API_URL=https://example.com""",
    },
    {
        "name": "kros shell spawn <cmd>",
        "summary": "Dispatch a long-running command asynchronously. Returns a task_id immediately.",
        "body": """\
Use spawn for background services, long builds, or any task where you don't
want to block the conversation. Query results with `jobs`, `logs`, `kill`.

Key flags:
  --cwd <path>       Working directory (default: inherit from caller)
  --env/-e KEY=VAL   Extra environment variable (repeatable)
  --timeout/-t <sec> Auto-kill after N seconds (default 600). State becomes "killed_by_timeout"

stdout JSON (returned immediately):
  {"task_id": "t_9x82f1", "pid": 412, "started_at": "...", "timeout_sec": 600}

Exit codes:
  0  Task spawned successfully
  126 = spawn failed (OS error)
  127 = /bin/bash not found

Examples:
  kros shell spawn 'python app.py --port 8080'
  kros shell spawn --timeout 60 'pytest tests/'
  kros shell spawn --cwd /project --env PORT=3000 'npm start'""",
    },
    {
        "name": "kros shell jobs [TASK_ID]",
        "summary": "List async tasks spawned by `kros shell spawn`. NOT a wrapper of system `ps`.",
        "body": """\
Shows only kros-managed tasks. To inspect system processes use `kros shell run 'ps -ef'`.

Key flags:
  --state <str>   Filter by state: running / exited / killed_by_timeout / lost

State values (closed set):
  running            Task is alive
  exited             Task finished (check exit_code)
  killed_by_timeout  Auto-killed after --timeout expired (exit_code 124)
  lost               Task died abnormally before landing exit_code

stdout JSON (single task or array):
  {"task_id": "t_9x82f1", "pid": 412, "state": "running", "uptime_sec": 42, "started_at": "...", "cmd": "..."}

Examples:
  kros shell jobs                     # list all tasks
  kros shell jobs t_9x82f1            # single task detail
  kros shell jobs --state running     # only running tasks""",
    },
    {
        "name": "kros shell logs <task_id>",
        "summary": "Tail stdout/stderr of a spawned task (default: last 80 lines).",
        "body": """\
Designed for "last words" debugging: the tail is usually enough to identify
a failure without burning tokens on megabytes of log output.

Key flags:
  --tail/-n <int>  Lines from end per stream (default 80)
  --full           Dump entire stdout/stderr (use sparingly)

stdout JSON:
  {"task_id": "t_9x82f1", "state": "exited", "exit_code": 1, "stdout_tail": "...", "stderr_tail": "..."}

Important: accepts task_id (t_xxx), NOT a raw PID.

Examples:
  kros shell logs t_9x82f1
  kros shell logs t_9x82f1 --tail 200
  kros shell logs t_9x82f1 --full""",
    },
    {
        "name": "kros shell kill <task_id>",
        "summary": "Signal a spawned task (default SIGTERM). Refuses raw PIDs.",
        "body": """\
Signals the entire process group (wrapper + user command + grandchildren).
To kill a system process by PID, use `kros shell run 'kill <pid>'` instead.

Key flags:
  --signal/-s <name>  TERM (default) / KILL / INT

stdout JSON:
  {"task_id": "t_9x82f1", "killed": true, "signal": "SIGTERM"}
  {"task_id": "t_9x82f1", "killed": false, "reason": "already_exited"}

Important: only accepts task_id (t_xxx). Raw PIDs are rejected with guidance.

Examples:
  kros shell kill t_9x82f1
  kros shell kill t_9x82f1 --signal KILL""",
    },
    {
        "name": "kros file read <src>",
        "summary": "Read any document into LLM-ready Markdown. Supports PDF, Word, Excel, HTML, images (OCR), and more.",
        "body": """\
<src> can be a local file path or an http(s) URL.

Supported formats: csv, docx, epub, html, json, markdown, pdf, pptx, rst,
  rtf, tsv, txt, xlsx, xml, yaml, image (jpg/png/webp via OCR), audio (wav/mp3 via ASR)

Exit codes:
  0  Success (Markdown written to stdout)
  1  Unsupported format or parse error
  2  File not found

Output format:
  Structured Markdown with a header block (filename, format, size) followed
  by the document content. Designed to be directly appended to an LLM prompt.

Examples:
  kros file read /workspace/report.pdf
  kros file read https://example.com/data.xlsx
  kros file read /tmp/screenshot.png""",
    },
    {
        "name": "kros memory <command>",
        "summary": "Cross-session persistent memory for agents. Semantic search over stored knowledge.",
        "body": """\
Commands:
  remember <text>     Record + process in one call (most common)
  note <text>         Quick record (no LLM processing)
  dream               Process pending notes into structured memos
  recall <query>      Semantic search (returns top-N matches)
  list                List all memos (newest first)
  get <id>            Fetch single memo by ID
  update <id> <text>  Update memo content
  delete <id>         Delete a memo
  forget              Wipe all memos in namespace

Key flags (shared):
  --namespace/-n <str>  Memory partition (default: "default" or $KROS_MEMORY_NAMESPACE)
  --n <int>             Number of recall results (default 5, only for `recall`)

Exit codes:
  0  Success
  1  Memo not found / operation failed

Output format: tab-separated "id\\tcontent" per line (parseable with `cut -f1` for IDs).

Examples:
  kros memory remember "The API rate limit is 100 req/min"
  kros memory recall "rate limit" --n 3
  kros memory list --limit 10
  kros memory delete abc123""",
    },
    {
        "name": "kros sandbox run <cmd>",
        "summary": "Run a command in an isolated sandbox (bubblewrap). Network, CPU, memory, and filesystem constraints.",
        "body": """\
Key flags:
  --timeout/-t <sec>   Kill after N seconds
  --memory/-m <str>    Memory limit (default "512m"; e.g. "1g")
  --cpu <float>        CPU quota in cores (default 1.0; supports 0.5)
  --no-network         Disable network access inside sandbox
  --readonly           Make filesystem read-only
  --env/-e KEY=VAL     Extra environment variable (repeatable)
  --workdir/-w <path>  Working directory inside sandbox

Exit codes:
  Mirrors the sandboxed command's exit code.
  127 = sandbox runtime not found (install bubblewrap)

stdout/stderr are forwarded transparently from the sandboxed process.

Examples:
  kros sandbox run "python3 untrusted_script.py" --timeout 60 --no-network
  kros sandbox run "npm test" --memory 1g --cpu 2
  kros sandbox run "make build" --readonly""",
    },
    {
        "name": "kros log <query>",
        "summary": "Query the audit log. Every kros command is automatically recorded.",
        "body": """\
The audit system records every kros invocation (command, args, exit code,
timing, stdout/stderr preview) to ~/.kros/log/audit.jsonl.

Use `kros log` to search and filter past invocations. Useful for debugging
("what did I run?") and accountability ("what happened in this session?").

Examples:
  kros log                    Show recent entries
  kros log --last 20          Show last 20 entries""",
    },
]

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_markdown() -> str:
    """Render the full capability manifest as Markdown."""
    lines: list[str] = []
    lines.append(f"# kros — Agent Operating System (v{_VERSION})")
    lines.append("")
    lines.append(
        "You have access to `kros`, a CLI toolkit designed for LLM agents. "
        "Below is the complete reference for every available command."
    )
    lines.append("")
    lines.append(_GLOBAL_RULES)
    lines.append("## Commands")
    lines.append("")

    for i, cmd in enumerate(_COMMANDS):
        lines.append(f"### {cmd['name']}")
        lines.append(cmd["summary"])
        lines.append("")
        lines.append(cmd["body"])
        lines.append("")
        if i < len(_COMMANDS) - 1:
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


def _render_json() -> str:
    """Render the capability manifest as JSON."""
    data = {
        "version": _VERSION,
        "commands": [],
    }
    for cmd in _COMMANDS:
        data["commands"].append(
            {
                "signature": cmd["name"],
                "summary": cmd["summary"],
                "details": cmd["body"],
            }
        )
    return json.dumps(data, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

caps_app = typer.Typer(
    name="caps",
    help="Print the full capability manifest (for LLM agent system prompts).",
    no_args_is_help=False,
    add_completion=False,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@caps_app.callback(invoke_without_command=True)
def caps_cmd(
    format: str = typer.Option(
        "md",
        "--format",
        "-f",
        help="Output format: 'md' (Markdown, default) or 'json'.",
    ),
) -> None:
    """Output the full kros capability manifest.

    Designed to be called once by an agent framework on startup and injected
    into the LLM's system prompt. Contains every command, its flags, exit
    codes, and usage examples.
    """
    if format == "json":
        output = _render_json()
    elif format == "md":
        output = _render_markdown()
    else:
        typer.secho(
            f"Unknown format: {format!r}. Use 'md' or 'json'.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    sys.stdout.write(output)
    sys.stdout.write("\n")


def register(app: typer.Typer) -> None:
    """Entry point called from ``kros.cli``."""
    app.add_typer(caps_app, name="caps")
