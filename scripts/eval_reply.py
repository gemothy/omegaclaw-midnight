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

    python3 scripts/eval_reply.py [samples] [model] [prompt-cache-file]

Scores three things, in the order they matter:
  answered      a speak aimed at the id the line said was waiting

RE-BASELINED 2026-08-14 after the harness changes below, on a FRESH capture -
the vitals line materially changed (reachable= now agrees with waiting=), so the
older cached prompt no longer represents what the agent is shown:

    huginnfork/Qwen3.8-27B-NVFP4A16   40/40 answered  wrong-target 0  no-speak 0
    negative control (waiting=0)      spoke 1 in 20

So the model is NOT the bottleneck and there is no reason to change it. That
matters because the checkpoint is a community quant with single-digit downloads
and an official Qwen/Qwen3.8-27B-FP8 has since appeared: the official one is the
safer provenance, but this one is measured perfect on the metric that decides,
and swapping a 40/40 for an unmeasured checkpoint trades evidence for a feeling.
Swap only if something starts looking wrong, and re-run this first.

Everything that has moved the reply rate this session was the harness telling the
model who was waiting - never the model itself.

MODEL COMPARISON, 2026-08-14, 40 samples each, both scored against the SAME
cached prompt (third argument) so the question was identical:

    nvidia/Qwen3.6-35B-A3B-NVFP4  (MoE)    18/40  45%   wrong-target 0
    huginnfork/Qwen3.8-27B-NVFP4A16 (dense) 40/40 100%  wrong-target 0

and the negative control that makes 100% mean something rather than "this model
always speaks": the same prompt with waiting=0 put back, Qwen3.8-27B spoke 1 time
in 20. It answers the person waiting; it does not emit a message regardless.

Qwen3.8-27B is now the served model. It costs ~4.3 tok/s against ~62 for the MoE -
decode on a dense 27B is bandwidth-bound - which is affordable only because this
agent writes one short command per turn.

Use the prompt cache for any comparison. Scored on two separate captures the SAME
model gave 57% and 45%, so a candidate measured against a differently-captured
baseline is being compared partly on prompt luck.

BASELINE, 40 samples, 2026-08-14, nvidia/Qwen3.6-35B-A3B-NVFP4, fresh capture:

    answered 23 (57%)   wrong-target 0   no-speak 17

Compare future models against THAT number, not against the 85% below it. The 85%
was measured through a capture that read `docker logs --since 20m` on a daemon
four hours behind, took the FIRST prompt it found rather than the live one, and
then rewrote the mission text instead of the vitals line. Three defects, all in
the instrument, all fixed here. Same model, same day, measured through each
successive fix: 0%, then 12%, then 57%. Nothing about the model changed.

So 85% is not a number this eval can reproduce or compare to - it was taken on a
prompt nobody can now identify. It is left in the history below as a warning
rather than deleted, because deleting it would make the record look tidier than
it is.

The superseded figure, kept for that reason: 40 samples, 2026-08-13,
    answered 34 (85%)   wrong-target 0   no-speak 6

What has held across every version of this eval, and is the part worth keeping,
is that the replies use the name from who= rather than the one inside the quoted
message: "Holly, how is the crystal shipment holding up?". Against the 57%
baseline, anything materially below is a regression and anything above needs 40
samples of its own to believe.

The earlier small-sample history, kept because it shows why 40: the current
model answers roughly half to two thirds of the time, and the run-to-run spread is
large. Five runs of 10-14 samples each gave 42%, 100%, 90%, 30%, 60%. Two of those
had near-identical history composition (21 speaks / 4 reads versus 21 / 5) and
scored 9/10 and 3/10, so the swing is not explained by what the agent did lately -
it is sampling noise at this sample size.

Read that as: a single run of this eval decides nothing. Anything under about
forty samples cannot separate a prompt change from the noise, and the earlier "0%"
and "18%" figures in this file were injection bugs plus that same variance.

Results so far, same prompt, same harness, 2026-08-13:

  nvidia/Qwen3.6-35B-A3B-NVFP4   0/20 answered - emits (mcity-threads) every
                                 time, the two-step the mission was changed to
                                 stop
  nvidia/Qwen3-32B-FP4 (dense)   0/16 answered - speaks, but to somebody pulled
                                 out of its own history rather than the waiting
                                 id. Worse: the MoE at least takes a step toward
                                 answering

So the local alternative is not better, and the MoE stays.

Both of those were measured through the broken capture described above, so read
them as "not yet retested" rather than as settled. The dense model's wrong-target
result is the one worth re-checking first, since aiming at the wrong person is
the only failure mode here that is worse than saying nothing.

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
import os
import re
import sys
import urllib.request

PORT = 8000
WAITING_ID = "user-agent-eval-waiting-7f3a"
SAID = "Gem, did the crystal shipment clear customs tonight?"


def capture():
    """The LATEST prompt in a correctly-clocked window.

    Both halves of this were wrong and together they scored the live model 0/40.

    `docker logs --since 20m` is the trap README.md lists first: the daemon on
    this host runs about four hours behind it, so the window is not twenty
    minutes and what came back was a prompt from container boot - before any
    skill had returned a result, therefore carrying no vitals line at all.
    read_window filters on each line's own timestamp instead.

    re.search then took the FIRST prompt in whatever came back. The live prompt
    is the LAST one; the earlier ones are history.
    """
    sys.path.insert(0, "scripts")
    from dockerlogs import read_window                  # noqa: PLC0415
    text, error = read_window("omegaclaw", "30m", include_prompt=True)
    if error:
        return None
    found = re.findall(r"\(CHARS_SENT: \d+ PROMPT: (.*?)(?=\n2026-)",
                       text or "", re.S)
    return found[-1] if found else None


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
    # Patch the VITALS LINE, not the last textual waiting=0 anywhere in the
    # prompt. rfind("waiting=0") found the copy inside the mission text, which
    # quotes the token to explain the rule, and rewrote the rule itself into
    #
    #   "if vitals says waiting=1 (answer <id>) who=Holly ... then nobody who
    #    can hear you is owed a reply, so do NOT call mcity-threads"
    #
    # The model read that, correctly answered nobody, and scored 0 of 40 - on the
    # model that had just scored 85%. Fourth time this eval has measured its own
    # injection; the previous three are above. The line the agent acts on is the
    # LAST segment beginning "vitals " (mcity_client._out appends it to every
    # result), so that is the only thing that gets rewritten.
    segments = prompt.split("_newline_")
    live = [n for n, seg in enumerate(segments)
            if seg.strip().startswith("vitals ") and "waiting=" in seg]
    if not live:
        return None
    n = live[-1]
    seg = re.sub(r"waiting=\d+(?: \(answer \S+\))?", injected,
                 segments[n], count=1)
    # And take the competing target off the line. The harness suppresses talk-to=
    # whenever somebody is owed a reply - one person named per turn - so leaving
    # it in builds a vitals line the agent is never shown. The first run of this
    # eval did exactly that and scored the model 0 of 20, which measured my
    # injection rather than the model.
    # Only the COMPETING target goes. who= now belongs to the waiting person.
    # Scoped to the vitals line: applied to the whole prompt it also edited the
    # mission text.
    seg = re.sub(r"\s(?:talk-to|met-before|last)=\S+", "", seg)
    seg = re.sub(r"\swho=(?!Holly)\S+", "", seg)
    # reachable has to agree with waiting, for the same reason waiting has to
    # agree with answer=. _someone_is_waiting() counts only people who can
    # actually hear a reply, so waiting=1 alongside reachable=0 is a state the
    # harness cannot produce: it tells the model in one breath that Holly is
    # owed an answer and that nobody can hear it. Scored 12% while the capture
    # happened to carry reachable=0, and whether it does is pure luck of when
    # the prompt was taken - which made the number uncomparable to any other
    # run rather than merely low.
    if re.search(r"reachable=0\b", seg):
        seg = re.sub(r"reachable=0\b", "reachable=1", seg, count=1)
    segments[n] = seg
    return "_newline_".join(segments)


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
    # Optional third argument: a file to hold the injected prompt.
    #
    # Comparing two models means serving them one at a time, and a fresh capture
    # between the two runs is a fresh prompt - different history, different
    # hunger, different reachable - so the comparison silently includes a change
    # of question. Written on first use and reused after, which also means the
    # candidate can be scored while its own container is the one running and the
    # window no longer holds a prompt from the model being replaced.
    cache = sys.argv[3] if len(sys.argv) > 3 else None
    prompt = None
    if cache and os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            prompt = fh.read()
        print(f"reusing the prompt in {cache} - identical question for "
              f"every model scored against it")
    if prompt is None:
        prompt = capture()
        if not prompt:
            print("no prompt captured from the last 30m of logs")
            return 1
        prompt = with_waiting(prompt)
        if prompt and cache:
            with open(cache, "w", encoding="utf-8") as fh:
                fh.write(prompt)
            print(f"captured and saved the injected prompt to {cache}")
    if not prompt:
        # Refuse to score rather than score a prompt the agent never sees. A
        # capture with no vitals line is a boot-time prompt, and patching one
        # anyway is how this eval produced a 0/40 on a model that scores 85%.
        print("captured prompt carries no live vitals line - nothing the agent "
              "acts on to inject into. Refusing to score; try again once the "
              "agent has run a few turns.")
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
