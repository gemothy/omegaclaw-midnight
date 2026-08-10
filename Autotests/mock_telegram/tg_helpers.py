"""Telegram-flavored thin wrappers over the shared IRC test infrastructure.

The shared `Autotests/helpers.py` is built around an IRC `send_prompt`. For
the Telegram suite we substitute that with `tg_send_prompt(tg, prompt)`,
which has the driver bot send `sendMessage(@agent, prompt)` to the agent
bot via api.telegram.org (Bot API 10.0 bot-to-bot mode).

Everything else — `Checker`, `dexec`, `wait_for_file`, history/skill waiters,
prompt envelope — is reused unchanged.
"""
import os
import sys

# Make Autotests/ importable so we can pull in the shared helpers.
_PARENT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from helpers import (  # noqa: E402, F401  (re-exported for tests)
    Checker,
    dexec,
    dexec_root,
    make_prompt,
    wait_for_file,
    wait_for_file_mtime_change,
    wait_for_history_keyword,
    wait_for_history_block,
    wait_for_skill_call,
    wait_for_any_skill_call,
    wait_for_skill_match,
    find_skill_calls,
    read_history,
    get_mtime,
    get_size,
)


TG_USER_ID = 999
TG_CHAT_ID = 999
TG_USERNAME = "qatestuser"


def tg_send_prompt(tg_driver, prompt,
                   user_id=TG_USER_ID, chat_id=TG_CHAT_ID, username=TG_USERNAME):
    """Have the driver bot send `prompt` to the agent bot.

    Mirrors the role of `helpers.send_prompt` in the IRC suite — the agent
    sees the message on its next `getUpdates` poll. `user_id` / `chat_id` /
    `username` are accepted for API parity but ignored: the only real
    sender Telegram knows about is the driver bot.
    """
    tg_driver.inject_user_message(
        prompt, user_id=user_id, chat_id=chat_id, username=username,
    )
    return True
