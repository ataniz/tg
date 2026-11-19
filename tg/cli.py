"""Headless commands for scripts and agents. Output is one JSON object per line."""

import argparse
import json
import os
import sys
from typing import Any, Dict

CONFIG_FILE = os.path.expanduser("~/.config/tg/conf.py")
COMMANDS = ("login", "me", "chats", "history", "send")


def client() -> Any:
    from tg import config
    from tg.tdlib import Tdlib

    tg = Tdlib(
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        phone=config.PHONE,
        database_encryption_key=config.ENC_KEY,
        files_directory=config.FILES_DIR,
        tdlib_verbosity=config.TDLIB_VERBOSITY,
        library_path=config.TDLIB_PATH,
    )
    tg.login()
    return tg


def call(tg: Any, method: str, **params: Any) -> Dict[str, Any]:
    result = tg.call_method(method, params=params)
    result.wait()
    if result.error:
        sys.exit(f"{method}: {result.error_info}")
    return result.update


def text_of(msg: Dict[str, Any]) -> str:
    content = msg.get("content", {})
    if "text" in content:
        return content["text"]["text"]
    caption = content.get("caption", {}).get("text", "")
    return caption or f"[{content.get('@type', 'unknown')}]"


def message(msg: Dict[str, Any]) -> Dict[str, Any]:
    sender = msg.get("sender_id", {})
    return {
        "id": msg["id"],
        "date": msg["date"],
        "outgoing": msg["is_outgoing"],
        "sender": sender.get("user_id") or sender.get("chat_id"),
        "text": text_of(msg),
    }


def chat(tg: Any, chat_id: int) -> Dict[str, Any]:
    c = call(tg, "getChat", chat_id=chat_id)
    last = c.get("last_message")
    return {
        "id": c["id"],
        "title": c["title"],
        "type": c["type"]["@type"].removeprefix("chatType"),
        "unread": c["unread_count"],
        "muted": c["notification_settings"]["mute_for"] > 0,
        "last": message(last) if last else None,
    }


def emit(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def main(argv: list) -> None:
    p = argparse.ArgumentParser(prog="tg")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="Sign in once (asks for phone and code)")
    sub.add_parser("me", help="Show the signed-in account")
    c = sub.add_parser("chats", help="Recent chats, newest first")
    c.add_argument("--limit", type=int, default=50)
    h = sub.add_parser("history", help="Recent messages in a chat")
    h.add_argument("chat_id", type=int)
    h.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("send", help="Send a text message (dry run without --yes)")
    s.add_argument("chat_id", type=int)
    s.add_argument("text")
    s.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)

    if args.cmd != "login" and not os.path.isfile(CONFIG_FILE):
        sys.exit("Not signed in. Run: tg login")

    tg = client()
    try:
        if args.cmd == "login":
            me = call(tg, "getMe")
            emit({"signed_in": True, "user_id": me["id"]})
        elif args.cmd == "me":
            me = call(tg, "getMe")
            emit({k: me.get(k) for k in ("id", "first_name", "last_name")})
        elif args.cmd == "chats":
            ids = call(tg, "getChats", chat_list={"@type": "chatListMain"}, limit=args.limit)
            for chat_id in ids["chat_ids"]:
                emit(chat(tg, chat_id))
        elif args.cmd == "history":
            msgs = call(tg, "getChatHistory", chat_id=args.chat_id, from_message_id=0,
                        offset=0, limit=args.limit, only_local=False)
            for m in msgs["messages"]:
                emit(message(m))
        elif args.cmd == "send":
            target = chat(tg, args.chat_id)["title"]
            if not args.yes:
                emit({"dry_run": True, "to": target, "text": args.text})
                return
            sent = tg.send_message(args.chat_id, args.text)
            sent.wait()
            if sent.error:
                sys.exit(f"send: {sent.error_info}")
            emit({"sent": True, "to": target, "id": sent.update["id"]})
    finally:
        tg.stop()


def entry() -> None:
    """`tg <command>` runs headless; plain `tg` opens the terminal UI."""
    if len(sys.argv) > 1 and sys.argv[1] in COMMANDS + ("-h", "--help"):
        main(sys.argv[1:])
    else:
        from tg.__main__ import main as tui

        tui()
