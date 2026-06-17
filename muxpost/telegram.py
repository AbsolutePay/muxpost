import hashlib
import html
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from core.config import API, DOC_MAX_BYTES


def api(method, _timeout=20, **params):
    """Call a Telegram Bot API method. dict/list params are JSON-encoded."""
    data = {}
    for key, val in params.items():
        if val is None:
            continue
        data[key] = json.dumps(val) if isinstance(val, (dict, list)) else val
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(f"{API}/{method}", data=body)
    try:
        with urllib.request.urlopen(req, timeout=_timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()
        except Exception:  # noqa: BLE001
            pass
        print(f"[api] {method} failed: HTTP {exc.code} {detail}", file=sys.stderr)
        return {"ok": False, "error": str(exc), "detail": detail}
    except Exception as exc:  # noqa: BLE001
        print(f"[api] {method} failed: {exc}", file=sys.stderr)
        return {"ok": False, "error": str(exc)}


def send(chat_id, text, reply_markup=None, reply_to=None):
    res = api(
        "sendMessage",
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=reply_markup,
        reply_to_message_id=reply_to,
    )
    if res.get("ok"):
        return res["result"]["message_id"]
    return None


def edit(chat_id, message_id, text=None, reply_markup=None):
    if text is not None:
        api(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
    else:
        api(
            "editMessageReplyMarkup",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )


def answer(callback_id, text=None):
    api("answerCallbackQuery", callback_query_id=callback_id, text=text)


def send_document(chat_id, path, caption=None):
    """Upload a local file to the chat via sendDocument (multipart/form-data).

    api() is urlencoded and can't carry a file, so we build the multipart body
    by hand (stdlib only). Returns (ok, error_message).
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return False, str(exc)
    if size > DOC_MAX_BYTES:
        return False, f"file is {size // (1024 * 1024)} MB (Telegram limit is 50 MB)"
    boundary = "muxpost" + os.urandom(16).hex()
    bb = boundary.encode()
    fname = os.path.basename(path).replace('"', "_") or "file"
    out = []

    def _field(name, value):
        out.append(b"--" + bb + b"\r\n")
        out.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.append(str(value).encode() + b"\r\n")

    _field("chat_id", chat_id)
    if caption:
        _field("caption", caption)
        _field("parse_mode", "HTML")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return False, str(exc)
    out.append(b"--" + bb + b"\r\n")
    out.append(
        f'Content-Disposition: form-data; name="document"; filename="{fname}"\r\n'.encode()
    )
    out.append(b"Content-Type: application/octet-stream\r\n\r\n")
    out.append(data + b"\r\n")
    out.append(b"--" + bb + b"--\r\n")
    req = urllib.request.Request(f"{API}/sendDocument", data=b"".join(out))
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            res = json.load(resp)
        return bool(res.get("ok")), res.get("description", "")
    except Exception as exc:  # noqa: BLE001
        print(f"[api] sendDocument failed: {exc}", file=sys.stderr)
        return False, str(exc)
