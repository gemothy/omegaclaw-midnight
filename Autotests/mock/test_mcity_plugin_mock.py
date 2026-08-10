"""
Mock tests for the Midnight City plugin.

The plugin (plugins/mcity) registers its skills from MeTTa at load time and
implements them in Python. These tests cover the wiring that unit tests cannot
see, without touching the live world: no lease is taken and no world changing
skill is ever called here, because Midnight City is shared with other people
and their agents and the operator holds the real control lease.

  - command recogniser: every registered skill name must be in
                  src/helper.py:LLM_COMMANDS. A plugin skill missing from that
                  set is not treated as the start of a command line, so in a
                  multi command turn it is silently swallowed into the previous
                  command's argument and never runs.
  - parse table:  the exact balance_parentheses output for the shapes an LLM
                  actually emits, including the two command turn that only
                  parses correctly once the names above are registered.
  - registration: the running container logs one `Add skill:` line per
                  observation skill and injects the mcity_rules prompt
                  extension, so the skills really are in the SKILLS block.
  - execution:    a scripted turn calls mcity-status, the read only skill, and
                  the agent loop reports an MCITY-STATUS-OK result.

Run:
    pytest test_mcity_plugin_mock.py -s
"""
import os
import subprocess
import sys
import time

from helpers import CONTAINER, Checker, make_prompt, wait_for_history_keyword

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _path in (os.path.join(_REPO, "src"), os.path.join(_REPO, "plugins", "mcity")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import helper                # noqa: E402  (src/helper.py)
import mcity_client          # noqa: E402  (plugins/mcity/mcity_client.py)

# Exactly as src/skills.metta:53 renders it: "- <description>: <function> <args>".
# A zero argument skill therefore ends with a trailing space.
EXPECTED_SKILL_LINES = [
    "- Report your Midnight City connection and control status: mcity-status ",
    "- Look around in Midnight City and see where you are and what you are doing: mcity-context ",
    "- List the items you carry in Midnight City: mcity-inventory ",
    "- Check your hunger energy and other needs in Midnight City: mcity-needs ",
    "- List the areas you can move to from here in Midnight City: mcity-areas ",
    "- List the other agents near you in Midnight City: mcity-agents ",
    "- List districts you can travel to and buildings you can enter in Midnight City: mcity-navigation ",
    "- List Midnight City merchants with the items they buy and pay: mcity-merchants ",
    "- Read what recently happened around you in Midnight City: mcity-recent-events ",
    "- List your Midnight City conversation threads: mcity-threads nothing",
    "- Read the messages of one Midnight City conversation thread: mcity-thread thread_id_in_quotes",
]

PARSE_TABLE = [
    ("mcity-context", "((mcity-context))"),
    ("(mcity-context)", "((mcity-context))"),
    ('(mcity-speak "agent-123 hello there friend")',
     '((mcity-speak "agent-123 hello there friend"))'),
    ("mcity-speak agent-123 hello there friend",
     '((mcity-speak "agent-123 hello there friend"))'),
    ('(mcity-move-tile "12 34")', '((mcity-move-tile "12 34"))'),
    ("mcity-context\nmcity-agents", "((mcity-context) (mcity-agents))"),
    ("send hi\nmcity-context", '((send "hi") (mcity-context))'),
    ("mcity-harvest area-7 mine ore\nmcity-recent-events",
     '((mcity-harvest "area-7 mine ore") (mcity-recent-events))'),
]


def _flush(comm):
    """Drop any messages left in the shared queue by earlier turns/tests."""
    while comm.getLastMessage():
        pass


def _container_log():
    result = subprocess.run(["docker", "logs", CONTAINER],
                            capture_output=True, text=True)
    return (result.stdout or "") + (result.stderr or "")


def _wait_for_log(needle, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in _container_log():
            return True
        time.sleep(3)
    return False


class TestMcityPlugin:

    def test_skill_names_start_a_command_line(self):
        with Checker("mcity skill names are recognised commands") as c:
            missing = sorted(mcity_client.SKILL_NAMES - set(helper.LLM_COMMANDS))
            if missing:
                c.fail("LLM_COMMANDS",
                       f"missing from src/helper.py: {missing}; a multi command "
                       "turn would silently swallow these into the previous "
                       "command's argument")
            c.ok("LLM_COMMANDS", f"{len(mcity_client.SKILL_NAMES)} names registered")

            for skill in sorted(mcity_client.SKILL_NAMES):
                if not helper.starts_command_line(skill):
                    c.fail("starts_command_line", f"{skill} is not a command start")
            c.ok("starts_command_line", "every mcity skill starts a command line")
            c.done()

    def test_llm_output_shapes_parse(self):
        with Checker("mcity command shapes parse as the model writes them") as c:
            for source, expected in PARSE_TABLE:
                got = helper.balance_parentheses(source)
                if got != expected:
                    c.fail("balance_parentheses",
                           f"{source!r} parsed as {got!r}, expected {expected!r}")
            c.ok("balance_parentheses", f"{len(PARSE_TABLE)} shapes parse correctly")
            c.done()

    def test_skills_registered_in_the_running_agent(self):
        with Checker("mcity skills reach the SKILLS block (mock)") as c:
            print("\n=== OmegaClaw: mcity skill registration ===", flush=True)
            if not _wait_for_log("MCITY-STARTUP-", timeout=60):
                c.fail("startup", "the plugin never logged its startup line; "
                                  "a failed MeTTa import is silent, so this is "
                                  "the only positive proof it loaded")
            log = _container_log()
            c.ok("startup", "plugin startup line present")

            if "Add skill: " not in log:
                c.fail("add-skill", "the container logged no skill registration at all")
            for line in EXPECTED_SKILL_LINES:
                if line not in log:
                    c.fail("add-skill", f"missing skill registration: {line!r}")
            c.ok("add-skill", f"{len(EXPECTED_SKILL_LINES)} observation skills registered")

            if "Add prompt extension mcity_rules" not in log:
                c.fail("prompt extension", "mcity_rules was not injected into the prompt")
            c.ok("prompt extension", "mcity_rules injected")

            # read mode is the default: no lease may ever be taken implicitly
            if "- Trade with an allowed merchant" in log:
                c.fail("trade gating", "the trade skill was registered without an "
                                       "operator merchant allowlist")
            c.ok("trade gating", "trade skill absent without an allowlist")
            c.done()

    def test_status_skill_runs(self, llm, comm):
        with Checker("mcity-status runs in the agent loop (mock)") as c:
            print(f"\n=== OmegaClaw: mcity-status (run-id {c.run_id}) ===", flush=True)
            c.add_cleanup_marker(str(c.run_id))
            _flush(comm)

            c.step("turn 1: ask for the Midnight City status")
            prompt = make_prompt(c.run_id, "Report your Midnight City connection status.")
            llm.set_answer(prompt, "(mcity-status)")
            if not comm.send_message(prompt):
                c.fail("comm-1", "could not deliver the turn 1 prompt within 60s")

            if wait_for_history_keyword(c.run_id, ["mcity-status"]) is None:
                c.fail("skill called", "the agent never invoked mcity-status")
            c.ok("skill called", "mcity-status invoked")

            if not _wait_for_log("MCITY-STATUS-OK", timeout=90):
                c.fail("skill result",
                       "the loop never reported an MCITY-STATUS-OK result")
            c.ok("skill result", "MCITY-STATUS-OK returned to the agent")
            c.done()
