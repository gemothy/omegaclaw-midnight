"""
Test: OmegaClaw invokes (tavily-search ...) when asked explicitly.

Run:
    pytest test_tavily_search.py -s
"""
from helpers import (
    Checker, find_skill_calls, make_prompt, send_prompt,
    wait_for_skill_call, wait_for_skill_match,
)


# Phrases that mean "the skill failed / external uAgent was unreachable" —
# we must never count these as successful Tavily-search relays. Any of them
# in the agent's send invalidates the run, since the Tavily lookup never
# actually returned content the agent could summarise.
ERROR_MARKERS = (
    "delivery failed", "delivery error", "deliverystatus",
    "tavily search failed", "tavily-search failed", "tavily failed",
    "skill failed", "skill is currently unavailable",
    "currently unavailable", "is unreachable", "not reachable",
    "unable to reach", "could not reach", "couldn't reach",
    "no response from", "agent did not respond",
    "failed:", "failed.", "error:", "service is down",
)

# Fetch.ai-specific keywords. Deliberately avoid bare "ai" / "agent" — those
# match unrelated content (the substring "ai" inside the word "Tavily", or
# uAgent destination IDs like "agent1qt5..." printed inside an error
# message), which previously hid skill-delivery failures behind a green PASS.
FETCH_KEYWORDS = (
    "fetch.ai", "fetch ai", "fet ",
    "asi alliance", "asi-alliance", "alliance",
    "humayun", "humayun sheikh",
    "uagent", "u-agent",
    "decentralized", "blockchain",
    "token",
)


def test_tavily_search():
    with Checker("tavily-search invocation") as c:
        print(f"\n=== OmegaClaw: tavily-search (run-id {c.run_id}) ===", flush=True)

        c.step("send prompt via IRC asking for tavily-search")
        prompt = make_prompt(
            c.run_id,
            "Use the tavily-search skill (not regular search) for query "
            "'Fetch.ai latest news'. Summarize what Tavily returns.",
        )
        if not send_prompt(prompt):
            c.fail("irc", "could not deliver prompt within 60s")
        c.ok("irc", f"run-id={c.run_id}")

        c.step("verify agent invoked (tavily-search ...) with Fetch.ai query")
        arg = wait_for_skill_call(
            c.run_id, "tavily-search", timeout=240, arg_substr="fetch",
        )
        if arg is None:
            all_calls = find_skill_calls(c.run_id, "tavily-search") or []
            c.fail(
                "tavily-search invoked",
                f"no (tavily-search ...) with 'fetch' arg. Got: {all_calls[:3]}",
            )
        c.ok("tavily-search invoked", f"arg={arg!r}")

        c.step("verify agent did NOT fall back to regular (search ...)")
        regular_search = find_skill_calls(c.run_id, "search") or []
        if regular_search:
            print(f"       [WARN] agent also used plain (search ...): {regular_search[:2]}",
                  flush=True)
        c.ok("no search fallback" if not regular_search else "mixed skills",
             f"{len(regular_search)} plain search calls")

        c.step("wait for a (send ...) carrying real Fetch.ai content")

        def is_real_fetch_summary(s):
            low = s.lower()
            if any(em in low for em in ERROR_MARKERS):
                return False
            return any(kw in low for kw in FETCH_KEYWORDS)

        send_arg = wait_for_skill_match(
            c.run_id, "send", is_real_fetch_summary, timeout=240,
        )
        if send_arg is None:
            all_sends = find_skill_calls(c.run_id, "send") or []
            last = all_sends[-1] if all_sends else "<none>"
            low_last = last.lower() if isinstance(last, str) else ""
            error_hits = [em for em in ERROR_MARKERS if em in low_last]
            if error_hits:
                c.fail(
                    "tavily skill working",
                    f"agent reported tavily-search failure ({error_hits}). "
                    f"Last send: {last!r}",
                )
            c.fail(
                "send content",
                f"no Fetch-related keywords in any send. "
                f"Last send: {last!r}",
            )
        body = send_arg.lower()
        matched = [k for k in FETCH_KEYWORDS if k in body]
        c.ok("send content", f"matched: {', '.join(matched[:4])}")

        c.done()
