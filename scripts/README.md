# scripts/

Every one of these exists because something was silently wrong and the test suite
was green while it was. They are not general utilities; each answers a question
that a passing suite could not.

Run `check_all.py` first. The rest are for when it says something is off.

## The live checks

| script | the question it answers | why it exists |
|---|---|---|
| `check_all.py` | all of the below, in a safe order | four checks kept as separate scripts meant running them from memory, and forgetting one is how three defects survived |
| `check_deployed.py` | is the code in the container the code in this tree? | three passes of fixes were committed, "deployed" and verified against production while the launcher ran a three-hour-old image - it only ever did `docker run`, never a build. A stale deploy looks exactly like a fix that did not help |
| `can_it_act.py` | is the agent able to act at all, and when did anybody last write to it? | a whole pass was spent A/B testing prompts against a harness whose lease had been taken 50 minutes earlier. Silence from a dead agent looks exactly like silence from a quiet city |
| `check_world_contract.py` | does the world still send the fields we read? | `canStartConversation` was TRUE one day and absent two days later; the harness fell back to a worse rule and nothing failed |
| `check_reply_path.py` | would a real inbound message be noticed? | the waiting list was filtered on `threadStatus`, which excluded every thread this world returns, and `waiting=` read 0 in 885 consecutive samples |
| `check_escape.py` | would a room that refuses us offer a way out? | the agent sat in one building for over an hour; the escape chain had three separate broken links and each fix was masked by a deploy restart |
| `reply_funnel.py` | of the people who wrote to us and got no answer, WHERE did the reply die? | "we answer 31%" was read three ways in one session - a reply-path defect, the do-not-disturb ceiling, or the model declining - and they imply different work. It walks each inbound thread and asks the log what happened to that specific id. First run: 11 of 13 died on OUR side, not the world's |
| `check_outbound_quality.py` | what did we actually say to real people, and was any of it harness internals? | everything else measures whether a message arrived, nothing checked what was in it. Sampling the LOG for sent text showed three serious-looking leaks that had never been sent - drafts the harness refused, and the model narrating results back into the log. It reads the world's record instead |
| `check_operator_channel.py` | can the operator reach the agent, and does the agent speak unbidden? | the channel that reaches a real person's phone, and nothing checked it. Found the agent emitting `(send "is forbidden on this turn...")` - prose executed because a line runs as whatever its first word names. It reached nobody only because no chat id is configured, which is an accident of setup, not a guard |
| `check_mechanisms.py` | which parts of the harness have left a trace lately? | two mechanisms were dead in production for days while passing their unit tests, because the tests populate the state by hand |

## Is it working?

`state_of_play.py` - one comparable snapshot, fixed windows, world-sourced.
`check_all` answers "is anything broken"; this answers "is it working", which is
what decides whether a pass was worth running. Quoting a 45m window one pass and
90m the next made every trend unreadable, which is why the windows are fixed.

Five runs, 2026-08-15, answering over 1h / 3h / all held:

    42% / 38% / 29%      -> 44 / 50 / 34 -> 70 / 52 / 37 -> 83 / 56 / 39
    -> 50 / 56 / 38

and speak reach 10% -> 11% -> 14% -> 16% -> 31%. The jump came with the agent
standing in central rather than a room where speaking almost never worked.

WHERE the agent stands turned out to matter more than anything else measured:
one window had central reaching 12 of 26 speaks and hacker-house-interior 4 of
54. That line is printed every run now and left to accumulate, because one window
is not a property of a room and this project has drawn confident wrong
conclusions from single samples before.

## The measurements

| script | what it measures |
|---|---|
| `conversation_trend.py` | threads by the hour: who opened, who answered, how many went two-way. Every social judgement before this came from eight-minute windows that swung 1 → 8 → 3 → 0 |
| `eval_reply.py` | the model on the one metric we control - does it answer the person it is told is waiting. Baseline **85% of 40 samples**. Runs under ~40 samples cannot separate a change from the noise |
| `ab_vitals.py` | one token of the vitals line, on and off, against the live model |
| `bench_model.py` | decision latency and tokens/sec on the real prompt |
| `eval_grounding.py` | whether the agent's `send` claims to the operator match what the world actually recorded |
| `measure_loop_health.py` | whether the loop is doing useful work or spinning on reads |
| `measure_opener_targeting.py` | does opening with the right person pay, and how long does "they wrote to us" stay good? | the three tiers and the 1h TTL were reasoned about and never checked against an outcome. They pay ~2.8x - and the TTL turned out to be right, which is why it was not extended |
| `measure_dnd_recovery.py` | how long after the world refuses a send does that same person accept one? Sets the dnd backoff base, and answered "should we retry fast inside the 60s thread window" with no |
| `dockerlogs.py` | the log reader everything else uses |

## Four ways these tools have lied

Worth knowing before trusting a number out of them:

0. **Inferring behaviour from log patterns.** Use `dockerlogs.count_results`,
   which counts only real `MCITY-<VERB>-<TAG>` result lines - grepping for a
   command name counts the model narrating its last result, `do-THIS=` hints and
   HISTORY replays alongside the one time it ran. Three separate wrong conclusions
   Four wrong findings this way: replies the world had refused filed as "never attempted" (the
   pattern read 120 characters after the id, missing anything before it); "26
   prose lines" that was really 41, because it matched one arbitrary opener; and
   "29% of turns produce no command", which required a quoted argument and so
   missed every call to `mcity-threads`, `mcity-agents` and `mcity-navigation`.
   And "34 walks into a bedroom", which was 30 move results of which 8
   confirmed. Each looked like a finding and each was a regex. Where the world
   knows the answer - thread lists, `recipientMessageCount` - ask the world;
   where only the harness knows, count results, not mentions.
1. **Counting the prompt.** The mission text names most of the tokens worth
   counting, and is echoed into the log every turn. `they-said=` once read 658
   where the truth was 0. `dockerlogs.read_window` now strips the prompt by
   default; pass `include_prompt=True` if you genuinely want it.
2. **The wrong window.** The Docker daemon on this host runs four hours behind
   it, so `docker logs --since 20m` is not twenty minutes. Use
   `dockerlogs.read_window`, which filters on each line's own timestamp.
3. **Fixtures the world never produces.** `check_reply_path` modelled a thread
   20 seconds old and passed for several passes while every real inbound message
   was being discarded - the world publishes them later than that. Fixture
   parameters have to come from observation, not from what sounds reasonable.
