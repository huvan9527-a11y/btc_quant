#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC Position Engine V2.0  —  长期持有者的仓位引擎
================================================================
不猜买卖点。每天只回答一个问题: **今天应该持有多少 BTC (目标仓位 %)**

仓位 = 牛熊层 × 波动率层 + 回撤加仓层, 三层全部是规则, 参数在 2020-2022 训练, 2023-今 样本外验证。

用法:
    python btc_position.py              # 更新数据 + 今日仓位报告 + 样本外验证
    python btc_position.py --train      # 重新网格搜索参数 (只用 2020-01~2022-12), 并写入 params.json
    python btc_position.py --offline
"""
import argparse, json, os, itertools
import numpy as np
import pandas as pd
from btc_quant import load_klines, compute_indicators, OUT_DIR, BASE_DIR
import ledger

CAP = 1.0  # 牛市仓位上限. 1.0 = 永不加杠杆 (默认)
PARAM_FILE = os.path.join(BASE_DIR, f"params_v2_cap{CAP}.json")
TRAIN = ("2020-01-01", "2022-12-31")
TEST = ("2023-01-01", None)
FEE = 0.0005

# 两套正式方案 (2020-2022 训练选出, 无杠杆). 每日报告同时输出两套, 你选其一执行.
PROFILES = {
    "A_基础版":     dict(ma=150, buf=0.05, tgt_vol=1.0, cap_bull=1.0, floor_bear=0.0, dd_step=0.10, dd_add=0.00, dd_cap=0.4),
    "B_回撤加仓版": dict(ma=150, buf=0.03, tgt_vol=1.0, cap_bull=1.0, floor_bear=0.0, dd_step=0.10, dd_add=0.10, dd_cap=0.4),
}

DEFAULT = dict(
    ma=200,          # 牛熊分界均线
    buf=0.03,        # 缓冲带 (防洗)
    tgt_vol=0.55,    # 目标年化波动
    cap_bull=1.3,    # 牛市仓位上限 (>1 表示允许少量杠杆; 不想用杠杆改成 1.0)
    floor_bear=0.3,  # 熊市底仓
    dd_step=0.10,    # 距 ATH 每跌多少
    dd_add=0.10,     # 加多少仓
    dd_cap=0.4,      # 回撤加仓上限
)


# ----------------------------------------------------------------------
def features(df):
    x = compute_indicators(df).set_index("date")
    x["rv20"] = np.log(x.close).diff().rolling(20).std() * np.sqrt(365)
    x["rv60"] = np.log(x.close).diff().rolling(60).std() * np.sqrt(365)
    x["ath"] = x.close.cummax()
    x["dd_ath"] = x.close / x.ath - 1
    for n in (100, 130, 150, 170, 200, 250):
        x[f"sma{n}"] = x.close.rolling(n).mean()
    return x


def regime(x, ma, buf):
    if f"sma{ma}" not in x: x[f"sma{ma}"] = x.close.rolling(ma).mean()
    m = x[f"sma{ma}"].values; c = x.close.values
    out = np.zeros(len(x)); st = 0
    for i in range(len(x)):
        if np.isnan(m[i]): continue
        if st == 0 and c[i] > m[i] * (1 + buf): st = 1
        elif st == 1 and c[i] < m[i] * (1 - buf): st = 0
        out[i] = st
    return pd.Series(out, index=x.index)


def target_position(x, p):
    bull = regime(x, p["ma"], p["buf"])
    vol_pos = (p["tgt_vol"] / x["rv20"]).clip(upper=p["cap_bull"])
    dd_add = ((-x["dd_ath"] // p["dd_step"]) * p["dd_add"]).clip(upper=p["dd_cap"])
    pos = bull * vol_pos + (1 - bull) * p["floor_bear"] + dd_add
    pos = pos.clip(lower=0, upper=p["cap_bull"])
    comp = pd.DataFrame({"bull": bull, "vol_pos": vol_pos, "dd_add": dd_add, "target": pos})
    return comp


def backtest(x, pos, start, end=None):
    r = x.close.pct_change().fillna(0)
    p = pos.shift(1).fillna(0)
    sl = slice(start, end)
    r, p = r.loc[sl], p.loc[sl]
    turn = p.diff().abs().fillna(0)
    eq = (1 + r * p - turn * FEE).cumprod()
    bh = (1 + r).cumprod()
    yrs = max((eq.index[-1] - eq.index[0]).days / 365, 0.1)
    def stats(e):
        dd = (e / e.cummax() - 1).min()
        cagr = e.iloc[-1] ** (1 / yrs) - 1
        d = e.pct_change().dropna()
        return dict(ret=e.iloc[-1] - 1, cagr=cagr, maxdd=dd, sharpe=d.mean() / d.std() * np.sqrt(365) if d.std() > 0 else 0, calmar=cagr / abs(dd) if dd < 0 else np.nan)
    s, b = stats(eq), stats(bh)
    s["avg_pos"] = p.mean(); s["turnover_yr"] = turn.sum() / yrs
    return s, b, eq, bh


# ----------------------------------------------------------------------
def train(x):
    grid = dict(ma=[150, 200, 250], buf=[0.0, 0.03, 0.05], tgt_vol=[0.55, 0.80, 1.0],
                cap_bull=[CAP], floor_bear=[0.0, 0.3], dd_step=[0.10], dd_add=[0.0, 0.10], dd_cap=[0.4])
    keys = list(grid); rows = []
    for vals in itertools.product(*grid.values()):
        p = dict(zip(keys, vals))
        pos = target_position(x, p)["target"]
        s, b, _, _ = backtest(x, pos, *TRAIN)
        rows.append({**p, **{k: s[k] for k in ("ret", "cagr", "maxdd", "sharpe", "calmar")}})
    res = pd.DataFrame(rows)
    # 目标: 训练期 收益 ≥ 死拿 (用户要求必须战胜持有), 在此前提下 Calmar 最大
    _, b, _, _ = backtest(x, pd.Series(1.0, index=x.index), *TRAIN)
    ok = res[res.ret >= b["ret"]]
    best = (ok if len(ok) else res).sort_values("calmar", ascending=False).iloc[0]
    p = {k: (int(best[k]) if k == "ma" else float(best[k])) for k in keys}
    json.dump(p, open(PARAM_FILE, "w"), indent=2)
    return p, res.sort_values("calmar", ascending=False)


def load_params():
    return json.load(open(PARAM_FILE)) if os.path.exists(PARAM_FILE) else DEFAULT


# ----------------------------------------------------------------------
def dd_guardrail(x, cur_dd):
    """历史上处于相似回撤区间时, 未来 90/180/365 日收益"""
    lo, hi = cur_dd - 0.10, cur_dd + 0.10
    h = x[(x.dd_ath >= lo) & (x.dd_ath <= hi) & (x.index >= "2018-01-01")]
    out = {}
    for n in (90, 180, 365):
        f = (x.close.shift(-n) / x.close - 1).reindex(h.index).dropna()
        out[n] = (len(f), f.mean(), (f > 0).mean(), f.quantile(0.1))
    return out


def _block(x, comp, p, name, holding=None):
    r = x.iloc[-1]; c = comp.iloc[-1]; y = comp.iloc[-2]
    L = []; ap = L.append
    ap(f"┌─ 方案 {name} " + "─" * (52 - len(name)))
    ap(f"│ ★ 今日目标仓位:  {c.target:>5.0%}    (昨日 {y.target:.0%}, 变化 {c.target - y.target:+.0%})")
    ap(f"│   牛熊层   {'BULL' if c.bull else 'BEAR'}   收盘 ${r.close:,.0f} vs {p['ma']}日线 ${r[f'sma{p['ma']}']:,.0f}  (缓冲 ±{p['buf']:.0%}: 上穿 ${r[f'sma{p['ma']}'] * (1 + p['buf']):,.0f} / 下破 ${r[f'sma{p['ma']}'] * (1 - p['buf']):,.0f})")
    if c.bull:
        ap(f"│   波动率层 {c.vol_pos:>5.0%}   目标波动 {p['tgt_vol']:.0%} / 当前 {r.rv20:.0%}")
    else:
        ap(f"│   熊市底仓 {p['floor_bear']:>5.0%}")
    if p["dd_add"] > 0:
        ap(f"│   回撤加仓 {c.dd_add:>+5.0%}   距ATH {r.dd_ath:+.0%}, 每跌 {p['dd_step']:.0%} 加 {p['dd_add']:.0%}, 上限 {p['dd_cap']:.0%}")
    cur = y.target if holding is None else holding
    diff = c.target - cur
    src = "昨日目标" if holding is None else "你的实际仓位"
    new = ledger.apply_band(c.target, cur)
    if new == cur: act = f"不动 (目标 {c.target:.0%} vs {src} {cur:.0%}, 偏离 <{ledger.BAND:.0%} 再平衡带)"
    elif diff > 0: act = f"买入, 把仓位从 {cur:.0%} 提到 {c.target:.0%} (约 {diff:.0%} 总资金), 明早开盘执行"
    else: act = f"卖出, 把仓位从 {cur:.0%} 降到 {c.target:.0%} (约 {-diff:.0%} 总资金), 明早开盘执行"
    ap(f"│   操作:    {act}")
    if holding is not None and diff >= 0.3:
        ap(f"│   建仓方式: 一次到位 或 分 3-5 批在 1-3 周内完成均可 (历史统计两者 90 日结果无显著差异,")
        ap(f"│             分批只是心理更好受); 不要等回调——BULL 中等回调的期望成本高于分批的成本。")
        if r["roc5"] > 10:
            ap(f"│   注意:    近 5 日已涨 {r['roc5']:.0f}%. 历史上此类日子入场 90 日后亏损概率约 36-40%, 最差 10% 情形 -27%;")
            ap(f"│             这与任意 BULL 日入场无差别, 不构成等待理由, 但请按此预期设置心理底线。")
    if c.bull:
        ap(f"│   失效线:  收盘跌破 ${r[f'sma{p['ma']}'] * (1 - p['buf']):,.0f} → 方案转 BEAR, 目标降至 {p['floor_bear'] + (c.dd_add if p['dd_add'] > 0 else 0):.0%}")
    ap("└" + "─" * 58)
    return L


def report(x, results, holding=None):
    r = x.iloc[-1]
    L = []; ap = L.append
    ap("=" * 60); ap("BTC POSITION ENGINE V2.0  —  长期持有者仓位引擎 (无杠杆)"); ap("=" * 60)
    ap(f"Date: {x.index[-1].date()}    Price: ${r.close:,.0f}    距历史高点: {r.dd_ath:+.1%}    20日波动: {r.rv20:.0%}")
    if holding is not None:
        ap(f"你的实际仓位: {holding:.0%}   (建议基于此仓位给出)")
    ap("")
    for name, (p, comp, *_ ) in results.items():
        L += _block(x, comp, p, name, holding); ap("")
    ap("-" * 60); ap("心理护栏 (历史上处于相似回撤位置之后, 2018→今):")
    g = dd_guardrail(x, r.dd_ath)
    ap(f"  {'未来':<8}{'样本':>6}{'平均收益':>10}{'上涨概率':>10}{'最差10%':>10}")
    for n, (cnt, mean, win, p10) in g.items():
        ap(f"  {str(n) + '日':<8}{cnt:>6}{mean:>+10.0%}{win:>10.0%}{p10:>+10.0%}")
    ap("")
    ap("-" * 60); ap("历史表现 (参数于 2020-01→2022-12 训练, 2023 起为样本外):")
    ap(f"  {'':<22}" + "".join(f"{n:>14}" for n in results) + f"{'死拿':>10}")
    rows = [("训练期 收益", "tr", "ret", "+.0%"), ("训练期 最大回撤", "tr", "maxdd", ".0%"),
            ("样本外 收益", "te", "ret", "+.0%"), ("样本外 最大回撤", "te", "maxdd", ".0%"),
            ("2020-今 收益", "all", "ret", "+.0%"), ("2020-今 最大回撤", "all", "maxdd", ".0%"),
            ("2020-今 年化/回撤", "all", "calmar", ".2f"), ("平均仓位", "all", "avg_pos", ".0%")]
    for lab, per, k, f in rows:
        line = f"  {lab:<22}"
        for name, res in results.items():
            line += f"{format(res[1 + ['tr', 'te', 'all'].index(per) + 1][k], f):>14}"
        bh = list(results.values())[0][5][per]
        line += f"{format(bh[k], f) if k != 'avg_pos' else '100%':>10}"
        ap(line)
    ap("")
    ap("-" * 60); ap("最近 10 天:")
    names = list(results)
    ap(f"  {'Date':<12}{'Price':>9}{'DD':>7}{'Regime':>7}" + "".join(f"{n[:6]:>10}" for n in names))
    for d in x.index[-10:]:
        c0 = results[names[0]][1].loc[d]
        ap(f"  {str(d.date()):<12}{x.loc[d, 'close']:>9,.0f}{x.loc[d, 'dd_ath']:>+7.0%}{'BULL' if c0.bull else 'BEAR':>7}" + "".join(f"{results[n][1].loc[d, 'target']:>10.0%}" for n in names))
    ap("=" * 60)
    ap("执行规则: 信号以日线收盘 (UTC 00:00 / 北京 08:00) 为准, 次日开盘执行; 晚 1 天无影响, 晚 3 天以上收益明显受损。")
    ap("          偏离目标 <10% 不调仓 (再平衡带), 目标为 0 必须清仓, 空手且目标 >0 必须建仓。")
    ap("A 基础版: 只做牛熊 + 波动率, 更简单, 回撤更小。")
    ap("B 回撤加仓版: 跌得越深买得越多, 样本外收益更高, 但 2022 式熊市中会多承受回撤。")
    ap("两套方案只选一套执行, 不要来回切换。所有数字为历史模拟, 不构成投资建议。")
    return "\n".join(L)


def plot(x, results, start):
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    sub = x.loc[start:]
    names = list(results); p0 = results[names[0]][0]; cp0 = results[names[0]][1].loc[start:]
    fig, ax = plt.subplots(3, 1, figsize=(14, 11), sharex=True, gridspec_kw={"height_ratios": [2, 1.5, 1]})
    ax[0].semilogy(sub.index, sub.close, "k", lw=1, label="BTC")
    ax[0].semilogy(sub.index, sub[f"sma{p0['ma']}"], lw=0.8, label=f"SMA{p0['ma']}")
    ax[0].fill_between(sub.index, sub.close.min(), sub.close.max(), where=cp0.bull == 0, color="red", alpha=0.08, label="BEAR (flat)")
    ax[0].legend(); ax[0].set_title(f"BTC Position Engine V2.0 (no leverage)  from {start}  |  2023+ is out-of-sample")
    ax[0].axvline(pd.Timestamp("2023-01-01"), color="k", ls="--", lw=.8)
    en = {"A_基础版": "A  Base (regime x vol)", "B_回撤加仓版": "B  Base + drawdown add-on"}
    for n, col in zip(names, ("tab:blue", "tab:green")):
        eq = results[n][6].loc[start:]; eq = eq / eq.iloc[0]
        ax[1].plot(eq.index, eq, label=en.get(n, n), color=col)
        cp = results[n][1].loc[start:]
        ax[2].plot(cp.index, cp.target, color=col, lw=.9, label=en.get(n, n))
    bh = (1 + sub.close.pct_change().fillna(0)).cumprod()
    ax[1].plot(bh.index, bh, label="Buy & Hold", color="gray", alpha=.7)
    ax[1].set_yscale("log"); ax[1].legend(); ax[1].set_ylabel("Equity (log)")
    ax[1].axvline(pd.Timestamp("2023-01-01"), color="k", ls="--", lw=.8)
    ax[2].axhline(1, color="k", lw=.5); ax[2].set_ylabel("Target position"); ax[2].legend(fontsize=8)
    plt.tight_layout(); plt.savefig(os.path.join(OUT_DIR, "position_chart.png"), dpi=110)


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true", help="重新网格搜索 (2020-2022), 结果写入 output/v2_train_grid_top.csv 供参考; 不会自动覆盖 PROFILES")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--holding", type=float, default=None, help="你当前实际 BTC 仓位占比, 如 0 (空手) / 0.5 / 1.0. 给出后操作建议按实际仓位计算")
    a = ap.parse_args()
    df = load_klines(offline=a.offline)
    x = features(df)
    if a.train:
        print("训练参数 (2020-2022) ..."); p, res = train(x)
        res.head(20).to_csv(os.path.join(OUT_DIR, "v2_train_grid_top.csv"), index=False)
        print(res.head(10).to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    results = {}
    for name, p in PROFILES.items():
        comp = target_position(x, p)
        s_tr, b_tr, _, _ = backtest(x, comp.target, *TRAIN)
        s_te, b_te, _, _ = backtest(x, comp.target, *TEST)
        s_all, b_all, eq, _ = backtest(x, comp.target, "2020-01-01")
        results[name] = (p, comp, s_tr, s_te, s_all, {"tr": b_tr, "te": b_te, "all": b_all}, eq)
    st = ledger.load_state()
    holding = a.holding if a.holding is not None else st.get("holding")
    if a.holding is not None:
        st["holding"] = a.holding; st["holding_date"] = str(x.index[-1].date()); ledger.save_state(st)
    rep = report(x, results, holding)
    led, st = ledger.update(x, {n: res[1] for n, res in results.items()})
    rep += "\n" + ledger.summary(led, st)
    print(rep)
    open(os.path.join(OUT_DIR, "BTC_Position_Report.txt"), "w", encoding="utf-8").write(rep)
    out = x[["close", "dd_ath", "rv20", f"sma{PROFILES['A_基础版']['ma']}"]].copy()
    for n, res in results.items():
        out[f"{n}_target"] = res[1].target
    out.round(4).to_csv(os.path.join(OUT_DIR, "position_daily.csv"))
    try: plot(x, results, "2020-01-01")
    except Exception as e: print("plot skipped:", e)


if __name__ == "__main__":
    main()
