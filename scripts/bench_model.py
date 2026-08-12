#!/usr/bin/env python3
"""Compare a model against the agent's real prompt, not a synthetic one.

The model choice here has been reversed once already - a dense 32B replaced a MoE
that could not follow the procedure, then a newer MoE replaced the dense one when
the harness stopped asking the model to derive anything. Both decisions turned on
numbers taken from the SAME live prompt, and both times I gathered them by hand.
This makes the next comparison repeatable.

What it measures, in order of how much it has mattered:

  tok/s          decode speed, which is what actually costs. A 21k-char prompt
                 prefills in about 1.3s while a fifty-token reply took five
                 seconds on the dense model, and decode is bound by ACTIVE
                 parameters - which is what a MoE reduces.
  command rate   how often the reply is a usable mcity command rather than prose.
                 Filler was 65 of 96 decisions at its worst, so this is the
                 quality bar that matters more than any benchmark score.
  seconds        end to end, what the agent's cadence is made of.

Usage:
    python3 scripts/bench_model.py --capture          # grab the live prompt
    python3 scripts/bench_model.py                    # bench the running server
    python3 scripts/bench_model.py --n 8 --port 8001  # bench a candidate
"""
import argparse
import json
import re
import statistics
import sys
import time
import urllib.request

PROMPT_FILE = "/tmp/mcity_bench_prompt.txt"
SKILL_RE = re.compile(r"\(?\s*(mcity-[a-z-]+)")


def capture(window="10m"):
    sys.path.insert(0, "scripts")
    from dockerlogs import read_window
    text, err = read_window("omegaclaw", window)
    if err:
        raise SystemExit(f"could not read the agent log: {err}")
    match = re.search(r"\(CHARS_SENT: \d+ PROMPT: (.*?)(?=\n2026-)", text or "", re.S)
    if not match:
        raise SystemExit("no prompt found in the log window")
    with open(PROMPT_FILE, "w", encoding="utf-8") as handle:
        handle.write(match.group(1))
    print(f"captured {len(match.group(1))} chars -> {PROMPT_FILE}")


def bench(port, n, model):
    with open(PROMPT_FILE, encoding="utf-8") as handle:
        prompt = handle.read()
    rows = []
    for _ in range(n):
        body = {"model": model, "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}]}
        start = time.time()
        request = urllib.request.Request(
            f"http://localhost:{port}/v1/chat/completions",
            json.dumps(body).encode(), {"Content-Type": "application/json"})
        payload = json.load(urllib.request.urlopen(request, timeout=300))
        elapsed = time.time() - start
        usage = payload["usage"]
        reply = (payload["choices"][0]["message"].get("content") or "").strip()
        rows.append((elapsed, usage["completion_tokens"], bool(SKILL_RE.match(reply)), reply))
    secs = [r[0] for r in rows]
    speeds = [r[1] / max(r[0], 0.01) for r in rows]
    commands = sum(1 for r in rows if r[2])
    print(f"model      : {model} (port {port})")
    print(f"samples    : {n}")
    print(f"seconds    : median {statistics.median(secs):.2f}  min {min(secs):.2f}  max {max(secs):.2f}")
    print(f"tok/s      : median {statistics.median(speeds):.1f}")
    print(f"command    : {commands}/{n} replies start with an mcity skill")
    for _, _, ok, reply in rows[:3]:
        print(f"   {'OK ' if ok else 'no '} {reply[:72]!r}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", action="store_true", help="refresh the prompt from the live agent")
    parser.add_argument("--n", type=int, default=5)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model", default="mcity-agent")
    args = parser.parse_args()
    if args.capture:
        capture()
    bench(args.port, args.n, args.model)
