import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import auth
from src.logger import get_logger
from delivery_queue import PendingMessages
import channels
from config import config_get_by_key

logger = get_logger(__name__)

_running = False
_last_message = ""
_msg_lock = threading.Lock()
_state_lock = threading.Lock()

_bot_token = ""
_api_base = ""
_chat_id = ""
_poll_timeout = 20
_offset = None
_connected = False

_authenticated_user_id = None
_outbox = PendingMessages()


def _set_last(msg):
    global _last_message
    with _msg_lock:
        if _last_message == "":
            _last_message = msg
        else:
            _last_message = _last_message + " | " + msg


def getLastMessage():
    global _last_message
    with _msg_lock:
        tmp = _last_message
        _last_message = ""
        return tmp


def _parse_auth_candidate(msg):
    text = msg.strip()
    lower = text.lower()
    if lower.startswith("auth "):
        return text[5:].strip()
    if lower.startswith("/auth "):
        return text[6:].strip()
    return text


def _display_name(user, chat):
    username = str(user.get("username", "")).strip()
    if username:
        return f"@{username}"

    first = str(user.get("first_name", "")).strip()
    last = str(user.get("last_name", "")).strip()
    full = f"{first} {last}".strip()
    if full:
        return full

    title = str(chat.get("title", "")).strip()
    if title:
        return title

    return "telegram_user"


def _api_call(method, params=None, timeout=30, use_post=False):
    if not _api_base:
        raise RuntimeError("Telegram adapter not initialized")

    params = params or {}
    encoded = urllib.parse.urlencode(params).encode("utf-8")
    url = f"{_api_base}/{method}"

    if use_post:
        req = urllib.request.Request(url, data=encoded)
    else:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url)

    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="ignore"))

    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", f"{method} failed"))

    return payload.get("result")


def _initialize_offset():
    global _offset
    try:
        updates = _api_call("getUpdates", {"timeout": 0}, timeout=10) or []
    except Exception as exc:
        logger.warning(f"Could not read initial offset: {exc}")
        return

    max_update = -1
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            max_update = max(max_update, update_id)

    if max_update >= 0:
        with _state_lock:
            _offset = max_update + 1


def _is_auth_command(msg):
    lower = msg.strip().lower()
    return lower.startswith("auth ") or lower.startswith("/auth ")


def _is_allowed_message(chat_id, user_id, msg):
    global _chat_id, _authenticated_user_id

    with _state_lock:
        if _chat_id and chat_id != _chat_id:
            return "ignore"
        if not auth.is_auth_enabled():
            if not _chat_id:
                _chat_id = chat_id
            return "allow"
        if _authenticated_user_id is not None:
            if chat_id != _chat_id:
                return "ignore"
            return "allow" if user_id == _authenticated_user_id else "ignore"
        auth_candidate = _parse_auth_candidate(msg) if _is_auth_command(msg) else None
        user_id_check = auth.authenticate_channel_user('TELEGRAM', user_id, auth_candidate)
        if user_id_check in ["auth_bound", "allow"]:
            _authenticated_user_id = user_id
            _chat_id = chat_id
            return user_id_check
        else:
            return "ignore"


def _ready_to_send():
    with _state_lock:
        return _connected and bool(_chat_id)


def _deliver_outbound(chunk):
    with _state_lock:
        target_chat = _chat_id
    if not target_chat:
        raise RuntimeError("Telegram chat is not bound")
    _api_call(
        "sendMessage",
        {"chat_id": target_chat, "text": chunk},
        timeout=15,
        use_post=True,
    )


def _flush_outbox():
    global _connected
    try:
        _outbox.flush(_deliver_outbound, _ready_to_send)
    except Exception as exc:
        _connected = False
        logger.warning(f"Telegram send failed; retaining queued message: {exc}")


def _poll_loop():
    global _connected, _offset
    logger.info("Polling started")

    while _running:
        try:
            params = {"timeout": int(_poll_timeout)}
            with _state_lock:
                if _offset is not None:
                    params["offset"] = _offset

            updates = _api_call("getUpdates", params=params, timeout=int(_poll_timeout) + 10) or []
            _connected = True

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    with _state_lock:
                        if _offset is None or (update_id + 1) > _offset:
                            _offset = update_id + 1

                message = update.get("message") or update.get("edited_message")
                if not isinstance(message, dict):
                    continue

                text = message.get("text")
                if not text:
                    continue

                chat = message.get("chat") or {}
                user = message.get("from") or {}
                chat_id = str(chat.get("id", "")).strip()
                user_id = str(user.get("id", "")).strip()
                if not chat_id or not user_id:
                    continue

                state = _is_allowed_message(chat_id, user_id, text)
                display_name = _display_name(user, chat)
                if state == "allow":
                    # An agent turn can take tens of seconds (LLM latency plus
                    # skill calls), during which the user sees nothing at all
                    # and assumes the bot is dead. Telegram clears a typing
                    # action after ~5s, so a keepalive thread re-sends it until
                    # the reply actually goes out.
                    _start_typing(chat_id)
                    _set_last(f"{display_name}: {text}")
                elif state == "auth_bound":
                    send_message(f"Authentication successful for {display_name}.")
            _flush_outbox()
        except Exception as exc:
            _connected = False
            logger.warning(f"Poll error: {exc}")
            time.sleep(2)

    _connected = False
    logger.info("Polling stopped")


def start_telegram(chat_id="", poll_timeout=20):
    global _running, _bot_token, _api_base, _chat_id, _poll_timeout, _offset, _connected

    proxy = auth.get_proxy_url()
    if proxy:
        _bot_token = "proxy"
        _api_base = f"{proxy}/telegram"
    else:
        _bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
        if not _bot_token:
            raise ValueError("TG_BOT_TOKEN is required")
        _api_base = f"https://api.telegram.org/bot{_bot_token}"

    _chat_id = str(chat_id).strip()

    try:
        _poll_timeout = max(1, int(poll_timeout))
    except Exception as e:
        logger.warning(f"Invalid poll_timeout {poll_timeout!r}, falling back to 20: {e}")
        _poll_timeout = 20

    _offset = None
    _running = True
    _connected = False
    logger.info(f"Starting adapter with chat target: {_chat_id or 'auto-bind'}")
    _initialize_offset()

    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
    return t


def stop_telegram():
    global _running
    _running = False


_typing_stop = None
_typing_thread = None
_typing_lock = threading.Lock()


def _start_typing(chat_id):
    """Show a 'typing...' bubble until the reply is sent.

    Telegram expires a chat action after about five seconds, so this refreshes
    it on a timer rather than sending it once. Best effort only: a failure here
    must never affect message handling.
    """
    global _typing_stop, _typing_thread
    try:
        with _typing_lock:
            if _typing_thread is not None and _typing_thread.is_alive():
                return
            stop = threading.Event()

            def _loop():
                # Bounded so a lost reply cannot leave it typing forever.
                for _ in range(120):
                    if stop.wait(4.0):
                        return
                    try:
                        _api_call("sendChatAction",
                                  {"chat_id": chat_id, "action": "typing"},
                                  timeout=10)
                    except Exception:
                        return

            try:
                _api_call("sendChatAction",
                          {"chat_id": chat_id, "action": "typing"}, timeout=10)
            except Exception:
                pass
            thread = threading.Thread(target=_loop, name="tg-typing", daemon=True)
            _typing_stop = stop
            _typing_thread = thread
            thread.start()
    except Exception:
        pass


def _stop_typing():
    global _typing_stop, _typing_thread
    try:
        with _typing_lock:
            if _typing_stop is not None:
                _typing_stop.set()
            _typing_stop = None
            _typing_thread = None
    except Exception:
        pass


# Phrases that mean "I have nothing to say". The agent is instructed never to
# send these, and for most of a long session it did not - then a prompt edit
# weakened the wording and it produced 132 of them in eight minutes. Prose alone
# is not a safeguard for something that rings a real person's phone, so the ban
# is enforced here too, at the last point before the message leaves the process.
#
# Deliberately narrow, and matched against the WHOLE message: a report that
# happens to contain "nothing to report" inside a real update still goes out.
_IDLE_REPORT_RE = re.compile(
    r"^\W*(?:"
    r"no new (?:input|messages?)(?: received)?"
    r"|nothing (?:new )?to report"
    r"|standing by"
    r"|awaiting (?:your )?(?:instructions?|input)"
    r"|no (?:new )?updates?"
    r")\W*$", re.I)


def is_idle_report(text):
    """True when the whole message says only that there is nothing to say."""
    return bool(_IDLE_REPORT_RE.match(str(text or "").strip()))


def send_message(text):
    _stop_typing()
    text = str(text).replace("\\n", "\n").replace("\r", "")
    if not text:
        return
    if is_idle_report(text):
        logger.warning("telegram: suppressed an idle report: %r", text[:80])
        return

    max_len = 3900
    chunks = []
    for i in range(0, len(text), max_len):
        chunk = text[i:i + max_len]
        if chunk:
            chunks.append(chunk)
    _outbox.extend(chunks)
    _flush_outbox()

class TelegramChannel(channels.CommChannel):

    def __init__(self):
        super().__init__()

    def start(self) -> None:
        chat_id = config_get_by_key("TG_CHAT_ID", "")
        poll_timeout = int(config_get_by_key("TG_POLL_TIMEOUT", 20))
        start_telegram(chat_id, poll_timeout)

    def stop(self) -> None:
        stop_telegram()

    def receive(self) -> str:
        return getLastMessage()

    def send(self, message: str) -> None:
        send_message(message)

def loadOmegaClawPlugin():
    channels.registerCommChannel("telegram", TelegramChannel())
