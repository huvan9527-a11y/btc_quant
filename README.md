# BTC Quant 工具箱

本目录有两个独立程序，目标不同：

| 程序 | 定位 | 每天回答 |
|---|---|---|
| **`btc_position.py`（V2，推荐）** | 长期持有者的仓位引擎 | 今天该持有多少 BTC？ |
| `btc_quant.py`（V1.2） | 波段趋势多因子打分 | 今天是不是买点？ |

结论（2020→今 6 年）：任何"猜买点"的方法都跑不赢死拿；能赢死拿的只有**仓位管理**。所以日常请用 V2，V1 保留作研究。

---

**先读 [PLAYBOOK.md](PLAYBOOK.md)（一页纸操作手册）。**

# V2  btc_position.py  —  仓位引擎（无杠杆）

```bash
python btc_position.py            # 每日运行 → output/BTC_Position_Report.txt + position_chart.png + position_daily.csv
python btc_position.py --train    # 重新网格搜索(仅 2020-2022)，结果写 output/v2_train_grid_top.csv，不自动覆盖方案
```

```bash
python ledger.py set-profile B        # 选定执行方案 (A/B), 只选一次
python ledger.py set-holding 0        # 记录你的实际仓位 (空手=0), 之后报告按实际仓位给建议
python ledger.py show                 # 查看 paper trading 账本
./run_daily.sh                        # 每日定时运行, 需要操作时才输出
python robustness.py                  # 稳健性检验 (walk-forward / 热力图 / bootstrap / 压力测试)
python monitor.py                     # 偏离监控: 现在的亏损是正常回撤还是系统失效 (GREEN/YELLOW/RED)
```

**执行规则**（已写入报告底部）：日线收盘后运行 → 次日开盘执行 → 偏离目标 <10% 不调仓。晚 1 天执行无影响，晚 3 天以上明显受损。
真实成本 (0.1% 手续费 + 0.05% 滑点 + 次日开盘成交) 下 2020→今：A +1145% / B +1378%，比理想回测低约 10%。每年交易 3–13 次。

**Paper trading** 自 2026-09-20 起自动累积 (`paper_ledger.csv`)，这是唯一真正的样本外记录。

三层规则，每一层都是公式：
1. **牛熊层**：收盘站上 150 日线 +缓冲 → BULL 持仓；跌破 150 日线 −缓冲 → BEAR 清仓
2. **波动率层**：BULL 中仓位 = 目标波动 100% ÷ 当前 20 日年化波动，上限 100%（波动 >100% 时自动减仓）
3. **回撤加仓层**（仅方案 B）：距历史高点每跌 10% 加 10%，上限 40%

两套方案同时输出，**只选一套执行，不要来回切**：

| | A 基础版 | B 回撤加仓版 | 死拿 |
|---|---|---|---|
| 训练期 2020-22 | +339% / DD -42% | +311% / DD -49% | +130% / -77% |
| **样本外 2023-今** | +210% / DD -39% | **+295% / DD -31%** | +421% / -53% |
| 2020-今 全程 | +1263% / -42% | +1523% / -49% | +1097% / -77% |
| 2022 熊市 | **0%（全年空仓）** | -26%（保留 20-40% 仓位越跌越买） | -64% |
| 2025 震荡 | -21% | -5% | -6% |

**稳健性检验结论**（`output/robustness_report.txt`）：
- Walk-forward 每年重选参数始终落在 ma 100–150 / buf 0–5%，逐年复合 +906%（死拿 +1097%）→ 收益与死拿相当、回撤减半才是诚实预期
- 384 组参数中 51% 同时收益>死拿且回撤<死拿，100% 回撤更小 → 高原而非尖峰
- Bootstrap 2000 次：A 跑赢死拿 61%、B 72%；回撤更小 97%
- 弱点：牛市年份必然跑输死拿；震荡年（2025）是最差场景

选择指南：
- **A**：熊市里完全离场，2022 那种年份零回撤。代价是牛市起点晚一点、震荡市被洗得更疼。适合"下跌时一股都不想拿"的人。
- **B**：熊市里保留一点底仓并越跌越买，回撤更大但底部筹码更多，样本外收益更高、震荡市更稳。适合"相信长期、跌了想加但怕加太早"的人。
- 参数是 810 组网格里按"训练期收益 ≥ 死拿、然后回撤最小"选出，且 2023 之后未用于调参。`ma=150`、`tgt_vol=1.0` 在所有组合中跨期一致。
- 报告里的"心理护栏"是历史上相似回撤位置之后的实际收益分布，用于在想割肉/想追高时看一眼。

---

# V1.2  btc_quant.py  —  波段趋势打分（研究用）

BTC/USDT 日线 · 多因子评分 · 大周期 Regime · 突破交易 · 内置回测
数据源：Binance 公开 Kline 接口（无需账号 / API Key），自动多端点回退（`data-api.binance.vision` 在受限地区也可用）。

## 运行
```bash
pip install -r requirements.txt
python btc_quant.py                                   # 更新数据 + 今日报告 + 回测(2025-01-01起)
python btc_quant.py --start 2024-01-01                # 更长回测
python btc_quant.py --focus 2025-08-10 2025-08-25     # 重点查看某段日期每天的信号
python btc_quant.py --offline                         # 只用本地缓存
```
> 建议每天 **北京时间 08:00 之后**运行（UTC 00:00 日线收盘）。收盘前运行会提示“今日K线未收盘”。

## 输出（output/）
| 文件 | 内容 |
|---|---|
| `BTC_Quant_Report.txt` | 今日报告：Regime / FinalScore / Signal / 六大因子 / 突破清单 / 入场质量 / 止损 / 理由与警告 |
| `backtest_report.txt` | 回测统计、每笔交易、各信号前瞻收益、新开仓信号清单、Top-15 高分日、Focus 区间 |
| `btc_daily_signals.csv` | 每天全部因子、分数、信号、+3/+7/+20D 前瞻收益 |
| `backtest_equity.csv` | 策略净值曲线 |
| `backtest_chart.png` | 价格+信号标记 / 净值 vs 买入持有 / FinalScore 三联图 |
| `data/BTCUSDT_1d.csv` | 本地 K 线缓存（增量更新） |

## 模型结构
```
Regime   : BEAR / BEAR_RECOVERY / ACCUMULATION / BULL_TRANSITION / BULL / EUPHORIA
Signal   : WAIT / EARLY_BUY / BREAKOUT_BUY / STRONG_BUY / HOLD / TAKE_PROFIT / EXIT
Entry    : ExtensionRisk / EntryQuality / Stop(Invalidation) / StopDist / RiskReward / SuggestedZone

FinalScore = 0.25*Trend + 0.25*Breakout + 0.15*Momentum + 0.15*Volume
           + 0.10*Compression + 0.10*Structure  -  ExtensionPenalty(0~30)
```
- **Trend**：EMA20/50/100/200 排列、5日斜率、价格相对位置
- **Breakout**：20/30/55/90/120D 新高、突破幅度、收盘位置、突破前箱体长度、前高测试次数、近5日延续
- **Momentum**：RSI14/7、ROC3/5/10、MACD、柱状加速（RSI>70 不触发卖出，仅用于动能与超延伸）
- **Volume**：Volume/Quote Volume 20日比、5/20 加速、OBV、阳线放量加分 / 阴线放量扣分
- **Compression**：BOLL 带宽 / ATR% / 20D 实现波动 的 120 日百分位 → 压缩后扩张
- **Structure**：20/50/100 日 swing HH/HL/LH/LL，突破区间所有前高视为结构反转
- **ExtensionPenalty**：价格高于 EMA20 超过 2~4 ATR、5日涨幅 >10%、RSI7 >82

信号阈值（可在 `classify_signal` 中调整）：
- STRONG_BUY：Final ≥ 78 且 Breakout ≥ 60 且 Ext < 2.5 ATR
- BREAKOUT_BUY：Final ≥ 70 且 Breakout ≥ 50 且 Ext < 3.0
- EARLY_BUY：Final ≥ 62、非 BEAR、Close > EMA20、MACD 柱 > 0
- EXIT：跌破 EMA50 且 20 日低点 / 跌破 EMA20 且 MACD 柱转负并走弱 / 跌破 EMA20 − 1ATR
- 吊灯止损：收盘 < 持仓期最高收盘 − 4 ATR（安全网）
- `EXIT_MODE="regime"`：BULL/EUPHORIA 且均线多头时，退出放宽为跌破 EMA50（或跌破 EMA20 + MACD 柱转负 + 20D 结构转弱）；其他 Regime 保持 EMA20 紧退出
- `TP_MAX_PER_TRADE=0`：默认**不减仓**。网格测试 2023–2026：不减仓 +143% / 减 1 次 +89% / 不限次数 +67%，减仓只会砍掉主升浪。过热时报告给出 Warning 由人工决定
- TAKE_PROFIT：EUPHORIA 或 Ext > 3.5 ATR 且 RSI7 > 88

回测规则：*_BUY 次日开盘全仓买入，TAKE_PROFIT 首日减仓 50%，EXIT 次日开盘清仓，单边手续费 0.1%。

## 免责声明
仅供研究学习，不构成投资建议。所有参数均未做优化，回测存在过拟合与样本外失效风险。

## 版本记录
| 版本 | 变化 | 2023-01→今 | 2025-01→今 |
|---|---|---|---|
| V1.0 | 初版 | +72% / DD -23% / Sharpe 0.86 | -0.1% / DD -23% |
| V1.1 | 参数化、吊灯止损、图表（信号不变） | 同上 | 同上 |
| V1.2 | Regime 自适应退出、取消自动减仓 | **+143% / DD -23% / Sharpe 1.04** | +1.5% / DD -23% |
| 参考 | 买入持有 | +418% / DD -53% | -9% / DD -53% |
