import json
import logging
import os
import re
import subprocess
from collections import deque
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

try:
    from src.logger import get_logger
except ModuleNotFoundError:  # running this file directly as a script
    from logger import get_logger

logger = get_logger(__name__)

TS_RE = re.compile(r'^\("(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"')
LLM_COMMANDS = {
    "append-file",
    "episodes",
    "metta",
    "pin",
    "query",
    "read-file",
    "remember",
    "search",
    "send",
    "shell",
    "tavily-search",
    "technical-analysis",
    "version",
    "write-file",
    "get-io-policy",
    "write-file-b64",
    # Midnight City plugin (plugins/mcity). A skill name missing from this set
    # is not recognised as the start of a command line, so in a multi command
    # turn it is silently swallowed into the previous command's argument.
    "mcity-status", "mcity-context", "mcity-inventory", "mcity-needs",
    "mcity-areas", "mcity-agents", "mcity-navigation", "mcity-merchants",
    "mcity-recent-events", "mcity-threads", "mcity-thread",
    "mcity-move-area", "mcity-move-agent", "mcity-move-tile",
    "mcity-travel-district", "mcity-enter-building", "mcity-exit-building",
    "mcity-work", "mcity-eat", "mcity-sleep", "mcity-harvest",
    "mcity-speak", "mcity-trade",
}
TWO_ARG_COMMANDS = {
    "write-file",
    "append-file",
    "write-file-b64"
}

def extract_timestamp(line):
    m = TS_RE.search(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError as e:
        logger.error(f"Line does not carry a parsable timestamp: {e}")
        return None

def around_time(needle_time_str, k):
    needle_time_str = needle_time_str.replace(r'\"', '').replace('"', '').strip()
    filename = "repos/OmegaClaw-Core/memory/history.metta"
    target = datetime.strptime(needle_time_str, "%Y-%m-%d %H:%M:%S")
    best_lineno = None
    best_line = None
    best_diff = None
    buffer = []
    best_idx = None
    with open(filename, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            buffer.append((lineno, line))
            ts = extract_timestamp(line)
            if ts is None:
                continue
            diff = abs((ts - target).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_lineno = lineno
                best_line = line
                best_idx = len(buffer) - 1
    if best_lineno is None:
        return
    start = max(0, best_idx - k)
    end = min(len(buffer), best_idx + k + 1)
    ret = ""
    for lineno, line in buffer[start:end]:
        ret += f"{lineno}:{line}"
    return ret

def strip_redundant_quotes(x):
    """Undo the model wrapping an argument that is already quoted.

    Measured live: 51 usable decisions against 65 syntax errors in one hour -
    more than half of every turn discarded before anything ran. Every failure
    was the whole argument double-wrapped, often with trailing debris:

        pin "\"status: idle\""     pin "\"status: idle\")"     pin "\"status: idle\"))

    Those reach MeTTa unparseable and the turn is lost to punctuation.

    Deliberately narrow. It fires ONLY when the entire argument is wrapped in an
    escaped quote pair, because upstream guarantees that inner quotes are
    meaningful and must survive: `send "hello" world` keeps its quotes, and so
    does multi-line prose. Anything else is returned untouched."""
    if not isinstance(x, str):
        return x
    text = x.strip()
    # Must open with a quote immediately followed by an ESCAPED quote.
    if not text.startswith('"\\"'):
        return x
    inner = text[1:]                     # drop the real opening quote
    close = inner.rfind('\\"')           # the escaped closing quote
    if close <= 0:
        return x
    content = inner[2:close]             # between the two escaped quotes
    if '\\"' in content:                 # more than a simple wrapper: leave it
        return x
    return '"' + content + '"'


def quote_arg(x):
    if x.startswith('"') and x.endswith('"') and "\n" not in x:
        return x
    else:
        return json.dumps(x, ensure_ascii=False)

def starts_command_line(line):
    s = line.lstrip()
    if not s:
        return False
    # allow "(send ...)" as command start too
    if s.startswith("("):
        s = s[1:].lstrip()
    if not s:
        return False
    first = s.split(maxsplit=1)[0].rstrip(")")
    return first in LLM_COMMANDS

def split_command_blocks(s):
    blocks = []
    cur = []
    for raw in s.splitlines():
        if not raw.strip():
            if cur:
                cur.append(raw)
            continue
        if starts_command_line(raw) and cur:
            blocks.append("\n".join(cur).strip())
            cur = [raw]
        else:
            cur.append(raw)
    if cur:
        blocks.append("\n".join(cur).strip())
    return blocks

def split_toplevel_groups(text):
    """Split "(a ...) (b ...)" into its top-level groups, honouring quotes.

    The model puts several commands on ONE line inside an extra paren wrapper:

        ((send "No new user input. Checking status.") (mcity-threads))

    split_command_blocks divides on NEWLINES, so all of that arrived as a single
    command whose argument swallowed the rest of the line. One extra wrapper is
    peeled, then the groups inside are separated. Returns [text] unchanged when
    there is nothing to split, so well-formed single commands are untouched."""
    t = (text or "").strip()
    if not t.startswith("("):
        return [text]
    # The extra wrapper is optional. Both of these arrive from the model:
    #   ((send "...") (mcity-threads))     wrapped
    #   (mcity-threads) (mcity-work)       bare
    # Requiring the wrapper missed the bare form, which then parsed as
    #   (mcity-threads) "(mcity-work"
    # - the second command swallowed as a quoted argument of the first.
    if t.startswith("((") and t.endswith("))"):
        inner = t[1:-1].strip()
    else:
        inner = t
    groups, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(inner):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and start is not None:
                groups.append(inner[start:i + 1])
                start = None
            elif depth < 0:
                return [text]
    # One group counts too: ((pin "x")) must peel to (pin "x"), otherwise the
    # single remaining layer leaves "(pin" as the command name and the form is
    # rejected just as surely as the multi-command case.
    if depth or not groups:
        return [text]
    return groups


def balance_parentheses(s):
    s = s.replace("_quote_", '"').replace("_newline_", "\n")
    # Diagnostic AFTER the substitution: the model writes _quote_, not a literal
    # quote, so checking before this line captured nothing while the same run
    # produced 15 parse errors. Three repair attempts were made against forms
    # INFERRED from the post-parse error text; this logs the real input.
    if isinstance(s, str) and '\\"' in s:
        try:
            logging.getLogger("helper").info("RAW_MODEL_ARG %r", s[:400])
        except Exception:
            pass
    # The model escapes the CLOSING quote of an argument, so the string never
    # terminates and MeTTa rejects the whole form. Captured verbatim from the
    # live agent:
    #   ((send "No new user input. Checking status.\\") (mcity-threads))
    #   ((pin "status: idle, no input\\"))
    # An escaped quote immediately before a closing paren is always the intended
    # terminator - a message does not end with a literal backslash-quote - so it
    # is unescaped. Narrow on purpose: \\" anywhere else is left alone, which is
    # what keeps `send "hello" world` and multi-line prose intact.
    if isinstance(s, str):
        s = re.sub(r'\\"(\s*\))', r'"\1', s)
    sexprs = []
    lines = []
    for _blk in split_command_blocks(s):
        lines.extend(split_toplevel_groups(_blk))
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("(-"):
            line = "(pin " + line[2:]
        elif line.startswith("-"):
            line = "pin " + line[1:]
        # remove one outer (...) if present
        if line.startswith("(") and line.endswith(")"):
            line = line[1:-1].strip()
        elif line.startswith("("):
            line = line[1:].strip()
        parts = line.split(maxsplit=1)
        if not parts:
            continue
        cmd = parts[0]
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd in TWO_ARG_COMMANDS:
            if not rest:
                sexprs.append(f"({cmd})")
                continue
            # filename is first token unless already quoted
            if rest.startswith('"'):
                end = 1
                escaped = False
                while end < len(rest):
                    ch = rest[end]
                    if ch == '"' and not escaped:
                        break
                    escaped = (ch == '\\' and not escaped)
                    if ch != '\\':
                        escaped = False
                    end += 1
                if end < len(rest) and rest[end] == '"':
                    filename = rest[:end+1]
                    content = rest[end+1:].strip()
                else:
                    filename = quote_arg(rest[1:])
                    content = ""
            else:
                split_rest = rest.split(maxsplit=1)
                filename = quote_arg(split_rest[0])
                content = split_rest[1].strip() if len(split_rest) > 1 else ""
            if content:
                sexprs.append(f"({cmd} {filename} {quote_arg(content)})")
            else:
                sexprs.append(f"({cmd} {filename})")
            continue
        if rest:
            sexprs.append(f"({cmd} {quote_arg(strip_redundant_quotes(rest))})")
        else:
            sexprs.append(f"({cmd})")
    ret = " ".join(sexprs)
    return "(" + ret + ")"

def normalize_string(x):
    try:
        if isinstance(x, bytes):
            return x.decode("utf-8", errors="ignore")
        return str(x).encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")
    except Exception as e:
        logger.debug(f"Could not normalize value, using its plain string form: {e}")
        return str(x)

def joinPath(parts):
    return os.path.join(*parts)

def projectRootDirectory():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _format_omegaclaw_version(version: str) -> str | None:
    version = version.strip()
    if not version:
        return None
    if version.startswith("OmegaClaw "):
        return version
    return f"OmegaClaw {version}"


def omegaclaw_version(repo_root: str | os.PathLike | None = None) -> str:
    """Return the checkout version, falling back to the baked version file."""
    root = Path(repo_root) if repo_root is not None else Path(projectRootDirectory())

    try:
        # Prevent `git -C` from walking up to a parent repository such as /PeTTa.
        if not (root / ".git").exists():
            raise FileNotFoundError
        result = subprocess.run(
            ["git", "-C", str(root), "describe", "--tags", "--dirty", "--always"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            version = _format_omegaclaw_version(result.stdout)
            if version is not None:
                return version
    except (OSError, subprocess.TimeoutExpired):
        pass

    try:
        version = _format_omegaclaw_version(
            (root / "version").read_text(encoding="utf-8")
        )
        if version is not None:
            return version
    except OSError:
        pass

    return "OmegaClaw unknown"


def test_omegaclaw_version():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        assert omegaclaw_version(root) == "OmegaClaw unknown"

        (root / "version").write_text("v1.2.3-4-g1234567\n", encoding="utf-8")
        assert omegaclaw_version(root) == "OmegaClaw v1.2.3-4-g1234567"

        (root / "version").write_text("OmegaClaw v1.2.3\n", encoding="utf-8")
        assert omegaclaw_version(root) == "OmegaClaw v1.2.3"


def test_balance_parenthesis():
    assert balance_parentheses('(write-file test.txt hello world)') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('(append-file test.txt hello world)') == '((append-file "test.txt" "hello world"))'
    assert balance_parentheses('(write-file-b64 test.txt aGVsbG8=)') == '((write-file-b64 "test.txt" "aGVsbG8="))'
    assert balance_parentheses('write-file-b64 test.txt aGVsbG8=') == '((write-file-b64 "test.txt" "aGVsbG8="))'
    assert balance_parentheses('(write-file "test.txt" hello world)') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('(write-file "test.txt" "hello world")') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('(write-file test.txt "hello world")') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('(send test.xt hello world)') == '((send "test.xt hello world"))'
    assert balance_parentheses('write-file test.txt hello world') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('append-file test.txt hello world') == '((append-file "test.txt" "hello world"))'
    assert balance_parentheses('write-file "test.txt" hello world') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('write-file "test.txt" "hello world"') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('write-file test.txt "hello world"') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('send test.xt hello world') == '((send "test.xt hello world"))'
    assert balance_parentheses('send Here are the planets:\n1. Mercury\n2. Venus') == '((send "Here are the planets:\\n1. Mercury\\n2. Venus"))'
    assert balance_parentheses('send Here are the options:\n- MacBook Air\n- ThinkPad X1\npin done') == '((send "Here are the options:\\n- MacBook Air\\n- ThinkPad X1") (pin "done"))'
    assert balance_parentheses('(shell "pwd")\n(version)') == '((shell "pwd") (version))'
    assert balance_parentheses('send "Plain text version:"\n**Mars** - red planet\nNote: Pluto is a dwarf planet') == '((send "\\\"Plain text version:\\\"\\n**Mars** - red planet\\nNote: Pluto is a dwarf planet"))'
    assert balance_parentheses('(send Here are the planets:\n1. Mercury\n2. Venus)') == '((send "Here are the planets:\\n1. Mercury\\n2. Venus"))'
    assert balance_parentheses('send "hello" world') == '((send "\\"hello\\" world"))'
    assert balance_parentheses('send "Hello"\nHow are you?') == '((send "\\"Hello\\"\\nHow are you?"))'
    # bare "()" lines yield no tokens after _strip_outer_parens and must be skipped, not crash
    assert balance_parentheses('()') == '()'
    assert balance_parentheses('') == '()'
    assert balance_parentheses('   ') == '()'
    assert balance_parentheses('()\nsend hello') == '((send "hello"))'
    assert balance_parentheses('write-file "test.txt" hello\nworld') == '((write-file "test.txt" "hello\\nworld"))'
    assert balance_parentheses('- Found a bug') == '((pin "Found a bug"))'
    assert balance_parentheses('(- Found a bug)') == '((pin "Found a bug"))'
    assert balance_parentheses('- Found\na\nbug') == '((pin "Found\\na\\nbug"))'
    assert balance_parentheses('(- Found a bug') == '((pin "Found a bug"))'

# ---------------------------------------------------------------------------
# History projection
# ---------------------------------------------------------------------------

HISTORY_BLOCK_RE = re.compile(r'^\("\d{4}-\d{2}-\d{2} ', re.M)


def _history_blocks(text):
    """Split history.metta into timestamped blocks, oldest first."""
    starts = [m.start() for m in HISTORY_BLOCK_RE.finditer(text)]
    if not starts:
        return []
    bounds = list(zip(starts, starts[1:] + [len(text)]))
    return [text[a:b] for a, b in bounds]


def _history_signature(block):
    """What makes two turns 'the same turn'.

    The timestamp line differs every time, so it is stripped; everything else
    is whitespace-normalised. Two turns issuing the same commands collapse to
    one signature however far apart they are."""
    body = block.split("\n", 1)[1] if "\n" in block else block
    return " ".join(body.split())


def _history_commands(block):
    """Every command s-expression in a turn, e.g. (mcity-trade "crystal 50 X")."""
    return re.findall(r'\((?:mcity-[a-z-]+|send|pin|remember|query|shell|episodes)'
                      r'(?:\s+"[^"]*")?\)', block)


# Commands the agent is no longer offered. A retired skill keeps its binding so
# that replayed history does not error, which means the agent can go on calling
# it - and it does: mcity-areas was unregistered and still ran 27 times in the
# next window, because its own recent turns were full of it. Feeding those back
# teaches the habit that removing the skill was meant to end.
# "cmd=work" and friends: the agent copied the old cmd= label as part of the
# command name and emitted (cmd=work), a malformed no-op, for 90 of 111 decisions
# in one window. The label is gone, but its examples are still in history.
RETIRED_COMMANDS = ("mcity-areas", "mcity-context", "cmd=", "mcity-work")

# Turns whose whole content was telling the operator nothing. The second group
# is the unsolicited self-status report - "I am currently in the
# hacker-house-interior, working on a task. My hunger is normal... No pending
# messages to reply to at the moment." The outbound guard already stops those
# reaching anyone, but the agent still spent 12 of about 30 turns writing them,
# because its own recent history was full of them.
_IDLE_SEND_RE = re.compile(
    r"no new input|nothing to report|standing by|awaiting (?:your )?instructions"
    r"|no new messages?|idle(?: status)? report"
    r"|no pending messages|i(?:'m| am) currently (?:in|at)"
    r"|working on a task", re.I)


def _is_idle_report(block, commands):
    """True for a turn that talked to the operator without being asked.

    STRUCTURAL, not phrase-based. The phrase list caught "no new input", then "I
    am currently in...", and the agent simply invented new wordings - "I'm here
    and ready to connect", "heartbeat: operational and awaiting task directives"
    - until sends were 77 of 92 turns. Chasing phrasings cannot win.

    loop.metta writes HUMAN_MESSAGE: into a turn only when a new operator message
    arrived on it, so a turn whose every command is a send and which has no
    HUMAN_MESSAGE was unprompted by definition, whatever words it used.

    The cost is accepted deliberately: a send reporting a confirmed world outcome
    is legitimate and also has no HUMAN_MESSAGE, so those stop appearing as
    exemplars too. The mission still permits them; history simply stops teaching
    the agent to talk to a human who did not speak first."""
    # _history_commands keeps the leading paren: "(send \"...\")".
    if not commands or any(not c.lstrip("( ").startswith("send") for c in commands):
        return False
    if "HUMAN_MESSAGE:" in block:
        return False                      # answering a real message: always kept
    return True


def rankedHistory(history_file, budget, repeat_cap=2):
    """Recent history with runaway repetition capped, newest-biased.

    Replaces a raw `read_file_tail`. Tail-truncation selects by POSITION, so a
    loop that repeats one failing command fills the window with copies of its
    own mistake and the model imitates the majority pattern. Measured on the
    live agent, the 30000-byte tail held

        (mcity-trade "to_go_food 50 Central Mart Outlet")   x13   <- all failed
        (mcity-trade "Central Mart Outlet to_go_food 50")   x2    <- all failed
        the correct "crystal 50 ..." form                    x0

    so every exemplar in context was wrong, and no improvement to the merchant
    listing could outvote fifteen of them.

    Turns are walked newest-first. A turn is kept if it still carries something
    worth seeing: a command we have not already shown `repeat_cap` times. Turns
    that are pure repetition are dropped whole - never edited, so nothing in
    the record is falsified - and the drop is stated in the header."""
    try:
        budget = int(budget)
    except (TypeError, ValueError):
        budget = 30000
    try:
        text = Path(history_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    blocks = _history_blocks(text)
    if not blocks:
        return text[-budget:] if budget > 0 else ""

    seen_commands, chosen, used = {}, [], 0
    for block in reversed(blocks):
        commands = _history_commands(block)
        # An idle report is not a turn worth imitating. Measured: after a prompt
        # edit weakened the ban on them, the agent emitted
        # (send "No new input received.") 90 times in four minutes, and restoring
        # the prompt text alone did not stop it - the newest turns in its own
        # history were all idle reports, and it copies its last turn. Repetition
        # capping does not help, because two shown copies are still the two most
        # recent things it did. Dropping them entirely means the newest turn it
        # can see is always one where something actually happened.
        if _is_idle_report(block, commands):
            continue
        # ANY retired call, not only a turn made entirely of them. The narrower
        # rule left mixed turns like ((mcity-areas) (mcity-agents)) in place, and
        # those went on teaching the habit: mcity-areas was still called 19 times
        # a window after being unregistered AND dropped in the all-retired form.
        # Editing a turn to remove one call is not an option - blocks are dropped
        # whole so nothing in the record is falsified - so the turn goes.
        if commands and any(
                c.lstrip("( ").startswith(RETIRED_COMMANDS) for c in commands):
            continue
        # A turn that invoked no recognised skill did nothing, whatever it said,
        # and _history_commands returns [] for exactly those. Matching the words
        # was whack-a-mole: "(No \"action needed.\")" was 133 of 143 turns, I
        # filtered that phrasing, and "(Continue.)" took its place at 65 of 96.
        #
        # This reverses the older rule below, which kept a command-less turn on
        # the grounds that it "still carries prose worth keeping". For an agent
        # whose every useful output is a command, that prose is filler, and it is
        # the single most repeated thing in its context. An exchange with the
        # operator is the exception and is kept.
        if not commands and "HUMAN_MESSAGE:" not in block:
            continue
        if any(f'"{name}"' in block or f"({name}" in block
               for name in RETIRED_COMMANDS):
            # The retired name passed as an ARGUMENT, which the model then
            # imitates: ((mcity-agents "mcity-areas")) appeared verbatim.
            continue
        if commands and all(seen_commands.get(c, 0) >= repeat_cap for c in commands):
            continue
        if used + len(block) > budget:
            break
        for command in commands:
            seen_commands[command] = seen_commands.get(command, 0) + 1
        chosen.append(block)
        used += len(block)

    shown, total = len(chosen), len(blocks)
    chosen.reverse()                                   # back to chronological
    body = "".join(chosen)
    if shown < total:
        body = (f'("history" ; {shown} of {total} turns shown, newest first; '
                f'turns repeating an already-shown command {repeat_cap}x are '
                f'omitted)\n' + body)
    return body


if __name__ == "__main__":
    test_omegaclaw_version()
    test_balance_parenthesis()
