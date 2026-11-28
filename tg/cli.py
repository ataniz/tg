"""Headless commands for scripts and agents. Output is one JSON object per line."""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

CONFIG_FILE = os.path.expanduser("~/.config/tg/conf.py")
COMMANDS = ("login", "me", "chats", "history", "send", "export")


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
    for _ in range(5):
        result = tg.call_method(method, params=params)
        result.wait()
        if not result.error:
            return result.update
        # Telegram slows bulk reads down with 429 "retry after N"; wait it out rather than fail.
        wait = re.search(r"retry after (\d+)", str(result.error_info.get("message", "")))
        if result.error_info.get("code") != 429 or not wait:
            break
        time.sleep(int(wait.group(1)) + 1)
    sys.exit(f"{method}: {result.error_info}")


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


# Telegram's own service account sends login codes; it is never exported.
SERVICE_USERS = {777000}


def history(tg: Any, chat_id: int, from_id: int) -> List[Dict[str, Any]]:
    """Messages older than from_id (0: the newest), newest first. TDLib may return fewer than asked."""
    return call(tg, "getChatHistory", chat_id=chat_id, from_message_id=from_id, offset=0, limit=100, only_local=False)["messages"]


def pages(tg: Any, chat_id: int, from_id: int, stop_at: int, budget: List[int]) -> tuple:
    """Messages from from_id back, newest first, until stop_at, the start of the chat or the page budget.
    Returns them and whether it stopped for a reason other than the budget."""
    out: List[Dict[str, Any]] = []
    while budget[0] > 0:
        budget[0] -= 1
        batch = history(tg, chat_id, from_id)
        if not batch:
            return out, True
        for m in batch:
            if m["id"] <= stop_at:
                return out, True
            out.append(m)
        from_id = batch[-1]["id"]
    return out, False


class Users:
    def __init__(self, tg: Any) -> None:
        self.tg, self.cache = tg, {}

    def get(self, user_id: int) -> Dict[str, Any]:
        if user_id not in self.cache:
            self.cache[user_id] = call(self.tg, "getUser", user_id=user_id)
        return self.cache[user_id]

    def name(self, user_id: int) -> str:
        u = self.get(user_id)
        return " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or str(user_id)


def describe(tg: Any, users: Users, c: Dict[str, Any]) -> Dict[str, Any]:
    t = c["type"]
    kind, members, count, bot = "group", [], None, False
    if t["@type"] in ("chatTypePrivate", "chatTypeSecret"):
        kind, members, count = "dm", [str(t["user_id"])], 2
        bot = t["user_id"] in SERVICE_USERS or users.get(t["user_id"]).get("type", {}).get("@type") == "userTypeBot"
    elif t["@type"] == "chatTypeBasicGroup":
        count = call(tg, "getBasicGroup", basic_group_id=t["basic_group_id"]).get("member_count")
        if count is not None and count <= 50:
            info = call(tg, "getBasicGroupFullInfo", basic_group_id=t["basic_group_id"])
            members = [str(m["member_id"]["user_id"]) for m in info.get("members", []) if m["member_id"].get("user_id")]
    elif t["@type"] == "chatTypeSupergroup":
        kind = "channel" if t.get("is_channel") else "group"
        count = call(tg, "getSupergroup", supergroup_id=t["supergroup_id"]).get("member_count")
    return {"type": "chat", "id": str(c["id"]), "title": c["title"], "kind": kind, "members": members, "memberCount": count, "bot": bot}


def record(users: Users, chat_id: int, m: Dict[str, Any]) -> Dict[str, Any]:
    sender = m.get("sender_id", {})
    user = sender.get("user_id")
    reply = m.get("reply_to") or {}
    return {
        "type": "message",
        "chat": str(chat_id),
        "id": str(m["id"]),
        "ts": datetime.fromtimestamp(m["date"], timezone.utc).isoformat().replace("+00:00", "Z"),
        "sender": "me" if m["is_outgoing"] else str(user or sender.get("chat_id")),
        "senderName": None if m["is_outgoing"] or not user else users.name(user),
        "text": text_of(m),
        "kind": m.get("content", {}).get("@type", "unknown").removeprefix("message").lower(),
        "replyTo": str(reply["message_id"]) if reply.get("message_id") else None,
    }


def export(tg: Any, out: str, max_members: int, skip: List[str], page_budget: int) -> Dict[str, Any]:
    """Appends new messages of every chat to <out>/<YYYY-MM>.jsonl and backfills older ones a budget at a time.

    state.json keeps, per chat, the newest and oldest message id written and whether the start was reached,
    so a second run writes only what is new. Channels, bots, Telegram's service account, groups over
    max_members and the chats in skip are left out."""
    os.makedirs(out, exist_ok=True)
    state_path = os.path.join(out, "state.json")
    state = json.load(open(state_path)) if os.path.exists(state_path) else {"chats": {}}
    log = open(os.path.join(out, datetime.now(timezone.utc).strftime("%Y-%m") + ".jsonl"), "a", encoding="utf-8")
    users, budget, report = Users(tg), [page_budget], {"chats": 0, "skipped": 0, "messages": 0, "backfilling": 0}
    ids: List[int] = []
    for chat_list in ({"@type": "chatListMain"}, {"@type": "chatListArchive"}):
        ids += call(tg, "getChats", chat_list=chat_list, limit=1000)["chat_ids"]

    def write(obj: Dict[str, Any]) -> None:
        log.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def save() -> None:
        log.flush()
        json.dump(state, open(state_path + ".tmp", "w"))
        os.replace(state_path + ".tmp", state_path)

    try:
        # First every chat's new messages, so recent ones are never stuck behind a backfill: a chat
        # seen before is read back to its newest known message, a new one gets its latest page.
        kept = []
        for chat_id in dict.fromkeys(ids):
            meta = describe(tg, users, call(tg, "getChat", chat_id=chat_id))
            if meta["kind"] == "channel" or meta["bot"] or str(chat_id) in skip or meta["title"] in skip or (meta["memberCount"] or 0) > max_members:
                report["skipped"] += 1
                continue
            report["chats"] += 1
            meta.pop("bot")
            st = state["chats"].setdefault(str(chat_id), {"newest": 0, "oldest": 0, "complete": False, "meta": None})
            if st["meta"] != meta:
                write(meta)
                st["meta"] = meta
            fresh, _ = pages(tg, chat_id, 0, st["newest"], [50 if st["newest"] else 1])
            for m in reversed(fresh):
                write(record(users, chat_id, m))
            if fresh:
                st["newest"] = max(st["newest"], fresh[0]["id"])
                st["oldest"] = st["oldest"] or fresh[-1]["id"]
            elif not st["oldest"]:
                st["complete"] = True
            report["messages"] += len(fresh)
            kept.append(chat_id)
            save()
        # Then older history, chat by chat, as far as the page budget goes.
        for chat_id in kept:
            st = state["chats"][str(chat_id)]
            if st["complete"] or budget[0] <= 0:
                continue
            older, ended = pages(tg, chat_id, st["oldest"], 0, budget)
            for m in reversed(older):
                write(record(users, chat_id, m))
            if older:
                st["oldest"] = older[-1]["id"]
            st["complete"] = ended
            report["messages"] += len(older)
            save()
        report["backfilling"] = sum(1 for chat_id in kept if not state["chats"][str(chat_id)]["complete"])
    finally:
        log.close()
    return report


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
    e = sub.add_parser("export", help="Append new messages of every chat to JSON lines in a folder, backfilling older ones")
    e.add_argument("out")
    e.add_argument("--max-members", type=int, default=50)
    e.add_argument("--skip", action="append", default=[], help="chat id or title to leave out (repeat)")
    e.add_argument("--pages", type=int, default=300, help="most history pages (100 messages each) to fetch in this run")
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
        elif args.cmd == "export":
            emit(export(tg, args.out, args.max_members, args.skip, args.pages))
    finally:
        tg.stop()


def entry() -> None:
    """`tg <command>` runs headless; plain `tg` opens the terminal UI."""
    if len(sys.argv) > 1 and sys.argv[1] in COMMANDS + ("-h", "--help"):
        main(sys.argv[1:])
    else:
        from tg.__main__ import main as tui

        tui()
