#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

MANAGE = Path("/data/skills/user-profiler/scripts/manage-profile.py")
SEND_BATCH = Path("/data/skills/user-profiler/scripts/send-review-batch.py")
PUBLISH_GATE_ENV = Path("/data/.openclaw/publish-gate.env")
OPENCLAW_CONFIG = Path("/data/.openclaw/openclaw.json")


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True)


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def load_items(limit: int) -> list[dict]:
    proc = run(["python3", str(SEND_BATCH), str(limit)])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "failed to load review batch")
    payload = json.loads(proc.stdout)
    return list(payload.get("items") or [])


def load_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or os.environ.get("TELEGRAM_APPROVAL_BOT_TOKEN", "").strip()
    if token:
        return token
    if PUBLISH_GATE_ENV.exists():
        for line in PUBLISH_GATE_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_APPROVAL_BOT_TOKEN"} and v.strip():
                return v.strip()
    if OPENCLAW_CONFIG.exists():
        try:
            obj = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
            token = (((obj.get("channels") or {}).get("telegram") or {}).get("botToken") or "").strip()
            if token:
                return token
        except Exception:
            pass
    raise RuntimeError("telegram bot token not found")


def tg_post(token: str, method: str, body: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram HTTP {e.code}: {detail}") from e
    if not raw.get("ok"):
        raise RuntimeError(f"Telegram API error: {raw}")
    return raw["result"]


def send_card(token: str, target: str, item: dict, silent: bool = False) -> dict:
    cid = str(item["id"])
    text = str(item.get("text") or "").strip()
    evidence = str(item.get("evidence") or "").strip()
    body = f"[candidate] `{cid}`\n{text}\n\nEvidence: {truncate(evidence, 700)}"
    payload = {
        "chat_id": int(target) if str(target).lstrip("-").isdigit() else str(target),
        "text": body[:4096],
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "must", "callback_data": f"profile_rate:candidate:{cid}:must"},
                {"text": "nice", "callback_data": f"profile_rate:candidate:{cid}:nice"},
                {"text": "not", "callback_data": f"profile_rate:candidate:{cid}:not"},
                {"text": "review", "callback_data": f"profile_rate:candidate:{cid}:review"},
            ]]
        },
    }
    if silent:
        payload["disable_notification"] = True
    result = tg_post(token, "sendMessage", payload)
    message_id = result.get("message_id")
    if message_id is not None:
        reg = run(["python3", str(MANAGE), "review-register", cid, str(message_id)])
        if reg.returncode != 0:
            raise RuntimeError(reg.stderr.strip() or reg.stdout.strip() or f"failed to register card {cid}")
    return {"candidate_id": cid, "message_id": message_id}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--limit", type=int, default=4)
    ap.add_argument("--silent", action="store_true")
    args = ap.parse_args()

    items = load_items(args.limit)
    if not items:
        print(json.dumps({"ok": True, "sent": 0, "items": []}, ensure_ascii=False))
        return 0

    token = load_token()
    sent = []
    for item in items:
        sent.append(send_card(token, args.target, item, silent=args.silent))

    print(json.dumps({"ok": True, "sent": len(sent), "items": sent}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
