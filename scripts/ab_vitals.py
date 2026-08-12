#!/usr/bin/env python3
"""A/B one token of the vitals line against the live model, without deploying.

Every prompt change so far has been shipped and then judged from the world an
hour later, which conflates the change with the city, the time of day, and the
restart it took to deploy it. This asks the model directly: same captured prompt,
same server, one token different, N samples each.

    python3 scripts/ab_vitals.py holding=      # drop the holding= token
    python3 scripts/ab_vitals.py 'earned=\\S+'  # any regex over the vitals line

Reports how many replies are trade solicitations, because that is the failure
being chased: the agent opened 30 threads across two hours with "Alan, I have
fresh ore. Do you have lumber or meat to trade?" and got nothing back, where the
hour before it had asked Jack about the lumber supply chain and 5 of 6 answered.
"""
import json
import re
import subprocess
import sys
import urllib.request

PROMPT_FILE = "/tmp/mcity_bench_prompt.txt"
PORT = 8000
N = 10
TRADE_WORDS = ("trade", "ore", "lumber", "crystal", "buying", "selling",
               "meat", "merchant", "sell", "buy")


def capture():
    out = subprocess.run(
        ["docker", "logs", "--since", "15m", "omegaclaw"],
        capture_output=True, text=True, timeout=120)
    text = (out.stdout or "") + (out.stderr or "")
    match = re.search(r"\(CHARS_SENT: \d+ PROMPT: (.*?)(?=\n2026-)", text, re.S)
    if not match:
        print("no prompt in the last 15m of logs")
        return None
    return match.group(1)


def ask(prompt):
    body = json.dumps({
        "model": "mcity-agent",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 120,
        "temperature": 0.8,
    }).encode()
    req = urllib.request.Request(
        f"http://localhost:{PORT}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.load(resp)
    return (payload["choices"][0]["message"].get("content") or "").strip()


def spoken_text(reply):
    """The sentence the agent would actually say, if this reply speaks."""
    match = re.search(r'mcity-speak\s+"?\S+\s+([^"\n)]{4,})', reply)
    return match.group(1).strip() if match else None


def score(label, prompt):
    said, spoke, trade = [], 0, 0
    for _ in range(N):
        try:
            reply = ask(prompt)
        except Exception as exc:      # noqa: BLE001
            print(f"  {label}: request failed - {exc}")
            continue
        text = spoken_text(reply)
        if not text:
            continue
        spoke += 1
        said.append(text)
        if any(word in text.lower() for word in TRADE_WORDS):
            trade += 1
    print(f"\n{label}: {spoke}/{N} replies speak, {trade} of those are trade talk")
    for line in said[:4]:
        print(f"    {line[:88]}")
    return spoke, trade


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    pattern = sys.argv[1]
    prompt = capture()
    if not prompt:
        return 1
    stripped = re.sub(r"\s" + pattern + r"\S*", "", prompt)
    if stripped == prompt:
        print(f"pattern {pattern!r} matched nothing in the prompt")
        return 1
    print(f"prompt {len(prompt)} chars; without {pattern!r}: {len(stripped)}")
    a_spoke, a_trade = score("WITH   ", prompt)
    b_spoke, b_trade = score("WITHOUT", stripped)
    print(f"\ntrade talk: {a_trade}/{max(a_spoke, 1)} with, "
          f"{b_trade}/{max(b_spoke, 1)} without")
    return 0


if __name__ == "__main__":
    sys.exit(main())
