#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
推送  —  微信 (Server酱 / PushPlus) 或 Telegram, 由 run_daily.sh 调用
================================================================
配置任意一个即可 (环境变量 或 同目录 .env 文件; 不要写进代码/提交到 git):
  微信 Server酱:   SCT_SENDKEY=SCTxxxxxxxx          ← 推荐, 微信扫码即得, 免费 5 条/天
  微信 PushPlus:   PUSHPLUS_TOKEN=xxxxxxxx           ← 备选
  Telegram:        TG_BOT_TOKEN=...  TG_CHAT_ID=...
配置了多个会全部发送。

用法:
  python notify.py --test              # 发一条测试消息
  python notify.py --daily             # 发今日摘要 (默认只在需要操作/非GREEN时发; 加 --always 每天都发)
  python notify.py --whoami            # (仅 Telegram) 打印 chat_id
"""
import os, sys, re, json, argparse, urllib.request, urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")


def load_env():
    p = os.path.join(BASE, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
    return dict(tg_tok=os.environ.get("TG_BOT_TOKEN"), tg_cid=os.environ.get("TG_CHAT_ID"),
                sct=os.environ.get("SCT_SENDKEY"), pushplus=os.environ.get("PUSHPLUS_TOKEN"))


def _post(url, data, as_json=False):
    body = json.dumps(data).encode() if as_json else urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json" if as_json else "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def send_serverchan(title, text, key):
    js = _post(f"https://sctapi.ftqq.com/{key}.send", {"title": title[:32], "desp": text})
    return js.get("code") == 0


def send_pushplus(title, text, token):
    js = _post("http://www.pushplus.plus/send", {"token": token, "title": title, "content": text, "template": "txt"}, as_json=True)
    return js.get("code") == 200


def send_all(title, text, cfg):
    sent = []
    if cfg["sct"]:
        sent.append(("微信Server酱", send_serverchan(title, text, cfg["sct"])))
    if cfg["pushplus"]:
        sent.append(("微信PushPlus", send_pushplus(title, text, cfg["pushplus"])))
    if cfg["tg_tok"] and cfg["tg_cid"]:
        sent.append(("Telegram", send(text, cfg["tg_tok"], cfg["tg_cid"])))
    return sent


def send(text, tok, cid):
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": cid, "text": text, "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
        return json.load(r).get("ok", False)


def whoami(tok):
    with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=20) as r:
        js = json.load(r)
    ids = {(u.get("message") or u.get("channel_post") or {}).get("chat", {}).get("id") for u in js.get("result", [])}
    ids.discard(None)
    if not ids: print("没有收到任何消息。先在 Telegram 里给你的 bot 发一句话, 再运行。"); return
    for i in ids: print("TG_CHAT_ID =", i)


def build_title(msg):
    """标题: 最重要的一行 (Server酱标题就是微信通知栏预览)"""
    m = re.search(r"操作: (买入|卖出)[^\n]*", msg)
    if m: return "BTC 需要操作: " + m.group(1)
    st = re.search(r"监控状态: (YELLOW|RED)", msg)
    if st: return f"BTC 监控 {st.group(1)} 先读 PLAYBOOK"
    tgt = re.search(r"目标仓位 (\S+)", msg)
    return f"BTC 今日仓位 {tgt.group(1) if tgt else ''} 不动"


def build_daily(always=False):
    rep_path = os.path.join(OUT, "BTC_Position_Report.txt"); mon_path = os.path.join(OUT, "monitor_report.txt")
    if not os.path.exists(rep_path): return None
    rep = open(rep_path, encoding="utf-8").read()
    mon = open(mon_path, encoding="utf-8").read() if os.path.exists(mon_path) else ""
    st = json.load(open(os.path.join(BASE, "state.json"))) if os.path.exists(os.path.join(BASE, "state.json")) else {}
    prof = st.get("profile")
    head = re.search(r"^Date: .*$", rep, re.M); head = head.group(0) if head else ""
    blocks = re.findall(r"┌─ 方案 (\S+) ─+\n(.*?)└", rep, re.S)
    need_action = False; lines = ["BTC 仓位引擎", head, ""]
    for name, body in blocks:
        tag = name[0]
        if prof and tag != prof: continue
        tgt = re.search(r"目标仓位:\s+(\S+)", body); act = re.search(r"操作:\s+(.*)", body); inv = re.search(r"失效线:\s+(.*)", body)
        mark = "★" if (prof == tag) else "·"
        lines.append(f"{mark} 方案 {name}")
        lines.append(f"  目标仓位 {tgt.group(1) if tgt else '?'}")
        if act:
            a = act.group(1).strip(); lines.append(f"  操作: {a}")
            if a.startswith(("买入", "卖出")): need_action = True
        if inv: lines.append(f"  失效线: {inv.group(1).strip()}")
        lines.append("")
    status = re.search(r"综合状态: (\w+)", mon); status = status.group(1) if status else "N/A"
    lines.append(f"监控状态: {status}" + ("  ← 先读 PLAYBOOK 第4节" if status in ("YELLOW", "RED") else ""))
    if st.get("holding") is not None: lines.append(f"你记录的实际仓位: {st['holding']:.0%}")
    if not always and not need_action and status in ("GREEN", "N/A"):
        return None
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true"); ap.add_argument("--daily", action="store_true")
    ap.add_argument("--always", action="store_true"); ap.add_argument("--whoami", action="store_true")
    a = ap.parse_args()
    cfg = load_env()
    if a.whoami:
        if not cfg["tg_tok"]: print("未配置 TG_BOT_TOKEN"); return
        whoami(cfg["tg_tok"]); return
    if not any([cfg["sct"], cfg["pushplus"], cfg["tg_tok"] and cfg["tg_cid"]]):
        print("未配置任何推送渠道 (SCT_SENDKEY / PUSHPLUS_TOKEN / TG_BOT_TOKEN+TG_CHAT_ID)。跳过推送。"); return
    if a.test:
        for ch, ok in send_all("BTC 仓位引擎 测试", "测试消息 OK\n如果你看到这条, 推送已配置成功。", cfg): print(ch, "sent" if ok else "FAILED")
        return
    if a.daily:
        msg = build_daily(a.always)
        if msg is None: print("今日无需推送 (不需要操作且状态 GREEN)。"); return
        for ch, ok in send_all(build_title(msg), msg, cfg): print(ch, "sent" if ok else "FAILED")


if __name__ == "__main__":
    main()
