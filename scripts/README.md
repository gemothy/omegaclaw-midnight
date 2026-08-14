# scripts/

Every one of these exists because something was silently wrong and the test suite
was green while it was. They are not general utilities; each answers a question
that a passing suite could not.

Run `check_all.py` first. The rest are for when it says something is off.

## The live checks

| script | the question it answers | why it exists |
|---|---|---|
| `check_all.py` | all of the below, in a safe order | four checks kept as separate scripts meant running them from memory, and forgetting one is how three defects survived |
| `can_it_act.py` | is the agent able to act at all, and when did anybody last write to it? | a whole pass was spent A/B testing prompts against a harness whose lease had been taken 50 minutes earlier. Silence from a dead agent looks exactly like silence from a quiet city |
| `check_world_contract.py` | does the world still send the fields we read? | `canStartConversation` was TRUE one day and absent two days later; the harness fell back to a worse rule and nothing failed |
| `check_reply_path.py` | would a real inbound message be noticed? | the waiting list was filtered on `threadStatus`, which excluded every thread this world returns, and `waiting=` read 0 in 885 consecutive samples |
| `check_escape.py` | would a room that refuses us offer a way out? | the agent sat in one building for over an hour; the escape chain had three separate broken links and each fix was masked by a deploy restart |
| `reply_funnel.py` | of the people who wrote to us and got no answer, WHERE did the reply die? | "we answer 31%" was read three ways in one session - a reply-path defect, the do-not-disturb ceiling, or the model declining - and they imply different work. It walks each inbound thread and asks the log what happened to that specific id. First run: 11 of 13 died on OUR side, not the world's |
| `check_mechanisms.py` | which parts of the harness have left a trace lately? | two mechanisms were dead in production for days while passing their unit tests, because the tests populate the state by hand |

## The measurements

| script | what it measures |
|---|---|
| `conversation_trend.py` | threads by the hour: who opened, who answered, how many went two-way. Every social judgement before this came from eight-minute windows that swung 1 → 8 → 3 → 0 |
| `eval_reply.py` | the model on the one metric we control - does it answer the person it is told is waiting. Baseline **85% of 40 samples**. Runs under ~40 samples cannot separate a change from the noise |
| `ab_vitals.py` | one token of the vitals line, on and off, against the live model |
| `bench_model.py` | decision latency and tokens/sec on the real prompt |
| `eval_grounding.py` | whether the agent's `send` claims to the operator match what the world actually recorded |
| `measure_loop_health.py` | whether the loop is doing useful work or spinning on reads |
| `dockerlogs.py` | the log reader everything else uses |

## Three ways these tools have lied

Worth knowing before trusting a number out of them:

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
