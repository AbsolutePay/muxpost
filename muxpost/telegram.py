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

from core.config import API, DOC_MAX_BYTES, TOKEN


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


def _upload(method, field, chat_id, path, caption=None):
    """Upload a local file via a multipart Bot API method (sendDocument/sendPhoto).

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
        f'Content-Disposition: form-data; name="{field}"; filename="{fname}"\r\n'.encode()
    )
    out.append(b"Content-Type: application/octet-stream\r\n\r\n")
    out.append(data + b"\r\n")
    out.append(b"--" + bb + b"--\r\n")
    req = urllib.request.Request(f"{API}/{method}", data=b"".join(out))
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            res = json.load(resp)
        return bool(res.get("ok")), res.get("description", "")
    except Exception as exc:  # noqa: BLE001
        print(f"[api] {method} failed: {exc}", file=sys.stderr)
        return False, str(exc)


def send_document(chat_id, path, caption=None):
    """Upload a local file to the chat as a document. Returns (ok, error)."""
    return _upload("sendDocument", "document", chat_id, path, caption)


def send_photo(chat_id, path, caption=None):
    """Upload a local image to the chat with inline preview. Returns (ok, error)."""
    return _upload("sendPhoto", "photo", chat_id, path, caption)


def download_file(file_id, dest_dir, suggested_name=None):
    """Download a Telegram file (document/photo/…) into dest_dir.

    getFile -> file_path on Telegram's server -> fetch it. Bot download is
    capped at 20 MB by Telegram. Returns (local_path | None, error_message).
    """
    r = api("getFile", file_id=file_id)
    if not r.get("ok"):
        return None, r.get("description") or r.get("error") or "getFile failed"
    fpath = (r.get("result") or {}).get("file_path")
    if not fpath:
        return None, "Telegram returned no file_path"
    name = re.sub(r"[^A-Za-z0-9._-]", "_",
                  suggested_name or os.path.basename(fpath) or "file") or "file"
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name)
        url = f"https://api.telegram.org/file/bot{TOKEN}/{fpath}"
        with urllib.request.urlopen(url, timeout=180) as resp, open(dest, "wb") as fh:
            fh.write(resp.read())
        return dest, ""
    except Exception as exc:  # noqa: BLE001
        print(f"[api] download_file failed: {exc}", file=sys.stderr)
        return None, str(exc)
