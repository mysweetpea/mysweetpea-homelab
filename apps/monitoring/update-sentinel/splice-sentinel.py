#!/usr/bin/env python3
"""Splice sentinel.py into values.yaml's embedded configMap (base indent 8).

The embedded copy is what the cluster runs; the standalone sentinel.py is what we
edit and test. This keeps them byte-identical. --check reports drift only.
"""
import sys, pathlib

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "sentinel.py"
VALUES = HERE / "values.yaml"
INDENT = " " * 8


def main() -> int:
    src = SRC.read_text()
    lines = VALUES.read_text().split("\n")
    start = None
    for i, l in enumerate(lines):
        if l.strip() == "sentinel.py: |":
            start = i + 1
            break
    if start is None:
        print("no 'sentinel.py: |' block in values.yaml", file=sys.stderr)
        return 2
    end = start
    while end < len(lines) and (lines[end].startswith(INDENT) or lines[end].strip() == ""):
        end += 1
    # keep the blank separator line that follows the block
    while end > start and lines[end - 1].strip() == "":
        end -= 1
    cur = "\n".join(l[len(INDENT):] if l.strip() else "" for l in lines[start:end]).rstrip("\n") + "\n"

    if "--check" in sys.argv:
        ok = cur == src
        print("embedded == sentinel.py" if ok else "DRIFT: embedded copy differs from sentinel.py")
        return 0 if ok else 1

    body = [INDENT + l if l.strip() else "" for l in src.rstrip("\n").split("\n")]
    new = lines[:start] + body + lines[end:]
    VALUES.write_text("\n".join(new))
    print(f"spliced {len(src.splitlines())} lines at values.yaml:{start + 1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
