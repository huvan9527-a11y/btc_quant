# 部署：GitHub Actions + 微信推送（约 10 分钟，零成本）

## 第 1 步：拿微信推送的 SendKey（2 分钟）

用 **Server酱**——不需要注册公众号，微信扫码就行，免费额度每天 5 条（我们每天最多 1 条）。

1. 浏览器打开 **https://sct.ftqq.com**
2. 点右上角 **登录** → 用 **微信扫码**
3. 登录后点顶部 **SendKey** → 复制那串 `SCT` 开头的 key（形如 `SCT123456Tabcdefg…`）
4. 点顶部 **消息通道** → 确认 **「方糖服务号」** 已勾选（扫码时通常已自动关注；没有的话按页面提示用微信扫码关注「方糖」服务号）——**消息就是从这个服务号推给你的**
5. 本地验证一下（在 btc_quant 目录）：
   ```bash
   SCT_SENDKEY=你的key python3 notify.py --test
   ```
   微信里「方糖」服务号应立刻收到「BTC 仓位引擎 测试」。收到即成功。

> 备选：如果 Server酱 打不开，用 **PushPlus**（https://www.pushplus.plus，同样微信扫码 → 复制 token），
> 对应的变量名是 `PUSHPLUS_TOKEN`，其余步骤一样。

## 第 2 步：建私有仓库并推送代码

1. GitHub 新建仓库：**Private**，不要勾选 README / .gitignore / license
2. 本地终端：
   ```bash
   cd btc_quant
   git init -b main
   git add -A
   git commit -m "BTC position engine"
   git remote add origin https://github.com/<你的用户名>/<仓库名>.git
   git push -u origin main
   ```
   `.env` 已被 `.gitignore` 排除，key 不会上传。

## 第 3 步：把 SendKey 存进 GitHub Secrets

仓库页 → **Settings → Secrets and variables → Actions → New repository secret**
- Name: `SCT_SENDKEY`
- Secret: 第 1 步复制的 key

（只需要这一个。Telegram / PushPlus 的 secret 不配就自动跳过。）

## 第 4 步：允许 Actions 写回仓库

**Settings → Actions → General → Workflow permissions** → 选 **Read and write permissions** → Save
（否则账本 `paper_ledger.csv` 没法自动提交回来。）

## 第 5 步：手动跑一次验证

**Actions** 标签 → 左侧 **BTC Position Daily** → 右侧 **Run workflow** → 等 1–2 分钟：
- 绿色 ✓，仓库里出现新 commit `daily 2026-xx-xx` → 运行成功
- 微信「方糖」收到消息（首次因为需要建仓一定会推）→ 推送成功

之后每天 **北京 08:10 左右**自动运行（GitHub 定时可能延迟 5–30 分钟，正常）。

## 你会收到什么

**默认只在两种情况推送**，其他日子安静：
- 需要买入 / 卖出 → 标题「BTC 需要操作: 买入」
- 监控状态 YELLOW / RED → 标题「BTC 监控 YELLOW 先读 PLAYBOOK」

想每天都收到一条摘要（哪怕是"不动"）：把 `run_daily.sh` 里的 `--daily` 改成 `--daily --always`，push 即生效。

消息正文示例：
```
BTC 仓位引擎
Date: 2026-09-21  Price: $85,974  距历史高点: -31.0%  20日波动: 47%

★ 方案 B_回撤加仓版
  目标仓位 100%
  操作: 买入, 把仓位从 0% 提到 100%, 明早开盘执行
  失效线: 收盘跌破 $68,321 → 方案转 BEAR, 目标降至 30%

监控状态: GREEN
你记录的实际仓位: 0%
```

## 日常维护（只有两个动作）

- **选定方案**：仓库里 `state.json` 的 `"profile"` 改成 `"A"` 或 `"B"`（改完消息就只显示你的方案）
- **你真的买卖之后**：`state.json` 的 `"holding"` 改成实际仓位（如 `1.0`），这样下次建议才准
  - 直接在 GitHub 网页上点文件 → 铅笔图标编辑 → Commit 即可，不用本地操作

查看账本：仓库里 `paper_ledger.csv`；查看完整报告：`output/BTC_Position_Report.txt`

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| Actions 日志里有 `restricted location` | GitHub 服务器在美国，`api.binance.com` 拒绝；程序自动切到 `data-api.binance.vision`，**不影响** |
| push 被拒 403 | 第 4 步没选 Read and write |
| 微信没收到 | 看 Actions 日志里 `notify.py` 那行：「今日无需推送」是正常的；「FAILED」检查 SendKey 是否复制完整、「方糖」服务号是否关注 |
| Server酱 提示额度用完 | 免费 5 条/天，正常用不完；若开了 `--always` 且手动 Run 多次会触发，次日恢复 |
| 想换手机 / 微信号 | 重新扫码登录 sct.ftqq.com，SendKey 不变，消息跟着微信号走 |
