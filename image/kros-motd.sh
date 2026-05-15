#!/bin/sh
# /etc/profile.d/kros.sh — Agent bootstrap: show capabilities on first shell.
#
# This file is copied into the Kros container image at build time.
# It fires ONCE per container lifecycle (flag in /tmp), printing a concise
# capability summary so that any LLM agent naturally discovers kros the
# first time it opens a shell — no prior knowledge required.
#
# Design: "主动推" channel of kros's dual-bootstrap mechanism.
# The "被动拉" channel is: `kros caps` or reading /etc/kros/system-prompt.md.

if [ ! -f /tmp/.kros-introduced ]; then
    cat <<'KROS_MOTD'

════════════════════════════════════════════════════════════════
  Kros — Agent Operating System

  You have 7 superpowers beyond raw bash:

    kros http      Structured HTTP client (semantic exit codes + JSON stdout)
    kros browse    Headless browser (navigate, click, fill, extract — 16 commands)
    kros file      Read any document (PDF, Word, Excel, images, audio → Markdown)
    kros memory    Persistent cross-session memory (remember / recall)
    kros shell     Audited shell execution (timeout, env, cwd)
    kros sandbox   Isolated execution (CPU, memory, network, filesystem limits)
    kros log       Query your audit trail

  Run `kros caps` for the complete reference with flags, exit codes, and examples.
  Or read /etc/kros/system-prompt.md

════════════════════════════════════════════════════════════════

KROS_MOTD
    touch /tmp/.kros-introduced
fi
