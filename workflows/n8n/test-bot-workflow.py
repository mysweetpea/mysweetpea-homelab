#!/usr/bin/env python3
"""Structural checks for the SweetPea Telegram bot workflow.

Three bugs in one session came from the same two gaps: a command that routes
nowhere, and a reply whose text Telegram rejects at send time (parse_mode is
HTML, so a bare <placeholder> is read as a tag and the node dies *after* its
side effects — the command looks like it did nothing).

Usage: python3 test-bot-workflow.py ["SweetPea Telegram Bot (Complete).json"]
"""
import json
import re
import sys
from collections import deque
from pathlib import Path

WF = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("SweetPea Telegram Bot (Complete).json"))

TG = "n8n-nodes-base.telegram"
CODE = "n8n-nodes-base.code"
ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "blockquote"}
TAG = re.compile(r"<\s*/?\s*([A-Za-z][A-Za-z0-9_]*)[^>]*>")


def load():
    d = json.loads(WF.read_text(encoding="utf-8"))
    return d["nodes"], d["connections"]


def commands(nodes):
    pc = next(n for n in nodes if n["name"] == "Parse Command")["parameters"]["jsCode"]
    return [x.strip().strip("'\"") for x in re.search(r"const VALID = \[(.*?)\];", pc, re.S).group(1).split(",") if x.strip()]


def routes(nodes, conns, cmds):
    """command -> reachable node names (switch outputKey N sits on connection N+1)."""
    by = {n["name"]: n for n in nodes}
    out = conns["Route Command"]["main"]
    table = {}
    for r in by["Route Command"]["parameters"]["rules"]["rules"]:
        idx = int(r["outputKey"]) + 1
        table[r["value2"]] = out[idx][0]["node"] if idx < len(out) and out[idx] else None

    reach = {}
    for cmd, start in table.items():
        seen, q = [], deque([start])
        while q and start:
            name = q.popleft()
            if name in seen:
                continue
            seen.append(name)
            for branch in conns.get(name, {}).get("main", []) or []:
                for link in branch or []:
                    q.append(link["node"])
        reach[cmd] = seen
    return table, reach


def main():
    nodes, conns = load()
    by = {n["name"]: n for n in nodes}
    cmds = commands(nodes)
    table, reach = routes(nodes, conns, cmds)
    fails = []

    for c in cmds:
        if c not in table:
            fails.append(f"/{c}: accepted by Parse Command but has no Route Command rule")
        elif not table[c]:
            fails.append(f"/{c}: route rule points at no node")
        elif not any(by.get(n, {}).get("type") == TG for n in reach[c]):
            fails.append(f"/{c}: chain never reaches a Telegram node -> silent command")

    for r in table:
        if r not in cmds and r != "unknown":
            fails.append(f"/{r}: routed but not in Parse Command's VALID list")

    # reply text Telegram can parse: telegram nodes and code-node string literals
    for n in nodes:
        if n["type"] == TG:
            for m in TAG.finditer(n["parameters"].get("text") or ""):
                if m.group(1).lower() not in ALLOWED_TAGS and "&" not in (n["parameters"]["text"][max(0, m.start() - 5):m.start()]):
                    fails.append(f"{n['name']}: bare <{m.group(1)}> in reply text (Telegram rejects it)")
        if n["type"] == CODE:
            code = n["parameters"].get("jsCode", "")
            lits = re.findall(r"'((?:[^'\\]|\\.)*)'", code) + re.findall(r'"((?:[^"\\]|\\.)*)"', code) + re.findall(r"`((?:[^`\\]|\\.)*)`", code)
            for lit in lits:
                for m in TAG.finditer(lit):
                    if m.group(1).lower() not in ALLOWED_TAGS and not lit[:m.start()].endswith("&lt;"):
                        fails.append(f"{n['name']}: bare <{m.group(1)}> in a reply string")

    # the menu must list every command the bot accepts (start is an alias of help)
    help_text = by["Reply Help"]["parameters"]["text"]
    listed = set(re.findall(r"/([a-z]+)", help_text))
    for c in cmds:
        if c not in listed and c != "start":
            fails.append(f"/{c}: works but is missing from /help")

    if fails:
        print(f"FAIL ({len(fails)})")
        for f in fails:
            print("  -", f)
        return 1
    print(f"ok: {len(cmds)} commands routed, every chain replies, no unparsable tags, /help complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
