#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
偏离监控  —  回答 "现在的亏损是正常回撤, 还是系统失效?"
================================================================
把 paper ledger / 你的实盘净值 与 2018→今 历史分布对比:
  - 绝对收益 (30/60/90/180/365 日) 处于历史第几百分位
  - 相对死拿 的差值 处于第几百分位
  - 当前回撤 vs 历史回撤分布
  - 三级信号:  GREEN 正常 / YELLOW 处于历史 5% 尾部, 观察 / RED 突破历史最差, 需要复核

用法:
  python monitor.py                 # 用 paper ledger
  python monitor.py --equity my_equity.csv   # 用你的实盘净值 (两列: date, equity)
"""
import os, sys, json, argparse
import numpy as np, pandas as pd
import btc_position as P, ledger

OUT = P.OUT_DIR
BASE_FILE = os.path.join(OUT, "baseline_distribution.json")
WINDOWS = (30, 60, 90, 180, 365)


def build_baseline(x):
    r = x.close.pct_change().fillna(0); base = {}
    for name in P.PROFILES:
        pos = P.target_position(x, P.PROFILES[name]).target.shift(1).fillna(0)
        eq = (1 + r * pos).cumprod().loc["2018-01-01":]; bh = (1 + r).cumprod().loc["2018-01-01":]
        base[name] = {}
        for n in WINDOWS:
            sr = (eq / eq.shift(n) - 1).dropna(); br = (bh / bh.shift(n) - 1).dropna(); rel = sr - br
            dd = eq.rolling(n).apply(lambda v: (v / np.maximum.accumulate(v) - 1).min(), raw=True).dropna()
            base[name][str(n)] = dict(ret=sr.values.tolist(), rel=rel.values.tolist(), dd=dd.values.tolist())
    json.dump(base, open(BASE_FILE, "w"))
    return base


def pct_rank(arr, v):
    arr = np.asarray(arr); return float((arr < v).mean())


def evaluate(eq, bh, base_name, base):
    """eq/bh: 净值序列 (同索引). 返回每个窗口的评估"""
    rows = []; worst = "GREEN"
    n_have = len(eq)
    # 自起始以来的总回撤 (与历史全程最大回撤比)
    dd_all = (eq / eq.cummax() - 1).min(); cur_dd = eq.iloc[-1] / eq.max() - 1
    hist_worst_dd = min(base[base_name]["365"]["dd"])
    rows.append(dict(n=0, ret=eq.iloc[-1] - 1, p_ret=float("nan"), rel=eq.iloc[-1] / bh.iloc[-1] - 1, p_rel=float("nan"), dd=cur_dd, p_dd=float("nan"),
                     worst_ret=float("nan"), worst_rel=float("nan"), worst_dd=hist_worst_dd, level="RED" if dd_all < hist_worst_dd else ("YELLOW" if dd_all < 0.8 * hist_worst_dd else "GREEN")))
    if rows[0]["level"] != "GREEN": worst = rows[0]["level"]
    for n in WINDOWS:
        if n_have <= n: continue
        sr = eq.iloc[-1] / eq.iloc[-1 - n] - 1; br = bh.iloc[-1] / bh.iloc[-1 - n] - 1; rel = sr - br
        dd = (eq.iloc[-n:] / eq.iloc[-n:].cummax() - 1).min()
        b = base[base_name][str(n)]
        p_ret, p_rel, p_dd = pct_rank(b["ret"], sr), pct_rank(b["rel"], rel), pct_rank(b["dd"], dd)
        lvl = "GREEN"
        if p_ret < 0.05 or p_rel < 0.05 or p_dd < 0.05: lvl = "YELLOW"
        if sr < min(b["ret"]) or rel < min(b["rel"]) or dd < min(b["dd"]): lvl = "RED"
        rows.append(dict(n=n, ret=sr, p_ret=p_ret, rel=rel, p_rel=p_rel, dd=dd, p_dd=p_dd, worst_ret=min(b["ret"]), worst_rel=min(b["rel"]), worst_dd=min(b["dd"]), level=lvl))
        if lvl == "RED" or (lvl == "YELLOW" and worst == "GREEN"): worst = lvl
    return rows, worst


def report(rows, worst, name, n_days, src):
    n_have = n_days
    L = []; ap = L.append
    ap("=" * 64); ap(f"偏离监控  —  {name}   数据源: {src}   样本 {n_days} 天"); ap("=" * 64)
    if len(rows) <= 1 and n_days < 30:
        ap("  记录不足 30 天, 暂无法评估。继续累积。"); return "\n".join(L)
    ap(f"  {'窗口':<6}{'收益':>8}{'百分位':>7}{'相对死拿':>9}{'百分位':>7}{'回撤':>8}{'百分位':>7}{'状态':>8}")
    for r in rows:
        if r["n"] == 0:
            ap(f"  {'累计':<6}{r['ret']:>+8.1%}{'':>7}{r['rel']:>+9.1%}{'':>7}{r['dd']:>8.1%}{'':>7}{r['level']:>8}   (当前回撤; 历史最差 {r['worst_dd']:.0%}, 80% 处 {0.8 * r['worst_dd']:.0%} 为黄线)")
        else:
            ap(f"  {str(r['n']) + '日':<6}{r['ret']:>+8.1%}{r['p_ret']:>7.0%}{r['rel']:>+9.1%}{r['p_rel']:>7.0%}{r['dd']:>8.1%}{r['p_dd']:>7.0%}{r['level']:>8}")
    ap(""); ap(f"  综合状态: {worst}")
    if worst == "GREEN":
        ap("  → 所有指标在历史 5%~95% 区间内。无论盈亏, 这都是系统的正常行为。按规则继续。")
    elif worst == "YELLOW":
        ap("  → 至少一项指标落入历史最差 5%。历史上这种情况发生过 (2018/2021-05/2025), 之后系统都恢复了。")
        ap("    不要改参数、不要切方案。允许的动作: 复核数据是否正确、确认执行没有延迟。")
    else:
        ap("  → 至少一项指标突破了 2018 以来的历史最差值。这可能意味着市场结构改变 (例如 BTC 波动率永久下降,")
        ap("    或趋势特性消失)。允许的动作: 停止加仓, 保留现有仓位, 用 robustness.py 重新检验; 不要在恐慌中清仓。")
    ap(""); ap("  百分位含义: 收益/相对/回撤 在 2018→今 所有同长度窗口中的排位, 0% = 历史最差, 50% = 中位。")
    ap("  基线来自策略自身的历史模拟, 不是死拿的。'相对死拿 百分位低' 在牛市里是常态 (系统本就在牛市跑输)。")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--equity", default=None); ap.add_argument("--profile", default=None); ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    x = P.features(P.load_klines(offline=a.offline))
    base = json.load(open(BASE_FILE)) if os.path.exists(BASE_FILE) else build_baseline(x)
    st = ledger.load_state(); prof = (a.profile or st.get("profile") or "A").upper()
    name = "A_基础版" if prof == "A" else "B_回撤加仓版"
    if a.equity:
        e = pd.read_csv(a.equity, parse_dates=["date"]).set_index("date")["equity"]
        bh = x.close.reindex(e.index); bh = bh / bh.iloc[0]; src = a.equity
    else:
        led = ledger.load_ledger()
        if len(led) == 0: print("账本为空, 先运行 btc_position.py"); return
        e = led.set_index("date")[f"{prof}_eq"]; bh = led.set_index("date")["BH_eq"]; src = "paper_ledger.csv"
    rows, worst = evaluate(e, bh, name, base)
    rep = report(rows, worst, name, len(e), src); print(rep)
    open(os.path.join(OUT, "monitor_report.txt"), "w", encoding="utf-8").write(rep)


if __name__ == "__main__":
    main()
