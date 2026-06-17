"""muxpost as an MCP server — lets an AI agent talk to *you* through muxpost.

Deliberately tiny and safe: the only capabilities exposed are sending a message
or a file/photo to the muxpost owner's Telegram. There is **no** session
visibility or control here — an agent (which often runs inside one of the
watched sessions) must not be able to read, drive, or kill those sessions.

Speaks MCP over stdio: newline-delimited JSON-RPC 2.0, requests on stdin,
responses on stdout (stdout stays pure JSON — all logging goes to stderr).
Register with an MCP host, e.g.:
    hermes mcp add muxpost --command python3 --args /path/to/muxpost.py mcp
"""
import html
import json
import os
import sys

from core.config import PROJECT_ROOT, USER_ID, require_config
from muxpost.process import version
from muxpost.telegram import api, send_document, send_photo

PROTO_DEFAULT = "2025-06-18"

TOOLS = [
    {
        "name": "send_message",
        "description": "Send a text message to the muxpost owner on Telegram. "
                       "Use this to report progress, ask something, or notify the user.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Message text (plain text)."}},
            "required": ["text"],
        },
    },
    {
        "name": "send_file",
        "description": "Upload a local file to the muxpost owner on Telegram (as a document). "
                       "The path must be inside the configured project root.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or project-relative file path."},
                "caption": {"type": "string", "description": "Optional caption."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "send_photo",
        "description": "Send a local image to the muxpost owner on Telegram with an inline "
                       "preview (screenshots, charts). Path must be inside the project root.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or project-relative image path."},
                "caption": {"type": "string", "description": "Optional caption."},
            },
            "required": ["path"],
        },
    },
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _root():
    return os.path.abspath(PROJECT_ROOT or os.path.expanduser("~"))


def _allowed_path(path):
    """Resolve `path` and return it only if it sits inside the allowed root.

    Stops an agent from exfiltrating arbitrary files (e.g. ~/.ssh) via muxpost.
    """
    if not path:
        return None
    root = _root()
    p = os.path.expanduser(path)
    if not os.path.isabs(p):           # relative paths resolve against the root
        p = os.path.join(root, p)
    p = os.path.abspath(p)
    return p if p == root or p.startswith(root + os.sep) else None


def _text(s, err=False):
    return {"content": [{"type": "text", "text": s}], "isError": err}


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


# --------------------------------------------------------------------------
# tool dispatch
# --------------------------------------------------------------------------
def _call_tool(name, args):
    try:
        if name == "send_message":
            text = (args.get("text") or "").strip()
            if not text:
                return _text("text is required", err=True)
            r = api("sendMessage", chat_id=USER_ID, text=text, disable_web_page_preview=True)
            if r.get("ok"):
                return _text("Message delivered to the user.")
            return _text(f"Delivery failed: {r.get('description') or r.get('error')}", err=True)

        if name in ("send_file", "send_photo"):
            p = _allowed_path(args.get("path"))
            if not p:
                return _text(f"path must be inside the allowed root ({_root()})", err=True)
            if not os.path.isfile(p):
                return _text(f"no file at {p}", err=True)
            caption = args.get("caption")
            caption = html.escape(caption) if caption else None
            fn = send_photo if name == "send_photo" else send_document
            ok, err = fn(USER_ID, p, caption=caption)
            if ok:
                return _text(f"Sent {os.path.basename(p)} to the user.")
            return _text(f"Send failed: {err}", err=True)

        return _text(f"unknown tool: {name}", err=True)
    except Exception as exc:  # noqa: BLE001
        return _text(f"error running {name}: {exc}", err=True)


def _handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion") or PROTO_DEFAULT
        return _result(mid, {
            "protocolVersion": proto,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "muxpost", "version": version()},
        })
    if mid is None:           # any other notification (e.g. notifications/initialized)
        return None
    if method == "tools/list":
        return _result(mid, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        return _result(mid, _call_tool(params.get("name"), params.get("arguments") or {}))
    if method == "ping":
        return _result(mid, {})
    return _error(mid, -32601, f"method not found: {method}")


def serve():
    """Run the stdio MCP loop until stdin closes."""
    require_config()
    print(f"muxpost MCP server ready ({version()}) — stdio", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            resp = _handle(msg)
        except Exception as exc:  # noqa: BLE001
            resp = _error(msg.get("id"), -32603, f"internal error: {exc}")
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
