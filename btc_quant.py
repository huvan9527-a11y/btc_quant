#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC Quant V1.0  —  BTC 日线 多因子评分 / 大周期Regime / 突破交易 / 回测
================================================================
用法:
    python btc_quant.py                 # 更新数据 + 生成今日报告 + 回测 (默认从2025-01-01)
    python btc_quant.py --start 2024-01-01
    python btc_quant.py --offline       # 只用本地缓存，不联网
    python btc_quant.py --focus 2025-08-15 2025-08-25   # 重点查看某段时间每日信号

输出 (同目录 output/):
    BTC_Quant_Report.txt     今日报告
    btc_daily_signals.csv    每日全部因子 / 分数 / 信号 / 前瞻收益
    backtest_report.txt      回测统计
    data/BTCUSDT_1d.csv      本地K线缓存 (增量更新)

数据源: Binance 公开 Kline 接口, 无需 API Key.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUT_DIR = os.path.join(BASE_DIR, "output")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

SYMBOL = "BTCUSDT"
INTERVAL = "1d"
CACHE = os.path.join(DATA_DIR, f"{SYMBOL}_{INTERVAL}.csv")

# 多端点回退：api.binance.com 在部分地区被限制，data-api.binance.vision 是官方公开数据镜像
ENDPOINTS = [
    "https://data-api.binance.vision/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
    "https://api1.binance.com/api/v3/klines",
    "https://api2.binance.com/api/v3/klines",
    "https://api3.binance.com/api/v3/klines",
]

# ---- 信号阈值 (V1.1) ----
TH_STRONG = 78        # V1.0: 85
TH_BREAKOUT = 70      # V1.0: 72
TH_EARLY = 62
TH_EARLY_RECOVERY = 99  # 底部首日放宽 (2025-26样本内多3笔假启动亏损, 默认关闭; 想开启改成52)
CHANDELIER_ATR = 4.0  # 吊灯止损: 持仓期间最高收盘 - 3 ATR
# ---- V1.2 出场 ----
EXIT_MODE = "regime"  # "tight" = V1.1 (统一 EMA20 退出) ; "regime" = BULL 中放宽为 EMA50/吊灯, 其他保持紧
TP_MAX_PER_TRADE = 0  # 每笔交易最多减仓次数. 网格测试: 0 最优 (2023-26: 143% vs 1次 89% vs 不限 67%), 减仓只会砍掉主升浪
TP_NEED_EXHAUSTION = True  # TAKE_PROFIT 需要衰竭K线确认 (阴线 / 长上影 / 放量滞涨), 否则只发警告继续 HOLD

WEIGHTS = {
    "Trend": 0.25,
    "Breakout": 0.25,
    "Momentum": 0.15,
    "Volume": 0.15,
    "Compression": 0.10,
    "Structure": 0.10,
}

# ----------------------------------------------------------------------
# 1. 数据获取
# ----------------------------------------------------------------------
KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
              "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]


def _fetch_chunk(start_ms, limit=1000):
    params = {"symbol": SYMBOL, "interval": INTERVAL, "startTime": int(start_ms), "limit": limit}
    last_err = None
    for url in ENDPOINTS:
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 200:
                js = r.json()
                if isinstance(js, list):
                    return js
            last_err = f"{url} -> HTTP {r.status_code}: {r.text[:120]}"
        except Exception as e:  # noqa
            last_err = f"{url} -> {e}"
    raise RuntimeError(f"所有 Binance 端点均失败: {last_err}")


def load_klines(offline=False, since="2017-08-17"):
    df_old = None
    if os.path.exists(CACHE):
        df_old = pd.read_csv(CACHE, parse_dates=["date"])
    if offline:
        if df_old is None:
            sys.exit("离线模式但没有本地缓存数据。")
        return df_old

    if df_old is not None and len(df_old) > 0:
        # 从最后一根（可能未收盘）重新拉取
        start_ms = int(df_old["open_time"].iloc[-1])
    else:
        start_ms = int(pd.Timestamp(since, tz="UTC").timestamp() * 1000)

    rows = []
    while True:
        chunk = _fetch_chunk(start_ms)
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        start_ms = chunk[-1][0] + 1
        time.sleep(0.2)

    df_new = pd.DataFrame(rows, columns=KLINE_COLS)
    for c in ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base", "taker_buy_quote"]:
        df_new[c] = df_new[c].astype(float)
    df_new["trades"] = df_new["trades"].astype(int)
    df_new["date"] = pd.to_datetime(df_new["open_time"], unit="ms", utc=True).dt.tz_localize(None)
    df_new = df_new[["date", "open_time", "open", "high", "low", "close", "volume", "quote_volume", "trades"]]

    if df_old is not None:
        df = pd.concat([df_old, df_new]).drop_duplicates("open_time", keep="last")
    else:
        df = df_new
    df = df.sort_values("open_time").reset_index(drop=True)
    df.to_csv(CACHE, index=False)
    return df


# ----------------------------------------------------------------------
# 2. 指标
# ----------------------------------------------------------------------
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n):
    d = s.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    ru = up.ewm(alpha=1 / n, adjust=False).mean()
    rd = dn.ewm(alpha=1 / n, adjust=False).mean()
    rs = ru / rd.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def compute_indicators(df):
    d = df.copy()
    c = d["close"]
    for n in (20, 50, 100, 200):
        d[f"ema{n}"] = ema(c, n)
        d[f"ema{n}_slope"] = d[f"ema{n}"].pct_change(5) * 100  # 5日斜率 %
        d[f"c_ema{n}"] = c / d[f"ema{n}"] - 1
    d["rsi14"] = rsi(c, 14)
    d["rsi7"] = rsi(c, 7)
    for n in (3, 5, 10, 20):
        d[f"roc{n}"] = c.pct_change(n) * 100
    d["macd"] = ema(c, 12) - ema(c, 26)
    d["macd_sig"] = ema(d["macd"], 9)
    d["macd_hist"] = d["macd"] - d["macd_sig"]
    d["macd_hist_acc"] = d["macd_hist"].diff()
    d["atr14"] = atr(d, 14)
    d["atr_pct"] = d["atr14"] / c * 100
    d["atr_pct_pctile"] = d["atr_pct"].rolling(120).rank(pct=True)
    # Bollinger
    ma20 = c.rolling(20).mean()
    sd20 = c.rolling(20).std()
    d["bb_up"] = ma20 + 2 * sd20
    d["bb_dn"] = ma20 - 2 * sd20
    d["bb_width"] = (d["bb_up"] - d["bb_dn"]) / ma20 * 100
    d["bb_width_pctile"] = d["bb_width"].rolling(120).rank(pct=True)
    d["bb_pctb"] = (c - d["bb_dn"]) / (d["bb_up"] - d["bb_dn"])
    # realized vol
    lr = np.log(c / c.shift(1))
    d["rv20"] = lr.rolling(20).std() * np.sqrt(365) * 100
    d["rv20_pctile"] = d["rv20"].rolling(120).rank(pct=True)
    # volume
    d["vol20"] = d["volume"].rolling(20).mean()
    d["vol_ratio"] = d["volume"] / d["vol20"]
    d["qvol20"] = d["quote_volume"].rolling(20).mean()
    d["qvol_ratio"] = d["quote_volume"] / d["qvol20"]
    d["vol_acc"] = d["volume"].rolling(5).mean() / d["volume"].rolling(20).mean()
    d["obv"] = (np.sign(c.diff()).fillna(0) * d["volume"]).cumsum()
    d["obv_ema20"] = ema(d["obv"], 20)
    d["obv_slope"] = d["obv"].diff(10)
    # rolling highs (prior N days, excluding today)
    for n in (20, 30, 55, 90, 120):
        d[f"hi{n}"] = d["high"].shift(1).rolling(n).max()
        d[f"lo{n}"] = d["low"].shift(1).rolling(n).min()
        d[f"brk{n}"] = c > d[f"hi{n}"]
    d["close_pos"] = (c - d["low"]) / (d["high"] - d["low"]).replace(0, np.nan)
    d["range_pct"] = (d["high"] - d["low"]) / c * 100
    d["ext_atr"] = (c - d["ema20"]) / d["atr14"]  # 距 EMA20 多少个 ATR
    d["dd_from_ath"] = c / c.cummax() - 1
    d["hi120_all"] = c.rolling(120).max()
    # 箱体长度: 连续多少天没有创55日新高 (突破前)
    new_hi = (c > c.shift(1).rolling(55).max()).astype(int)
    grp = new_hi.cumsum()
    d["days_since_55hi"] = d.groupby(grp).cumcount()
    # 前高测试次数: 过去60天有多少天 high 进入 hi55 的 2% 范围内且未突破
    near = ((d["high"] >= d["hi55"] * 0.98) & (c <= d["hi55"])).astype(int)
    d["hi_tests"] = near.rolling(60).sum()
    return d


# ----------------------------------------------------------------------
# 3. 市场结构 (swing highs/lows)
# ----------------------------------------------------------------------
def swing_structure(df, lookback, k=3):
    """在过去 lookback 天内找 swing high/low (k 左右各k根), 返回 -1..1 结构分"""
    h = df["high"].values
    l = df["low"].values
    n = len(df)
    out = np.full(n, np.nan)
    is_sh = np.zeros(n, bool)
    is_sl = np.zeros(n, bool)
    for i in range(k, n - k):
        if h[i] == h[i - k:i + k + 1].max():
            is_sh[i] = True
        if l[i] == l[i - k:i + k + 1].min():
            is_sl[i] = True
    sh_idx = np.where(is_sh)[0]
    sl_idx = np.where(is_sl)[0]
    for i in range(lookback, n):
        # 只能使用 i-k 之前确认的 swing
        shs = sh_idx[(sh_idx >= i - lookback) & (sh_idx <= i - k)]
        sls = sl_idx[(sl_idx >= i - lookback) & (sl_idx <= i - k)]
        s = 0.0
        cnt = 0
        if len(shs) >= 2:
            s += 1 if h[shs[-1]] > h[shs[-2]] else -1
            cnt += 1
        if len(sls) >= 2:
            s += 1 if l[sls[-1]] > l[sls[-2]] else -1
            cnt += 1
        # 当前价 vs 最近 swing high
        if len(shs) >= 1:
            c_now = df["close"].iat[i]
            if c_now > h[shs].max():      # 突破区间内所有前高 -> 结构性反转
                s += 1.0
            elif c_now > h[shs[-1]]:
                s += 0.5
            else:
                s -= 0.5
            cnt += 1.0
        if cnt > 0:
            out[i] = s / (cnt if cnt >= 1 else 1)
    return pd.Series(out, index=df.index).clip(-1, 1)


# ----------------------------------------------------------------------
# 4. 因子打分 (每个 0-100)
# ----------------------------------------------------------------------
def clip01(x, lo, hi):
    return np.clip((x - lo) / (hi - lo), 0, 1)


def score_trend(d):
    c = d["close"]
    s = pd.Series(0.0, index=d.index)
    # MA 排列 (40)
    s += 10 * (c > d["ema20"]) + 10 * (d["ema20"] > d["ema50"]) + 10 * (d["ema50"] > d["ema100"]) + 10 * (d["ema100"] > d["ema200"])
    # 斜率 (30)
    s += 10 * clip01(d["ema20_slope"], -1, 2) + 10 * clip01(d["ema50_slope"], -0.5, 1.5) + 10 * clip01(d["ema200_slope"], -0.3, 0.8)
    # 价格位置 (30)
    s += 10 * clip01(d["c_ema50"], -0.05, 0.08) + 10 * clip01(d["c_ema200"], -0.10, 0.20) + 10 * clip01(d["c_ema20"], -0.03, 0.05)
    return s.clip(0, 100)


def score_breakout(d):
    c = d["close"]
    s = pd.Series(0.0, index=d.index)
    # 突破级别 (45): 20D 10, 30D 8, 55D 12, 90D 8, 120D 7
    s += 10 * d["brk20"] + 8 * d["brk30"] + 12 * d["brk55"] + 8 * d["brk90"] + 7 * d["brk120"]
    # 距 55D 前高距离 (未突破也给部分分：接近前高) (10)
    dist55 = c / d["hi55"] - 1
    s += 10 * clip01(dist55, -0.06, 0.0)
    # 突破幅度 (10): 超过前高 1%~4%
    mag = np.where(d["brk20"], c / d["hi20"] - 1, 0)
    s += 10 * clip01(mag, 0, 0.03)
    # 收盘位置 (10)
    s += 10 * clip01(d["close_pos"], 0.4, 0.85)
    # 突破前箱体长度 (10): 憋得越久越好
    s += 10 * clip01(d["days_since_55hi"].shift(1).fillna(0), 5, 40) * d["brk55"]
    # 前高测试次数 (5)
    s += 5 * clip01(d["hi_tests"], 0, 4) * d["brk55"]
    # 近5日内曾发生突破 (延续) (10)
    recent = d["brk55"].rolling(5).max().fillna(0)
    s += 10 * recent * (c > d["hi55"].shift(5).fillna(np.inf) * 0.99).astype(float)
    return s.clip(0, 100)


def score_momentum(d):
    s = pd.Series(0.0, index=d.index)
    s += 20 * clip01(d["rsi14"], 40, 70)
    s += 10 * clip01(d["rsi7"], 40, 75)
    s += 10 * clip01(d["roc3"], -3, 6)
    s += 10 * clip01(d["roc5"], -4, 8)
    s += 15 * clip01(d["roc10"], -5, 12)
    s += 10 * (d["macd"] > 0) + 10 * (d["macd_hist"] > 0)
    s += 15 * clip01(d["macd_hist_acc"] / d["close"] * 1e4, -2, 4)
    return s.clip(0, 100)


def score_volume(d):
    s = pd.Series(0.0, index=d.index)
    s += 30 * clip01(d["vol_ratio"], 0.7, 1.8)
    s += 15 * clip01(d["qvol_ratio"], 0.7, 1.8)
    s += 15 * clip01(d["vol_acc"], 0.8, 1.4)
    s += 15 * (d["obv"] > d["obv_ema20"])
    s += 10 * (d["obv_slope"] > 0)
    # 阳线放量 / 阴线放量区分
    up = d["close"] > d["open"]
    s += 15 * np.where(up, clip01(d["vol_ratio"], 1.0, 2.0), 0)
    s -= 15 * np.where(~up, clip01(d["vol_ratio"], 1.5, 3.0), 0)
    return s.clip(0, 100)


def score_compression(d):
    """压缩 -> 扩张：过去20日曾处于低波动 (percentile低) 且现在开始扩张"""
    s = pd.Series(0.0, index=d.index)
    comp_prev = pd.concat([d["bb_width_pctile"].shift(1).rolling(20).min(),
                           d["atr_pct_pctile"].shift(1).rolling(20).min(),
                           d["rv20_pctile"].shift(1).rolling(20).min()], axis=1).mean(axis=1)
    # 之前压缩程度 (40)
    s += 40 * clip01(1 - comp_prev, 0.5, 0.9)
    # 当前扩张 (30): bb_width 相对 5 日前变化
    exp_ = d["bb_width"] / d["bb_width"].shift(5) - 1
    s += 30 * clip01(exp_, 0, 0.4)
    # 当日 range 相对 ATR (15)
    s += 15 * clip01(d["range_pct"] / d["atr_pct"], 0.8, 1.8)
    # 目前仍处于压缩状态 (蓄势) 给基础分 (15)
    s += 15 * clip01(1 - d["bb_width_pctile"], 0.6, 0.9)
    return s.clip(0, 100)


def score_structure(d):
    s = pd.Series(0.0, index=d.index)
    s += 40 * clip01(d["struct20"], -1, 1)
    s += 35 * clip01(d["struct50"], -1, 1)
    s += 25 * clip01(d["struct100"], -1, 1)
    return s.clip(0, 100)


def extension_penalty(d):
    """价格相对 EMA20 的 ATR 倍数 + 短期涨幅 + RSI 极端 -> 扣分 0~30"""
    p = pd.Series(0.0, index=d.index)
    p += 15 * clip01(d["ext_atr"], 2.0, 4.0)
    p += 8 * clip01(d["roc5"], 10, 20)
    p += 7 * clip01(d["rsi7"], 82, 95)
    return p.clip(0, 30)


# ----------------------------------------------------------------------
# 5. Regime / Signal / Entry
# ----------------------------------------------------------------------
def classify_regime(row):
    c, e20, e50, e200 = row["close"], row["ema20"], row["ema50"], row["ema200"]
    s200 = row["ema200_slope"]
    dd = row["dd_from_ath"]
    vals = [v for v in (row["struct50"], row["struct100"]) if not np.isnan(v)]
    struct = np.mean(vals) if vals else 0.0
    if c > e20 > e50 > e200 and row["ext_atr"] > 3 and row["rsi14"] > 80:
        return "EUPHORIA"
    if c > e50 > e200 and s200 > 0 and struct >= 0.3:
        return "BULL"
    if c > e50 and (c > e200 or s200 > -0.2) and struct >= 0:
        return "BULL_TRANSITION"
    if c < e50 and c < e200 and s200 <= 0 and struct <= 0:
        return "BEAR"
    if c < e200 and c > e50:
        return "BEAR_RECOVERY"
    return "ACCUMULATION"


def _exhaustion(row):
    """衰竭K线: 阴线, 或上影线 > 实体*1.5 且占区间40%以上, 或放量(1.5x)但收盘位置<40%"""
    rng = max(row["high"] - row["low"], 1e-9)
    body = abs(row["close"] - row["open"])
    upper = row["high"] - max(row["close"], row["open"])
    bearish = row["close"] < row["open"]
    wick = upper > 1.5 * body and upper / rng > 0.4
    stall = row["vol_ratio"] > 1.5 and row["close_pos"] < 0.4
    return bearish or wick or stall


def classify_signal(row, prev_signal, trail_stop, tp_count=0):
    fs = row["FinalScore"]
    reg = row["Regime"]
    brk = row["BreakoutScore"]
    ext = row["ext_atr"]
    c = row["close"]
    in_pos = prev_signal in ("EARLY_BUY", "BREAKOUT_BUY", "STRONG_BUY", "HOLD", "TAKE_PROFIT")
    if in_pos:
        # --- 退出 ---
        if trail_stop is not None and c < trail_stop:
            return "EXIT"                      # 吊灯止损
        if c < row["ema50"] and c < row["lo20"]:
            return "EXIT"
        strong_trend = EXIT_MODE == "regime" and reg in ("BULL", "EUPHORIA") and row["ema20"] > row["ema50"] > row["ema200"]
        if strong_trend:
            # 强趋势: 只在跌破 EMA50 或 (跌破EMA20 且 MACD柱转负且 20D结构转弱) 时退出
            if c < row["ema50"]:
                return "EXIT"
            if c < row["ema20"] and row["macd_hist"] < 0 and row["struct20"] < 0:
                return "EXIT"
        else:
            if c < row["ema20"] and row["macd_hist"] < 0 and row["macd_hist_acc"] < 0:
                return "EXIT"
            if c < row["ema20"] - 1.0 * row["atr14"]:
                return "EXIT"
        # --- 减仓 ---
        overext = reg == "EUPHORIA" or (ext > 3.5 and row["rsi7"] > 88)
        if overext and tp_count < TP_MAX_PER_TRADE and (not TP_NEED_EXHAUSTION or _exhaustion(row)):
            return "TAKE_PROFIT"
        if fs >= TH_STRONG and brk >= 60 and ext < 2.5:
            return "STRONG_BUY"                # 加仓级别
        return "HOLD"
    # --- 空仓 ---
    if reg == "BEAR":
        return "WAIT"
    if fs >= TH_STRONG and brk >= 60 and ext < 2.5:
        return "STRONG_BUY"
    if fs >= TH_BREAKOUT and brk >= 50 and ext < 3.0:
        return "BREAKOUT_BUY"
    base_ok = c > row["ema20"] and row["macd_hist"] > 0
    if fs >= TH_EARLY and base_ok:
        return "EARLY_BUY"
    if (reg in ("BEAR_RECOVERY", "ACCUMULATION") and fs >= TH_EARLY_RECOVERY and base_ok
            and ext < 1.5 and row["macd_hist_acc"] > 0 and (row["brk20"] or c > row["ema50"])):
        return "EARLY_BUY"
    return "WAIT"


def entry_metrics(row):
    ext = row["ext_atr"]
    if ext < 1.0:
        ext_risk = "LOW"
    elif ext < 2.0:
        ext_risk = "MEDIUM"
    elif ext < 3.0:
        ext_risk = "HIGH"
    else:
        ext_risk = "EXTREME"
    # 止损: max(EMA20 - 1ATR, 10日低点), 突破日用前高下方
    stop = max(row["ema20"] - 1.0 * row["atr14"], row["lo20"]) if not np.isnan(row["lo20"]) else row["ema20"] - row["atr14"]
    if row["brk55"] and row["hi55"] * 0.985 > stop:
        stop = row["hi55"] * 0.985
    stop = min(stop, row["close"] * 0.995)
    stop_dist = (row["close"] - stop) / row["close"] * 100
    target = row["close"] + 3.0 * row["atr14"] * (1.5 if row["brk55"] else 1.0)
    rr = (target - row["close"]) / max(row["close"] - stop, 1e-9)
    # entry quality
    q = 100 - 25 * np.clip(ext, 0, 4) - 10 * np.clip(stop_dist - 4, 0, 6)
    q += 10 if row["brk55"] else 0
    q = float(np.clip(q, 0, 100))
    eq = "EXCELLENT" if q >= 80 else "GOOD" if q >= 60 else "FAIR" if q >= 40 else "LOW"
    zone_lo = max(row["ema20"], row["hi20"] if row["brk20"] else row["ema20"])
    zone_hi = row["close"]
    if zone_lo > zone_hi:
        zone_lo = row["close"] - 0.5 * row["atr14"]
    return ext_risk, eq, q, stop, stop_dist, rr, zone_lo, zone_hi


# ----------------------------------------------------------------------
# 6. 主计算
# ----------------------------------------------------------------------
def build(df):
    d = compute_indicators(df)
    d["struct20"] = swing_structure(d, 20)
    d["struct50"] = swing_structure(d, 50)
    d["struct100"] = swing_structure(d, 100)
    d["TrendScore"] = score_trend(d)
    d["BreakoutScore"] = score_breakout(d)
    d["MomentumScore"] = score_momentum(d)
    d["VolumeScore"] = score_volume(d)
    d["CompressionScore"] = score_compression(d)
    d["StructureScore"] = score_structure(d)
    d["RawScore"] = sum(d[f"{k}Score"] * w for k, w in WEIGHTS.items())
    d["ExtensionPenalty"] = extension_penalty(d)
    d["FinalScore"] = (d["RawScore"] - d["ExtensionPenalty"]).clip(0, 100)
    d["Regime"] = d.apply(classify_regime, axis=1)
    sigs, trails = [], []
    prev = "WAIT"
    hi_close, trail, tp_count = None, None, 0
    for _, r in d.iterrows():
        if np.isnan(r["FinalScore"]) or np.isnan(r["ema200"]):
            sigs.append("WAIT"); trails.append(np.nan)
            continue
        s = classify_signal(r, prev, trail, tp_count)
        in_pos = s in ("EARLY_BUY", "BREAKOUT_BUY", "STRONG_BUY", "HOLD", "TAKE_PROFIT")
        if in_pos:
            hi_close = r["close"] if hi_close is None else max(hi_close, r["close"])
            trail = hi_close - CHANDELIER_ATR * r["atr14"]
            if s == "TAKE_PROFIT":
                tp_count += 1
        else:
            hi_close, trail, tp_count = None, None, 0
        sigs.append(s); trails.append(trail if trail is not None else np.nan)
        prev = s
    d["Signal"] = sigs
    d["TrailStop"] = trails
    em = d.apply(lambda r: entry_metrics(r) if not np.isnan(r["atr14"]) else (None,) * 8, axis=1, result_type="expand")
    em.columns = ["ExtensionRisk", "EntryQuality", "EntryQualityScore", "Stop", "StopDist%", "RiskReward", "ZoneLo", "ZoneHi"]
    d = pd.concat([d, em], axis=1)
    # 前瞻收益
    for n in (3, 7, 20):
        d[f"fwd{n}"] = (d["close"].shift(-n) / d["close"] - 1) * 100
    return d


# ----------------------------------------------------------------------
# 7. 回测
# ----------------------------------------------------------------------
def backtest(d, start):
    """简单规则: 出现 *_BUY 且空仓 -> 次日开盘买入全仓; EXIT/TAKE_PROFIT -> 次日开盘卖出.
       TAKE_PROFIT 卖出 50%, EXIT 全部卖出. 手续费 0.1% 单边."""
    fee = 0.001
    sub = d[d["date"] >= pd.Timestamp(start)].reset_index(drop=True)
    cash, pos = 1.0, 0.0
    equity, trades = [], []
    entry_px, entry_date = None, None
    for i in range(len(sub) - 1):
        r = sub.iloc[i]
        nxt_open = sub.iloc[i + 1]["open"]
        sig = r["Signal"]
        if pos == 0 and sig in ("EARLY_BUY", "BREAKOUT_BUY", "STRONG_BUY"):
            pos = cash * (1 - fee) / nxt_open
            cash = 0.0
            entry_px, entry_date, entry_sig = nxt_open, sub.iloc[i + 1]["date"], sig
        elif pos > 0 and sig == "EXIT":
            cash += pos * nxt_open * (1 - fee)
            trades.append((entry_date, sub.iloc[i + 1]["date"], entry_sig, entry_px, nxt_open, nxt_open / entry_px - 1, "EXIT"))
            pos = 0.0
        elif pos > 0 and sig == "TAKE_PROFIT":
            sell = pos * 0.5
            cash += sell * nxt_open * (1 - fee)
            pos -= sell
            trades.append((entry_date, sub.iloc[i + 1]["date"], entry_sig, entry_px, nxt_open, nxt_open / entry_px - 1, "TP50%"))
        equity.append(cash + pos * sub.iloc[i + 1]["close"])
    eq = pd.Series(equity, index=sub["date"].iloc[1:len(equity) + 1])
    if pos > 0:
        trades.append((entry_date, sub.iloc[-1]["date"], entry_sig, entry_px, sub.iloc[-1]["close"], sub.iloc[-1]["close"] / entry_px - 1, "OPEN"))
    bh = sub["close"].iloc[-1] / sub["open"].iloc[1] - 1
    ret = eq.iloc[-1] - 1 if len(eq) else 0
    dd = (eq / eq.cummax() - 1).min() if len(eq) else 0
    bh_dd = (sub["close"] / sub["close"].cummax() - 1).min()
    days = max((eq.index[-1] - eq.index[0]).days, 1) if len(eq) else 1
    daily = eq.pct_change().dropna()
    sharpe = daily.mean() / daily.std() * np.sqrt(365) if len(daily) > 2 and daily.std() > 0 else 0
    closed = [t for t in trades if t[6] != "OPEN"]
    wins = [t for t in closed if t[5] > 0]
    exposure = (eq.index.size and (sub["Signal"].isin(["EARLY_BUY", "BREAKOUT_BUY", "STRONG_BUY", "HOLD", "TAKE_PROFIT"]).mean()))
    stats = {
        "start": str(eq.index[0].date()) if len(eq) else start,
        "end": str(eq.index[-1].date()) if len(eq) else "",
        "strategy_return": ret * 100,
        "buy_hold_return": bh * 100,
        "strategy_maxdd": dd * 100,
        "buy_hold_maxdd": bh_dd * 100,
        "sharpe": sharpe,
        "n_trades": len(closed),
        "win_rate": len(wins) / len(closed) * 100 if closed else float("nan"),
        "avg_trade": np.mean([t[5] for t in closed]) * 100 if closed else float("nan"),
        "avg_win": np.mean([t[5] for t in wins]) * 100 if wins else float("nan"),
        "avg_loss": np.mean([t[5] for t in closed if t[5] <= 0]) * 100 if len(closed) > len(wins) else float("nan"),
        "exposure": exposure * 100,
        "days": days,
    }
    return stats, trades, eq


def signal_stats(d, start):
    """各信号发出后 +3/+7/+20 日 平均收益 & 胜率"""
    sub = d[d["date"] >= pd.Timestamp(start)]
    rows = []
    for sig in ["EARLY_BUY", "BREAKOUT_BUY", "STRONG_BUY", "HOLD", "TAKE_PROFIT", "EXIT", "WAIT"]:
        g = sub[sub["Signal"] == sig]
        if len(g) == 0:
            continue
        row = {"Signal": sig, "N": len(g)}
        for n in (3, 7, 20):
            v = g[f"fwd{n}"].dropna()
            row[f"+{n}D avg%"] = v.mean() if len(v) else np.nan
            row[f"+{n}D win%"] = (v > 0).mean() * 100 if len(v) else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------
# 8. 报告
# ----------------------------------------------------------------------
def fmt_money(x):
    return f"${x:,.0f}"


def daily_report(d):
    r = d.iloc[-1]
    prev = d.iloc[-2]
    live_note = ""
    now = datetime.now(timezone.utc)
    if r["date"].date() == now.date():
        live_note = "  (今日K线未收盘, 数据为实时值)"
    tick = lambda b: "✓" if b else "✗"
    reasons, warns = [], []
    if r["brk55"]: reasons.append("Major resistance breakout (55D high)")
    elif r["brk20"]: reasons.append("20D high breakout")
    if r["brk90"]: reasons.append("90D high breakout")
    if r["brk120"]: reasons.append("120D high breakout")
    if r["vol_ratio"] >= 1.5: reasons.append(f"Volume confirmation ({r['vol_ratio']:.2f}x)")
    if r["close"] > r["ema20"] > r["ema50"] > r["ema200"]: reasons.append("Bullish MA structure (C>EMA20>EMA50>EMA200)")
    elif r["close"] > r["ema50"] > r["ema200"]: reasons.append("Price above EMA50 & EMA200")
    if r["CompressionScore"] >= 60: reasons.append("Volatility compression -> expansion")
    if r["struct20"] > 0.3 and r["struct50"] > 0: reasons.append("Higher-high / higher-low structure")
    if r["macd_hist"] > 0 and r["macd_hist_acc"] > 0: reasons.append("MACD histogram accelerating")
    if r["ext_atr"] > 2: warns.append(f"Short-term extension elevated ({r['ext_atr']:.1f} ATR above EMA20)")
    if r["rsi14"] > 70: warns.append(f"RSI14 elevated ({r['rsi14']:.0f})")
    if r["rsi7"] > 80: warns.append(f"RSI7 overbought ({r['rsi7']:.0f})")
    if r["vol_ratio"] < 0.8: warns.append(f"Volume weak ({r['vol_ratio']:.2f}x)")
    if r["close"] < r["ema200"]: warns.append("Price below EMA200")
    if r["ema200_slope"] < 0: warns.append("EMA200 still declining")
    if r["Signal"] == "HOLD" and (r["Regime"] == "EUPHORIA" or (r["ext_atr"] > 3.5 and r["rsi7"] > 88)):
        warns.append("Over-extended (EUPHORIA) — model keeps HOLD (TP disabled by backtest); consider manual partial take-profit")
    if r["roc5"] > 12: warns.append(f"5D gain {r['roc5']:.1f}% — chasing risk")
    if live_note: warns.append("Today's candle not closed yet — re-run after 08:00 Asia/Shanghai (00:00 UTC)")

    L = []
    ap = L.append
    ap("=" * 56)
    ap("BTC DAILY QUANT V1.2")
    ap("=" * 56)
    ap(f"Date:               {r['date'].date()}{live_note}")
    ap(f"Price:              {fmt_money(r['close'])}   ({r['close'] / prev['close'] - 1:+.2%} vs prev close)")
    ap("")
    ap(f"REGIME:             {r['Regime']}")
    ap(f"FinalScore:         {r['FinalScore']:.1f} / 100   (Raw {r['RawScore']:.1f} - ExtPenalty {r['ExtensionPenalty']:.1f})")
    ap(f"Signal:             {r['Signal']}")
    ap("-" * 56)
    for k in WEIGHTS:
        ap(f"{k + 'Score':<20}{r[k + 'Score']:>6.0f}   (w={WEIGHTS[k]:.0%})")
    ap("-" * 56)
    ap(f"{'RSI14':<20}{r['rsi14']:>8.1f}")
    ap(f"{'RSI7':<20}{r['rsi7']:>8.1f}")
    ap(f"{'MACD':<20}{'+' if r['macd'] > 0 else '-':>8}   hist {r['macd_hist']:+.0f} ({'acc' if r['macd_hist_acc'] > 0 else 'dec'})")
    ap(f"{'ATR14':<20}{r['atr14']:>8.0f}   ({r['atr_pct']:.2f}%)")
    ap(f"{'BOLL %B':<20}{r['bb_pctb']:>8.2f}   width {r['bb_width']:.1f}% (pctile {r['bb_width_pctile']:.0%})")
    ap(f"{'Volume Ratio':<20}{r['vol_ratio']:>8.2f}x  quote {r['qvol_ratio']:.2f}x")
    ap(f"{'ROC 3/5/10':<20}{r['roc3']:>+7.1f}% {r['roc5']:+.1f}% {r['roc10']:+.1f}%")
    ap(f"{'EMA20/50/200':<20}{fmt_money(r['ema20'])} / {fmt_money(r['ema50'])} / {fmt_money(r['ema200'])}")
    ap(f"{'Ext (ATR from EMA20)':<20}{r['ext_atr']:>8.2f}")
    ap(f"{'Structure 20/50/100':<20}{r['struct20']:>+5.2f} {r['struct50']:+.2f} {r['struct100']:+.2f}")
    ap("-" * 56)
    ap("Breakout:")
    for n in (20, 30, 55, 90, 120):
        ap(f"  {str(n) + 'D HIGH':<20}{tick(r[f'brk{n}'])}   (level {fmt_money(r[f'hi{n}'])})")
    ap(f"  {'Volume Confirmation':<20}{tick(r['vol_ratio'] >= 1.5)}")
    ap(f"  {'Volatility Expansion':<20}{tick(r['bb_width'] > d['bb_width'].iloc[-6])}")
    ap(f"  {'Close Position':<20}{r['close_pos']:.0%} of day range")
    ap("-" * 56)
    ap(f"Extension Risk:     {r['ExtensionRisk']}")
    ap(f"Entry Quality:      {r['EntryQuality']} ({r['EntryQualityScore']:.0f})")
    ap(f"Suggested Zone:     {fmt_money(r['ZoneLo'])} – {fmt_money(r['ZoneHi'])}")
    ap(f"Invalidation:       {fmt_money(r['Stop'])}   (stop dist {r['StopDist%']:.1f}%)")
    ap(f"Risk/Reward:        {r['RiskReward']:.2f}")
    if not np.isnan(r["TrailStop"]):
        ap(f"Trailing Stop:      {fmt_money(r['TrailStop'])}   (highest close - {CHANDELIER_ATR:.0f} ATR)")
    ap("-" * 56)
    ap("Reasons:")
    for x in reasons or ["(none)"]:
        ap(f"  ✓ {x}")
    ap("Warnings:")
    for x in warns or ["(none)"]:
        ap(f"  ! {x}")
    ap("=" * 56)
    ap("Last 10 days:")
    ap(f"{'Date':<12}{'Regime':<17}{'Score':>6}  {'Signal':<14}{'Price':>10}{'Ext':>6}{'Vol':>6}")
    for _, x in d.tail(10).iterrows():
        ap(f"{str(x['date'].date()):<12}{x['Regime']:<17}{x['FinalScore']:>6.1f}  {x['Signal']:<14}{fmt_money(x['close']):>10}{x['ext_atr']:>6.1f}{x['vol_ratio']:>6.2f}")
    ap("=" * 56)
    return "\n".join(L)


def table(d, start=None, end=None, title=""):
    sub = d
    if start: sub = sub[sub["date"] >= pd.Timestamp(start)]
    if end: sub = sub[sub["date"] <= pd.Timestamp(end)]
    L = [title, f"{'Date':<12}{'Regime':<17}{'Score':>6}  {'Signal':<14}{'Price':>10}{'+3D':>8}{'+7D':>8}{'+20D':>8}{'EQ':<11}{'Ext':>5}"]
    for _, x in sub.iterrows():
        f = lambda v: f"{v:+.1f}%" if not np.isnan(v) else "  n/a"
        L.append(f"{str(x['date'].date()):<12}{x['Regime']:<17}{x['FinalScore']:>6.1f}  {x['Signal']:<14}{fmt_money(x['close']):>10}{f(x['fwd3']):>8}{f(x['fwd7']):>8}{f(x['fwd20']):>8} {str(x['EntryQuality']):<10}{x['ext_atr']:>5.1f}")
    return "\n".join(L)


def backtest_report(d, start):
    stats, trades, eq = backtest(d, start)
    ss = signal_stats(d, start)
    L = ["=" * 72, f"BACKTEST  {stats['start']} -> {stats['end']}  ({stats['days']} days)", "=" * 72,
         f"Strategy return:     {stats['strategy_return']:+.1f}%      Buy&Hold: {stats['buy_hold_return']:+.1f}%",
         f"Strategy max DD:     {stats['strategy_maxdd']:.1f}%      Buy&Hold: {stats['buy_hold_maxdd']:.1f}%",
         f"Sharpe (daily):      {stats['sharpe']:.2f}",
         f"Trades (closed):     {stats['n_trades']}   Win rate: {stats['win_rate']:.0f}%   Avg: {stats['avg_trade']:+.1f}%  (win {stats['avg_win']:+.1f}% / loss {stats['avg_loss']:+.1f}%)",
         f"Time in market:      {stats['exposure']:.0f}%",
         "规则: *_BUY 次日开盘全仓买入; TAKE_PROFIT 首日减半; EXIT 次日开盘清仓; 手续费0.1%",
         "-" * 72, "Trades:",
         f"{'Entry':<12}{'Exit':<12}{'Sig':<14}{'EntryPx':>10}{'ExitPx':>10}{'PnL':>8}  Type"]
    for t in trades:
        L.append(f"{str(t[0].date()):<12}{str(t[1].date()):<12}{t[2]:<14}{fmt_money(t[3]):>10}{fmt_money(t[4]):>10}{t[5]:>+7.1%}  {t[6]}")
    L += ["-" * 72, "Forward returns by signal (all days, not only entries):"]
    L.append(ss.to_string(index=False, float_format=lambda v: f"{v:.1f}"))
    # 每个信号第一次出现 (新开仓日) 的前瞻
    sub = d[d["date"] >= pd.Timestamp(start)]
    ent = sub[(sub["Signal"].isin(["EARLY_BUY", "BREAKOUT_BUY", "STRONG_BUY"])) & (~sub["Signal"].shift(1).isin(["EARLY_BUY", "BREAKOUT_BUY", "STRONG_BUY", "HOLD", "TAKE_PROFIT"]))]
    L += ["-" * 72, f"Fresh entry signals (from flat): {len(ent)}"]
    if len(ent):
        L.append(table(ent, title=""))
    # 高分日
    top = sub.sort_values("FinalScore", ascending=False).head(15).sort_values("date")
    L += ["-" * 72, "Top-15 FinalScore days:"]
    L.append(table(top, title=""))
    return "\n".join(L), eq


def plot_equity(d, eq, start):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    sub = d[d["date"] >= pd.Timestamp(start)].set_index("date")
    bh = sub["close"] / sub["close"].iloc[0]
    fig, ax = plt.subplots(3, 1, figsize=(14, 11), sharex=True, gridspec_kw={"height_ratios": [3, 1.2, 1]})
    ax[0].plot(sub.index, sub["close"], color="black", lw=1, label="BTC")
    ax[0].plot(sub.index, sub["ema20"], lw=0.8, label="EMA20")
    ax[0].plot(sub.index, sub["ema50"], lw=0.8, label="EMA50")
    ax[0].plot(sub.index, sub["ema200"], lw=0.8, label="EMA200")
    mk = {"EARLY_BUY": ("^", "tab:green"), "BREAKOUT_BUY": ("^", "lime"), "STRONG_BUY": ("*", "gold"),
          "TAKE_PROFIT": ("v", "orange"), "EXIT": ("v", "red")}
    prev = sub["Signal"].shift(1)
    for sig, (m, col) in mk.items():
        pts = sub[(sub["Signal"] == sig) & (prev != sig)]
        ax[0].scatter(pts.index, pts["close"], marker=m, c=col, s=90, zorder=5, label=sig, edgecolors="k", linewidths=0.5)
    ax[0].legend(loc="upper left", ncol=4, fontsize=8)
    ax[0].set_title(f"BTC Quant V1.2  backtest {start} -> {sub.index[-1].date()}")
    ax[1].plot(eq.index, eq, label="Strategy", color="tab:blue")
    ax[1].plot(bh.index, bh, label="Buy & Hold", color="gray", alpha=0.7)
    ax[1].axhline(1, color="k", lw=0.5); ax[1].legend(fontsize=8); ax[1].set_ylabel("Equity")
    ax[2].plot(sub.index, sub["FinalScore"], color="purple", lw=0.9)
    for th, col in ((TH_STRONG, "gold"), (TH_BREAKOUT, "lime"), (TH_EARLY, "green")):
        ax[2].axhline(th, color=col, lw=0.6, ls="--")
    ax[2].set_ylabel("FinalScore"); ax[2].set_ylim(0, 100)
    plt.tight_layout()
    plt.savefig(os.path.join(OUT_DIR, "backtest_chart.png"), dpi=110)


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01", help="回测起始日期")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--focus", nargs=2, metavar=("FROM", "TO"), help="重点输出某段日期的每日信号")
    args = ap.parse_args()

    print("[1/5] 获取 Binance BTCUSDT 日线 ...")
    df = load_klines(offline=args.offline)
    print(f"      {len(df)} 根K线  {df['date'].iloc[0].date()} -> {df['date'].iloc[-1].date()}")

    print("[2/5] 计算指标 / 因子 / Regime / Signal ...")
    d = build(df)

    print("[3/5] 生成今日报告 ...")
    rep = daily_report(d)
    with open(os.path.join(OUT_DIR, "BTC_Quant_Report.txt"), "w", encoding="utf-8") as f:
        f.write(rep)
    print(rep)

    print("[4/5] 回测 ...")
    bt, eq = backtest_report(d, args.start)
    focus_txt = ""
    if args.focus:
        focus_txt = "\n" + table(d, args.focus[0], args.focus[1], title=f"FOCUS {args.focus[0]} -> {args.focus[1]}")
    else:
        focus_txt = "\n" + table(d.tail(15), title="LAST 15 DAYS")
    bt += "\n" + focus_txt
    with open(os.path.join(OUT_DIR, "backtest_report.txt"), "w", encoding="utf-8") as f:
        f.write(bt)
    print(bt)

    print("[5/5] 保存 CSV ...")
    cols = ["date", "open", "high", "low", "close", "volume", "quote_volume", "Regime", "Signal", "FinalScore", "RawScore", "ExtensionPenalty",
            "TrendScore", "BreakoutScore", "MomentumScore", "VolumeScore", "CompressionScore", "StructureScore",
            "ExtensionRisk", "EntryQuality", "EntryQualityScore", "Stop", "TrailStop", "StopDist%", "RiskReward", "ZoneLo", "ZoneHi",
            "ema20", "ema50", "ema100", "ema200", "rsi14", "rsi7", "roc3", "roc5", "roc10", "macd", "macd_hist", "atr14", "atr_pct",
            "bb_pctb", "bb_width", "vol_ratio", "qvol_ratio", "ext_atr", "hi20", "hi55", "hi90", "hi120",
            "brk20", "brk30", "brk55", "brk90", "brk120", "struct20", "struct50", "struct100", "fwd3", "fwd7", "fwd20"]
    d[cols].round(4).to_csv(os.path.join(OUT_DIR, "btc_daily_signals.csv"), index=False)
    eq.rename("equity").to_csv(os.path.join(OUT_DIR, "backtest_equity.csv"))
    try:
        plot_equity(d, eq, args.start)
        print("      已生成 output/backtest_chart.png")
    except Exception as e:  # noqa
        print(f"      (跳过绘图: {e})")
    print(f"完成. 输出目录: {OUT_DIR}")


if __name__ == "__main__":
    main()
