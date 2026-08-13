#!/usr/bin/env python3
"""Score a model on the one thing this agent is judged by: answering people.

Measured 2026-08-13, the city has a conversation ceiling - 15 threads we opened
drew 0 replies, and none of the 3 inbound threads we answered were ever followed
up. Nobody here sends a second message. So the only social metric the harness can
move is whether it answers the people who write first, and the only reason to
change models is if a different one answers better.

Live data cannot settle that: inbound runs at a few threads an hour and some
windows have none at all, which is how a whole pass once got spent measuring a
lease outage. This takes the real captured prompt, injects a waiting person into
the vitals line, and asks the model N times whether it writes back to THAT id.

    python3 scripts/eval_reply.py [samples] [model]

Scores three things, in the order they matter:
  answered      a speak aimed at the id the line said was waiting

Results so far, same prompt, same harness, 2026-08-13:

  nvidia/Qwen3.6-35B-A3B-NVFP4   0/20 answered - emits (mcity-threads) every
                                 time, the two-step the mission was changed to
                                 stop
  nvidia/Qwen3-32B-FP4 (dense)   0/16 answered - speaks, but to somebody pulled
                                 out of its own history rather than the waiting
                                 id. Worse: the MoE at least takes a step toward
                                 answering

So the local alternative is not better, and the MoE stays.

CAVEAT, added after the fact: this samples ONE turn, and the agent works across
turns. The MoE answering with (mcity-threads) is not necessarily a failure - it
may be reading the thread and replying next turn, which live logs have measured
at 25 of 45 answered within four turns. So "0/20" is a compliance score, not an
outcome, and only the dense model's wrong-target result is unambiguously bad.
Treat this eval as a comparison between models, not as a verdict on either
  wrong-target  a speak aimed at somebody else - worse than silence, it burns
                the write and leaves the person unanswered
  no-speak      no message at all
"""
import json
import re
import subprocess
import sys
import urllib.request

PORT = 8000
WAITING_ID = "user-agent-eval-waiting-7f3a"
SAID = "Gem, did the crystal shipment clear customs tonight?"


def capture():
    out = subprocess.run(["docker", "logs", "--since", "20m", "omegaclaw"],
                         capture_output=True, text=True, timeout=180)
    text = (out.stdout or "") + (out.stderr or "")
    match = re.search(r"\(CHARS_SENT: \d+ PROMPT: (.*?)(?=\n2026-)", text, re.S)
    return match.group(1) if match else None


def with_waiting(prompt):
    """Put a live waiting person on the vitals line, exactly as the harness does.

    Replaces waiting=0 rather than appending, so the line stays self-consistent -
    an agent told both waiting=0 and answer=<id> is being asked a trick question
    and its answer would not mean anything.
    """
    # Exactly what the harness emits for a waiting person: the id, WHO they are,
    # and their words. who= used to belong only to talk-to= and was stripped
    # below as a competing target; it now names the person being answered, and
    # stripping it removed the one thing that stops the agent addressing the
    # reply to itself.
    injected = (f"waiting=1 (answer {WAITING_ID}) who=Holly,hacker "
                f"they-said=<<MC_UNTRUSTED {SAID} MC_UNTRUSTED>>")
    # The LAST waiting=0, not the first. The prompt carries dozens of old vitals
    # lines inside HISTORY, so patching the first one left the live line - the
    # only one the agent acts on - still reading waiting=0. The model then
    # correctly declined to answer nobody and scored 0 of 16, which I read as a
    # model failure twice before checking what the prompt actually said.
    #
    # Third time this eval has measured its own injection. It is a reminder that
    # a harness for measuring a model needs the same scepticism as the model.
    idx = prompt.rfind("waiting=0")
    if idx < 0:
        return None
    out = prompt[:idx] + injected + prompt[idx + len("waiting=0"):]
    # And take the competing target off the line. The harness suppresses talk-to=
    # whenever somebody is owed a reply - one person named per turn - so leaving
    # it in builds a vitals line the agent is never shown. The first run of this
    # eval did exactly that and scored the model 0 of 20, which measured my
    # injection rather than the model.
    # Only the COMPETING target goes. who= now belongs to the waiting person.
    out = re.sub(r"\s(?:talk-to|met-before|last)=\S+", "", out)
    out = re.sub(r"\swho=(?!Holly)\S+", "", out)
    return out


def ask(prompt, model):
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 120, "temperature": 0.7}).encode()
    req = urllib.request.Request(
        f"http://localhost:{PORT}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = json.load(resp)
    return (payload["choices"][0]["message"].get("content") or "").strip()


def main():
    samples = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    model = sys.argv[2] if len(sys.argv) > 2 else "mcity-agent"
    prompt = capture()
    if not prompt:
        print("no prompt captured from the last 20m of logs")
        return 1
    prompt = with_waiting(prompt)
    if not prompt:
        print("captured prompt had no waiting=0 to replace; try again shortly")
        return 1

    answered = wrong = silent = 0
    examples = []
    for _ in range(samples):
        try:
            reply = ask(prompt, model)
        except Exception as exc:      # noqa: BLE001
            print(f"  request failed: {exc}")
            continue
        targets = re.findall(r'mcity-speak\s+"?(\S+)', reply)
        if not targets:
            silent += 1
        elif targets[0].startswith(WAITING_ID):
            answered += 1
            if len(examples) < 3:
                said = re.search(r'mcity-speak\s+"?\S+\s+([^"\n)]{4,})', reply)
                examples.append(said.group(1)[:78] if said else "")
        else:
            wrong += 1

    done = answered + wrong + silent
    print(f"\n{model}: {done} samples")
    print(f"  answered      {answered:3}  ({100 * answered // max(done, 1)}%)")
    print(f"  wrong-target  {wrong:3}")
    print(f"  no-speak      {silent:3}")
    for line in examples:
        print(f"    > {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
