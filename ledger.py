#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paper Trading 账本 + 实盘状态
================================================================
每天由 btc_position.py 自动调用 (也可单独运行):
  - 记录当天两套方案的目标仓位、模拟成交 (次日开盘价 + 0.15% 成本)、模拟净值
  - 与死拿对比, 累计真正的样本外 track record
  - 维护 state.json: 你的真实仓位 / 选定方案, 报告据此给建议

命令:
  python ledger.py show                # 查看账本摘要
  python ledger.py set-holding 0.6     # 记录你实际仓位为 60%
  python ledger.py set-profile B       # 选定执行方案 A / B
  python ledger.py reset               # 清空账本 (慎用)
"""
import os, sys, json
import pandas as pd, numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(BASE, "paper_ledger.csv")
STATE = os.path.join(BASE, "state.json")
COST = 0.0015          # 手续费 0.1% + 滑点 0.05%
BAND = 0.10            # 再平衡带: 偏离目标 <10% 不动 (2020-26 测试: 收益无损, 交易次数减半)


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"profile": None, "holding": None, "holding_date": None, "started": None, "notes": []}


def save_state(s):
    json.dump(s, open(STATE, "w"), indent=2, ensure_ascii=False)


def load_ledger():
    if os.path.exists(LEDGER):
        return pd.read_csv(LEDGER, parse_dates=["date"])
    return pd.DataFrame(columns=["date", "close", "open_next", "A_target", "B_target", "A_pos", "B_pos", "A_eq", "B_eq", "BH_eq", "A_action", "B_action"])


def apply_band(target, held):
    """再平衡带: 目标与持仓差 >= BAND 才调; 目标为 0 时必须清空; 从 0 建仓时必须建"""
    if held == 0 and target > 0: return target
    if target == 0 and held > 0: return 0.0
    return target if abs(target - held) >= BAND else held


def update(x, comps):
    """x: features df (index=date); comps: {name: comp df}. 只追加已收盘的日子。"""
    led = load_ledger()
    st = load_state()
    if st["started"] is None:
        st["started"] = str(x.index[-1].date()); save_state(st)
    last = led["date"].max() if len(led) else None
    # 已收盘 = 不是今天 (UTC)
    today = pd.Timestamp.utcnow().tz_localize(None).normalize()
    closed = x.index[x.index < today]
    new_days = [d for d in closed if last is None or d > last]
    if last is None:
        new_days = new_days[-1:]        # 首次运行只记最新一根已收盘 K 线, 从此开始累积
    if not new_days:
        return led, st
    A_pos = led["A_pos"].iloc[-1] if len(led) else 0.0
    B_pos = led["B_pos"].iloc[-1] if len(led) else 0.0
    A_eq = led["A_eq"].iloc[-1] if len(led) else 1.0
    B_eq = led["B_eq"].iloc[-1] if len(led) else 1.0
    BH_eq = led["BH_eq"].iloc[-1] if len(led) else 1.0
    prev_close = x.loc[led["date"].iloc[-1], "close"] if len(led) else None
    rows = []
    for d in new_days:
        close = x.loc[d, "close"]
        # 用上一日持仓吃到今天的收益 (close-to-close 近似; 首日无收益)
        if prev_close is not None:
            r = close / prev_close - 1
            A_eq *= 1 + A_pos * r; B_eq *= 1 + B_pos * r; BH_eq *= 1 + r
        tA = float(comps["A_基础版"].loc[d, "target"]); tB = float(comps["B_回撤加仓版"].loc[d, "target"])
        nA = apply_band(tA, A_pos); nB = apply_band(tB, B_pos)
        A_eq *= 1 - abs(nA - A_pos) * COST; B_eq *= 1 - abs(nB - B_pos) * COST
        actA = "HOLD" if nA == A_pos else ("BUY" if nA > A_pos else "SELL") + f" {abs(nA - A_pos):.0%}"
        actB = "HOLD" if nB == B_pos else ("BUY" if nB > B_pos else "SELL") + f" {abs(nB - B_pos):.0%}"
        A_pos, B_pos = nA, nB
        i = x.index.get_loc(d)
        open_next = x["open"].iloc[i + 1] if i + 1 < len(x) else np.nan
        rows.append(dict(date=d, close=close, open_next=open_next, A_target=tA, B_target=tB, A_pos=A_pos, B_pos=B_pos,
                         A_eq=A_eq, B_eq=B_eq, BH_eq=BH_eq, A_action=actA, B_action=actB))
        prev_close = close
    led = pd.concat([led, pd.DataFrame(rows)], ignore_index=True)
    led.to_csv(LEDGER, index=False)
    return led, st


def summary(led, st):
    L = []
    ap = L.append
    ap("-" * 60); ap("PAPER TRADING 账本 (真实样本外, 次日开盘 + 0.15% 成本, 10% 再平衡带)")
    if len(led) == 0:
        ap("  尚无记录。"); return "\n".join(L)
    d0, d1 = led["date"].iloc[0].date(), led["date"].iloc[-1].date()
    n = len(led); last = led.iloc[-1]
    ap(f"  起始 {d0}  →  最新 {d1}   ({n} 个交易日)")
    ap(f"  {'':<10}{'净值':>8}{'收益':>8}{'最大回撤':>9}{'当前仓位':>9}{'交易次数':>8}")
    for k in ("A", "B", "BH"):
        eq = led[f"{k}_eq"]; dd = (eq / eq.cummax() - 1).min()
        pos = last[f"{k}_pos"] if k != "BH" else 1.0
        nt = (led[f"{k}_action"] != "HOLD").sum() if k != "BH" else 1
        name = {"A": "A 基础版", "B": "B 回撤加仓", "BH": "死拿"}[k]
        ap(f"  {name:<10}{eq.iloc[-1]:>8.3f}{eq.iloc[-1] - 1:>+8.1%}{dd:>9.1%}{pos:>9.0%}{nt:>8}")
    recent = led.tail(5)
    ap("  最近操作:")
    for _, r in recent.iterrows():
        if r.A_action != "HOLD" or r.B_action != "HOLD":
            ap(f"    {r.date.date()}  A: {r.A_action:<10} B: {r.B_action:<10} @ 次日开盘 ${r.open_next:,.0f}" if not np.isnan(r.open_next) else f"    {r.date.date()}  A: {r.A_action:<10} B: {r.B_action}")
    if st.get("profile"):
        ap(f"  你选定的方案: {st['profile']}    实际仓位: {st['holding']:.0%} (记录于 {st['holding_date']})" if st.get("holding") is not None else f"  你选定的方案: {st['profile']}    实际仓位: 未记录 (python ledger.py set-holding 0.x)")
    else:
        ap("  尚未选定执行方案: python ledger.py set-profile A|B")
    return "\n".join(L)


def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    cmd = sys.argv[1]; st = load_state()
    if cmd == "show":
        print(summary(load_ledger(), st))
    elif cmd == "set-holding":
        st["holding"] = float(sys.argv[2]); st["holding_date"] = str(pd.Timestamp.utcnow().date()); save_state(st); print("已记录实际仓位", st["holding"])
    elif cmd == "set-profile":
        p = sys.argv[2].upper(); assert p in ("A", "B"); st["profile"] = p; save_state(st); print("已选定方案", p)
    elif cmd == "reset":
        for f in (LEDGER, STATE):
            if os.path.exists(f): os.remove(f)
        print("已清空")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
