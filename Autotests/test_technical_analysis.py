"""
Test: OmegaClaw invokes (technical-analysis "AAPL") via the external agent.

Run:
    pytest test_technical_analysis.py -s
"""
from helpers import (
    Checker, find_skill_calls, make_prompt, send_prompt,
    wait_for_skill_call, wait_for_skill_match,
)

TICKER = "AAPL"


# Phrases that mean the technical-analysis uAgent did not actually return
# data. Treat these in the agent's send as failure, NOT success — otherwise
# a downed external skill is masked by a passing test.
ERROR_MARKERS = (
    "delivery failed", "delivery error", "deliverystatus",
    "technical-analysis failed", "technical analysis failed",
    "ta failed", "ta-failed", "skill failed",
    "skill is currently unavailable", "currently unavailable",
    "is unreachable", "not reachable", "unable to reach",
    "could not reach", "couldn't reach",
    "no response from", "agent did not respond",
    "failed:", "failed.", "error:", "service is down",
)

# Real TA-summary keywords. Bare "buy"/"sell" alone is too soft; require at
# least one indicator-name (rsi, macd, sma, ema, ...) OR an explicit signal
# phrase ("buy signal", "bullish", etc.) — and the ticker itself.
TA_INDICATORS = (
    "rsi", "macd", "sma", "ema", "wma", "dema", "tema", "kama",
    "stochastic", "willr", "adx", "atr",
    "moving average", "momentum",
    "bullish", "bearish",
    "buy signal", "sell signal", "strong buy", "strong sell",
    "support level", "resistance level",
    "trend", "indicator",
)


def test_technical_analysis():
    with Checker(f"technical-analysis {TICKER}") as c:
        print(f"\n=== OmegaClaw: technical-analysis {TICKER} (run-id {c.run_id}) ===",
              flush=True)

        c.step("send prompt via IRC")
        prompt = make_prompt(
            c.run_id,
            f"Use the technical-analysis skill to get technical analysis for "
            f"ticker {TICKER}. Summarize in one line.",
        )
        if not send_prompt(prompt):
            c.fail("irc", "could not deliver prompt within 60s")
        c.ok("irc", f"run-id={c.run_id}")

        c.step(f"verify agent invoked (technical-analysis ...) with {TICKER}")
        arg = wait_for_skill_call(
            c.run_id, "technical-analysis", timeout=240, arg_substr=TICKER,
        )
        if arg is None:
            all_calls = find_skill_calls(c.run_id, "technical-analysis") or []
            c.fail(
                "TA invoked",
                f"no (technical-analysis \"{TICKER}\"). Got: {all_calls[:3]}",
            )
        if arg.upper() != TICKER:
            c.fail("TA ticker", f"called with {arg!r}, expected {TICKER}")
        c.ok("TA invoked", f"arg={arg!r}")

        c.step("wait for a (send ...) carrying real TA indicator summary")

        def is_real_ta_summary(s):
            low = s.lower()
            if any(em in low for em in ERROR_MARKERS):
                return False
            mentions_ticker = (
                TICKER.lower() in low or "apple" in low
            )
            mentions_indicator = any(ind in low for ind in TA_INDICATORS)
            return mentions_ticker and mentions_indicator

        send_arg = wait_for_skill_match(
            c.run_id, "send", is_real_ta_summary, timeout=240,
        )
        if send_arg is None:
            all_sends = find_skill_calls(c.run_id, "send") or []
            last = all_sends[-1] if all_sends else "<none>"
            low_last = last.lower() if isinstance(last, str) else ""
            error_hits = [em for em in ERROR_MARKERS if em in low_last]
            if error_hits:
                c.fail(
                    "TA skill working",
                    f"agent reported technical-analysis failure ({error_hits}). "
                    f"Last send: {last!r}",
                )
            c.fail(
                "send content",
                f"no TA indicators or ticker mention in any send. "
                f"Last send: {last!r}",
            )
        body = send_arg.lower()
        matched = [ind for ind in TA_INDICATORS if ind in body]
        c.ok("send content", f"matched indicators: {', '.join(matched[:5])}")

        c.done()
