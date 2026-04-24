---
name: Quant System Evolution Log
description: 记录量化交易系统的每日代码变更、逻辑重构与架构决策，用于后期查阅与故障排查。
---

# 🚀 量化系统演进日志 (System Evolution Log)

> [!IMPORTANT]
> 本文档由 `knowledge_manager.py` 每日 18:00 自动更新。记录了当日核心代码的变更逻辑与架构决策。

## 📅 2026-04-24 — autopilot_master.py 物理单例锁（Socket 端口 38888）+ oracle_v2_universe.json 紧急还原 + fat_fish_master.py 量纲统一 + underdog_executor.py DEBUG日志级别修复

### 变更文件：`underdog_executor.py`（日志级别 Debug→Info，恢复可观测性）

**[BUG] 根因：无持仓时所有关键路径均走 debug 分支，INFO 日志文件完全空白**

**故障现象（2026-04-24）**：
- 09:26:55 Restart1 启动，QMT 连接成功
- 09:30 后 Patrol 时段开始，但 `underdog_executor.log` 无任何 Patrol 循环输出
- 用户误以为进程崩溃，实际进程在正常运行

**根因链路**：
1. `patrol_positions()` 账本为空 → `logger.debug("账本为空，跳过巡逻")` — INFO 文件不可见
2. `patrol_positions()` 无活跃持仓 → `logger.debug("无活跃持仓，跳过")` — INFO 文件不可见
3. 非 Patrol/Scanner 时段 → `logger.debug("[Main] ... 静默等待")` — INFO 文件不可见
4. 今日账本为空（无持仓），整个 Patrol 循环全程走 debug 路径

**次要原因（双实例崩溃）**：
- 09:22 时 AutoPilot 两次调度 `_launch_process("落水狗抄底执行器")` 导致双实例竞争 `underdog_slots.json`，exitcode=4294967295 (-1) 崩溃

**[FIX] 三处 debug→info 升级**：

| 位置 | 旧 | 新 |
|---|---|---|
| `patrol_positions()` L585 | `logger.debug("账本为空，跳过巡逻")` | `logger.info("账本为空，空仓静候")` |
| `patrol_positions()` L594 | `logger.debug("无活跃持仓，跳过")` | `logger.info("无活跃持仓，空仓静候")` |
| `main()` L978 | `logger.debug("[Main] ... 静默等待")` | `logger.info("[Main] ... 静默心跳")` |

**效果**：无论是否有持仓，INFO 日志每 30s 至少产生一行心跳，可供监控。

**验证**：`py_compile underdog_executor.py` → **Exit 0**

### 变更文件：`macro_rotation_executor.py`（Fill-Based 原子记账重构）

**[ARCH] 复刻 T0 on_order_trade 模式到 MacroRotation**

| 组件 | 变更 |
|------|------|
| `MacroCallback` | 新增，继承 `XtQuantTraderCallback`，实现 `on_order_trade` / `on_order_error` / `on_order_stock` |
| `_macro_pending` | 线程安全注册表 `{seq_id → {slot, direction, target_qty, filled_so_far}}` |
| `sync_sell` | 新增 `slot` 参数；下单后注册 pending，废弃 `time.sleep(2)` |
| `sync_buy` | 新增 `slot` 参数；下单后注册 pending，废弃 `time.sleep(1)` |
| 执行流 | 预写 code/capital → 下单注册 pending → `time.sleep(8)` 等回调 |
| `on_order_trade` | 原子写 shares/cost（加权均价）/hwm；支持分笔成交累加；完全成交才 pop pending |

**账本字段标准**：`slot_x` / `slot_x_capital` / `slot_x_shares` / `slot_x_cost` / `slot_x_hwm`

**验证**：`py_compile macro_rotation_executor.py` → **Exit 0**

**[BUG-PATCH] 两个生产安全补丁（缝隙修复）**

| 补丁 | 缝隙 | 修复方式 |
|------|------|---------|
| IO 锁（竞态） | 多笔碎单同时触发 `on_order_trade`，并发写 JSON 导致股数丢失 | `_macro_slots_lock = threading.Lock()` 包裹 `_load_slots + _save_slots`，同一时刻只有一个回调能写账本 |
| 无情清场（幽灵挂单） | `sleep(8)` 后订单仍挂在交易所，主程序退出后 14:55 突然成交 | `_cancel_pending_orders(trader, acc)`：查询所有 pending seq，状态 50/51/52（未报/待成交/部分成交）立即发 `cancel_order_stock`，N8N 推送告警，清场完毕才继续 |

清场逻辑位置：**卖出 sleep(5)** 后 × 2 + **买入 sleep(8)** 后 × 4，共 6 处全覆盖。


### 变更文件：`quant-v4-patterns/SKILL.md`（§14 Fill-Based 原子记账铁律入库）

**[PATTERN] 新增 §14 Fill-Based 原子记账铁律（7条）**

基于今日 `macro_rotation_executor.py` 实战重构提炼，永久沉淀为全局规范：

| 铁律编号 | 规范名称 | 禁止行为 |
|---------|---------|---------|
| §14.1 | Pending 注册表 | 禁止 `time.sleep` 推断成交 |
| §14.2 | IO 锁 | 禁止并发回调裸写账本（竞态） |
| §14.3 | 无情清场 | 禁止留幽灵挂单过夜（QMT状态码50/51/52须撤单） |
| §14.4 | 加权均价 | 禁止 Topup 时覆盖历史成本 |
| §14.5 | 预写回写分离 | 主流程写 slot/capital，回调写 shares/cost/hwm |
| §14.6 | 精准卖出 | 禁止全仓物理清盘误杀手动仓，优先用账本 known_qty |
| §14.7 | 账本字段规范 | slot_x / slot_x_capital / slot_x_shares / slot_x_cost / slot_x_hwm |

额外记录：**账本 JSON 禁止使用 `//` 注释**（今日 `macro_slots.json` 手工编辑引入，已在铁律中标红）。

### 变更文件：`.state/etf_grid_positions.json` + `.state/macro_slots.json`（收盘账本修正）

**[OPS] ETF OU Grid 账本手工重置**

- **背景**：`520500.SH` 4格（27800股）被 `intraday_reconcile.py` 误清仓（防火墙漏洞，已修复）。实盘已平，账本随之清空。
- **修改**：`etf_grid_positions.json` 中 `520500.SH` 格位列表清空 `[]`，其余空标的框架保留，周一重启时从实盘对账重建。

**[OPS] MacroRotation 账本收盘修正**

今日 14:42 轮动执行后 `macro_slots.json` 的两处偏差，收盘后手工修正：

| 字段 | 修正前 | 修正后 | 说明 |
|------|--------|--------|------|
| `slot_a_capital` | 50000 | **75000** | UPTREND 满配，代码 alloc_a=75000 但旧账本未更新 |
| `slot_a_hwm` | 缺失 | **135.215** | 今日补买 511260 参考价（bid1=135.217） |
| `slot_b_capital` | 75000 ✅ | 75000 | 正确，无需修改 |
| `slot_b_hwm` | 3.691 ✅ | 3.691 | 正确，无需修改 |

最终账本：`SlotA=511260.SH/75000/HWM=135.215` + `SlotB=159915.SZ/75000/HWM=3.691`

### 变更文件：`intraday_reconcile.py`（多策略防火墙）

**[BUG] 根因：ETF OU Grid 标的被 T0 对账引擎误清仓**

- **故障现象**：`520500.SH` 属于 ETF OU Grid（持仓 4 格共 27800 股），但同时存在于 `grid_state.json`（T0 账本，volume=0）。对账引擎对比时判断"账本0股 vs 实盘27800股"，触发 `eod_clear` 强制清仓，造成 OU Grid 仓位被误平。
- **根因**：`intraday_reconcile.py` 防火墙仅保护 Rotation + T1，**ETF OU Grid、Momentum、Sniper、MacroRotation、Fat Fish、Underdog 均无保护**。

**[ARCH] 修复：新增 `_load_all_protected_codes()` 全策略防火墙**

读取 7 个策略账本，构建统一保护集：

| 策略 | 账本文件 | 读取方式 |
|------|---------|---------|
| ETF OU Grid | `etf_grid_positions.json` | dict.keys() |
| Momentum | `momentum_holdings.json` | dict.keys() |
| Sniper | `sniper_holdings.json` | dict.keys() |
| MacroRotation | `macro_slots.json` | slot_a / slot_b 字段值 |
| T1 Grid | `t1_grid_ledger.yaml` | dict.keys() |
| Fat Fish | `fat_fish_slots.yaml` | slots.keys() |
| Underdog | `underdog_slots.json` | dict.keys() |

防护生效位置：
1. **对账主循环**（L174）：`if code in protected_codes: continue`
2. **EOD 二次强清**（L241）：`if code in protected_codes: continue`

所有账本读取失败均静默（`except: pass`），不阻断对账主逻辑。

**验证**：`py_compile intraday_reconcile.py` → **Exit 0**

### 变更文件：`fat_fish_master.py`（量纲铁律：vol → amount）

**[ARCH] 工程铁律落地：永远不用 volume（成交量）做跨数据源比较，只用 amount（成交额/元）**

根因：不同 API（xtdata / parquet / Tick）返回的 `vol` 字段单位不一致（股/手/手×100），直接比较会失真。`amount`（人民币元）量纲在任何接口中绝对统一。

**三处基因替换**：

| 位置 | 旧 | 新 |
|------|----|----|
| L183 因子计算 | `ma_vol_20 = vol.rolling(20)` | `ma_amount_20 = amount.rolling(20)` |
| L198 momentum_score | `vol / ma_vol_20` | `amount / ma_amount_20` |
| L206 must_valid 守门 | `'ma_vol_20'` | `'ma_amount_20'` |
| L322 开仓条件 | `vol > 1.5 × ma_vol_20` | `amount > 1.5 × ma_amount_20` |
| L323 乖离防线 | `<= 2 × atr_14` | `<= 2.5 × atr_14`（放宽）|

**新增诊断探针**：`cond_breakout` 但未通过其他两关时，打印标的代码 + 未通过原因（`额比=X.XXx` / `偏离过大`），便于每日复盘哪些标的卡在哪里。

**[FEAT] N8N Webhook 推送接入（quant-v4-patterns §13 标准）**

按策略三件套规范补齐 `fat_fish_master.py` 的 N8N 推送，4 个节点：

| 触发节点 | 推送标题 | 触发条件 |
|---------|---------|---------|
| 全局熔断 | `🚨 胖鱼全局熔断` | 市场宽度 < 20% |
| 右侧逃顶 | `🔪 胖鱼右侧逃顶 {code}` | RSRS Z-score > 1.2 + MA10 破位 |
| 信号生成 | `🎯 胖鱼信号生成 N 条` | 有买入信号写入 signals.json |
| 无信号 | `📭 胖鱼今日无信号` | 全池扫描无标的通过三铁律 |

实现模板符合 §13.1：`try: load_dotenv()` + `requests.post(timeout=5)` + `except: pass`（推送失败不影响交易逻辑）。

**验证**：`py_compile fat_fish_master.py` → **Exit 0**

### 变更文件：`fat_fish_executor.py`（N8N 推送接入）

**[FEAT] 执行器 N8N 推送 — 5 个关键节点**

| 触发节点 | 推送标题 | 说明 |
|---------|---------|------|
| 无指令文件 | `⏸️ 胖鱼火炮静默` | orders.yaml 且 signals.json 均不存在 |
| QMT 连接失败 | `❌ 胖鱼火炮 QMT 连接失败` | miniQMT 连接失败，全部指令未执行 |
| 买入成交落盘 | `✅ 胖鱼开仓成交 {code}` | 每笔 Fill-Based 成交写槽位后推送（含成交价/量/止损线） |
| 执行完成汇总 | `🏁 胖鱼火炮执行完成` | 清仓笔数 + 买入笔数 + 归档文件名 + 时间戳 |

`send_n8n_alert()` 独立函数，与 master 规范对齐（`timeout=5`，`except: pass`）。

**验证**：`py_compile fat_fish_executor.py` → **Exit 0**

### 变更文件：`autopilot_master.py`


**[FEAT] 单例运行锁（Singleton Lock）**

- 新增 `import socket`（第 19 行）
- 新增 `enforce_single_instance()` 函数（import 区末尾，UTF-8 配置块之前）
- 调用位置：任何业务逻辑启动前（含 logging 初始化、load_dotenv 之前）

**实现原理**：
- 绑定本地 TCP 端口 `127.0.0.1:38888`（`SO_STREAM`，排他性）
- 绑定成功 → 唯一实例，继续启动
- 绑定失败（`socket.error`）→ 检测到另一进程已在运行，立即 `sys.exit(1)`

**为何优于文件锁（.lock 文件）**：
- 进程强制 Kill、断电崩溃后，OS 立即回收 socket 端口
- 文件锁可能因崩溃未删除导致下次误拦截（stale lock）
- Socket 端口在同一时刻绝对不允许两个进程共享（Windows 物理保证）

**防御场景**：
1. 计划任务（WOL 唤醒后 autopilot 已运行，08:50 定时任务重复触发）
2. 手工误操作（用户手动再次运行 autopilot_master.py）
3. 看门狗误重拉（外部 watchdog 未检测到进程状态正确时误复活）

**验证**：`py_compile autopilot_master.py` → **Exit 0**

### 变更文件：`t0_multigrid_executor.py` + `etf_ou_grid_master.py`（弹性加固）

**[FIX] T0 进程锁双保险升级（`acquire_lock_with_ttl`）**

今日故障根因：`executor.lock` 遗留孤儿锁（PID=2884，昨日进程已死），但 T0 每次启动只存活 6~49 秒（因 QMT session 冲突），始终未能撑过 300s TTL，导致孤儿锁全天有效，T0 完全不工作。

修复方案（双保险）：
1. **PID 存活校验**（新增，优先级最高）：启动时读锁文件内的 PID，用 `psutil.pid_exists()` 验证进程是否仍活着。进程已死 → 立即粉碎，0 秒响应，无需等 TTL。
2. **TTL 从 300s → 60s**（缩短）：即使 psutil 不可用，最多 60 秒后孤儿锁自动清除，不再卡住整个交易日。

```python
# 旧：max_age_seconds=300，纯 TTL 机制
# 新：PID 存活校验（死亡即刻粉碎）+ TTL=60s 兜底
if not pid_alive:
    os.remove(lock_path)   # 立即粉碎
elif file_age > 60:
    os.remove(lock_path)   # TTL 兜底
```

**[FIX] ETF_OU_Grid 席位解算文件缺失降级兜底（`etf_ou_grid_master.py`）**

今日故障根因：`oracle_v2_universe.json` 误删，第 132 行裸 `open()` 直接抛 `FileNotFoundError`，席位解算 exitcode=1，ETF_OU_Grid 全天无席位。

修复方案（自动降级重建）：
- `oracle_v2_universe.json` 存在 → 正常读取（原逻辑）
- 文件缺失 → 自动读取 `top100_liquidity.csv` 重建候选池，写回 `oracle_v2_universe.json`，席位解算继续运行
- 两个文件均不存在 → 打印明确错误后 `return`（不再崩溃抛异常）

**验证**：`py_compile t0_multigrid_executor.py` → **Exit 0**；`py_compile etf_ou_grid_master.py` → **Exit 0**

---

## 📅 2026-04-23 — 四大铁闸加固（etf_ou_grid_executor.py v4.9 + autopilot 熔断器核查）

### 变更文件：`etf_ou_grid_executor.py`（v4.8 → v4.9）

**[FIX] 铁闸一：午休物理锁升级为三段全覆盖**
- 原代码：主循环只锁午休（`11:30~13:00`），开盘前（`<09:30`）和收盘后（`>=15:00`）无锁
- 改用 `%H%M` 纯数字格式避免字符串字典序陷阱，同时在 `while True` 最开头用一个 `if/continue` 块覆盖三段非交易时间：
  - `now_hm < "0930"` → 开盘前静默等待（10s sleep）
  - `"1130" <= now_hm < "1300"` → 午休挂起（10s sleep）
  - `now_hm >= "1500"` → 收盘后夜间待机（10s sleep）
- 效果：彻底防止 `run_hybrid_executor` 在非交易时间被调用，消灭夜间/开盘前触发下单的根本风险

**[VERIFY] 铁闸二（on_stock_order）**：已在 v4.7 实现，`{50,52,54}` 三终态才 pop pending，中间态 return，**无需修改**。

**[VERIFY] 铁闸三（on_stock_trade 分笔累加）**：已在 v4.5 实现，`filled_so_far >= qty` 才 pop，**无需修改**。

**[VERIFY] 铁闸四（autopilot_master 熔断器）**：已在 L265-L370 实现：`restarts > WATCHDOG_MAX_RESTART=5` 时 break + send_webhook + update_status="error"，**无需修改**。

**验证**：`py_compile etf_ou_grid_executor.py` → **Exit 0**

---

## 📅 2026-04-23 — etf_ou_grid_executor.py v4.7/4.8 三大增强 + 机枪根因修复

### 变更文件：`etf_ou_grid_executor.py`（v4.6 → v4.8）

**[FIX] 修复一：on_stock_order 状态锁（v4.7）**
- 新增 `on_stock_order` 回调，仅在状态码 `{50已撤, 52废单, 54部撤}` 时清理 pending
- 中间态（48未报/56已报/53部成）一律 return，不干扰午休挂单的 pending 状态

**[FIX] 修复三：午休硬熔断（v4.7）**
- `while True` 主循环开头加入 `if "11:30" <= now_hhmm < "13:00": continue`
- 物理禁止午休期间触发任何建仓/平仓逻辑

**[BUG] 机枪连发根因修复（v4.8）**
- 根因：`_sweep_stale_pending` 每轮调用，其 `finally` 块会 `_pending.pop(seq)` 清除 `fail_` 字符串冷静 key，导致下轮守卫失盲，机枪重启
- 修复：新增**独立冷静字典** `_failed_cooldowns: dict`（与 `_pending` 完全隔离）
  - `_set_failed_cooling(code, lvl, dir)` → 写入 `expire_ts = now + 30s`
  - `_is_failed_cooling(code, lvl, dir)` → 过期自清理，返回 True 阻断
  - `_is_order_pending` 优先查冷静字典再查 `_pending`
  - `_place_order` 失败时调 `_set_failed_cooling`，不再写 `_pending`
- 效果：格5 废单(seq=-57)后 30s 内任何发单请求被直接拦截，N8N 不再收到洪水

**验证**：`py_compile` → **Exit 0**；PID=12956 午休熔断中（12:00~13:00）

---

## 📅 2026-04-23 — etf_ou_grid_executor.py v4.5 四大 Hotfix

### 变更文件：`etf_ou_grid_executor.py`（v4.4 → v4.5）

**[BUG] 修复一：分笔成交黑洞 (Callback Pop Bug)**
- 根因：`_pending.pop(seq)` 在第一笔分笔到达时即弹出 meta，后续分笔找不到 meta → 回调静默丢弃，账本只记录第一笔成交量
- 修复：`pop` → `get`（只读），新增 `meta['filled_so_far'] += filled` 累加；只有 `filled_so_far >= target_qty` 才执行 `pop`

**[BUG] 修复二：机枪连发守卫 (Pending Guard)**
- 新增辅助函数 `_is_order_pending(code, grid_lvl, direction)`：加锁遍历 `_pending`，命中同标的/同格/同方向的在途委托返回 True
- 覆盖位置：断头台/时间止损/浅水止盈/深水爆破（sell）+ 首网/深潜加仓（buy）共 6 处
- 命中时 `surviving.append(p); continue`，格保留到下一轮，不重复发单

**[FIX] 修复三：止盈/时间止损后封小黑屋 (Whipsaw Loop)**
- 浅水止盈成功（`_seq_sh > 0`）+ 时间止损成功（`_seq_ts > 0`）→ 强制 `_add_to_blacklist(code)`
- 效果：卖出后 48h 内不开新仓，彻底消灭"卖完立刻重买"循环

**[FIX] 修复四：卖单 bid1 Taker 价 (Anti-Stale-Order)**
- 四条卖出路径（断头台/时间止损/浅水止盈/深水爆破）均改为：
  ```python
  bid1_list  = tick.get('bidPrice', [price])
  bid1       = float(bid1_list[0]) if bid1_list and bid1_list[0] > 0 else price
  sell_price = round(bid1 - 0.002, 3)
  ```
- 急跌时 lastPrice 可能已是成交价，用 bid1-0.002 保证 Taker 扫盘成交

**验证**：`py_compile etf_ou_grid_executor.py` → **Exit 0**；已重启 PID=16036

---

## 📅 2026-04-23 — 孤儿持仓检测（首网护盾三）+ 低卖高买竞态修复

### 变更文件：`etf_ou_grid_executor.py`（v4.2 → v4.3）

**[BUG] 账本清零后重启，忽略实盘已有持仓，直接触发首网买入**

**故障现象**（10:38）：
- 09:41 紧急清仓 → 实盘 159518.SZ 清零
- 10:36 低卖高买 Bug → 错误买入 25,600 股（未被清仓脚本覆盖）
- 人工清零账本并重启 → 引擎看账本为空 → 直接买入 12,800 股首网
- 结果：实盘 25,600 + 新买 12,800 = **38,400 股**（含大量孤儿仓）

**[FIX] 首网护盾三：孤儿持仓检测**
- 位置：首网买入 `else:` 分支入口（小黑屋/趋势护盾通过后，开仓前）
- 逻辑：`query_stock_positions` 物理查仓，若实盘有持仓但账本为空
  - 收编：写入账本（格1，用券商成本价 `open_price`）
  - 跳过：不发买单，防止重复建仓
  - 告警：N8N 推送 + `ORPHAN_ADOPTED` 遥测记录

**[OPS] 10:57 再次紧急清仓**：38,400 股 @ 1.1730 → 成功清仓 ≈ 45,043 元

**验证**：`py_compile etf_ou_grid_executor.py` → **Exit 0**；已重启 PID=14092

---

## 📅 2026-04-23 — 低卖高买同轮竞态 Bug 修复（etf_ou_grid 第三次加固）

### 变更文件：`etf_ou_grid_executor.py`（v4.1 → v4.2）

**[BUG] 根因：止盈卖出与首网买入在同一轮循环中被同时触发**

**故障现象**（10:36 盘中捕捉）：
```
💰 [159518.SZ] 浅水区 格1 止盈 +1.31%
📉 [下单] 159518.SZ sell 格1 | 13000股 @ 1.162 | seq=...    ← 低价卖出
🌊 [首网] 159518.SZ 偏离0轴，买入 qty=12800 @ 1.164          ← 高价买入！同一轮！
✅ [Fill-Sell] 成交 @ 1.162
✅ [Fill-Buy]  成交 @ 1.163    ← 低卖高买，净亏约 130 元
```

**竞态机制**：
```
for p in pos_list:                     # 遍历每个格
    if profit >= step:
        _place_order(...sell...)
        sold = True                    # 格被剔出 surviving
# surviving = []（所有格已售出）
if not surviving:                      # ← 同轮立刻判断
    if price < ma20*(1-step):
        _place_order(...buy 首网...)   # ← 同轮触发！卖单未成交，账本格未清
```

**[FIX] `any_sold_this_round` 守卫**：
- 新增布尔标记 `any_sold_this_round = False`（每标的每轮重置）
- 四处卖出路径（断头台/时间止损/浅水止盈/深水爆破）设 `any_sold_this_round = True`
- 建仓侦测入口：`if any_sold_this_round: 跳过 elif not surviving: ...`
- 效果：止盈后当轮不买，等 5s 后下一轮账本回调清格后再判断是否需要首网

**验证**：`py_compile etf_ou_grid_executor.py` → **Exit 0**

---

## 📅 2026-04-23 — 紧急清仓 + 两大策略 Bug 修复

### [OPS] 紧急强制清仓（09:41）

**背景**：ETF_OU_Grid 断头台 Bug 导致 159518.SZ 大规模重复买入（累计持仓 104600股），Hawkes 死锁 Bug 导致 513300.SH 45s 时间斩仓未触发（持仓 8400股）。

**`emergency_liquidation.py`（新建）**

| 标的 | 策略 | 卖出量 | bid1 | 预计金额 | 结果 |
|---|---|---|---|---|---|
| 159518.SZ | ETF_OU_Grid | 104600股 | 1.1460 | ≈119,872元 | ✅ 4笔分批成交 |
| 513300.SH | Hawkes_V3  | 8400股  | 2.3360 | ≈19,622元  | ✅ 1笔成交 |

**执行流程**：`query_stock_positions` 物理查仓 → `FIX_PRICE bid1` 追单 → `on_stock_trade` 回调确认 → 账本清零

**账本清零**：
- `etf_grid_positions.json` → `159518.SZ: []`
- `hawkes_holdings.json` → `{}`

---

## 📅 2026-04-23 — ETF_OU_Grid 断头台竞态 Bug 修复（大规模重复买入根治）

### 变更文件：`etf_ou_grid_executor.py`（v4.0 → v4.1）

**[BUG] 根因：断头台触发后每轮重复买入首网（159518.SZ 失控案例）**

**故障现象**：159518.SZ 在盘中被断头台触发后，出现大量 `buy 格1 13000股` 的重复买入委托，小黑屋完全失效，每 5 秒一次循环不断买入。

**三重根因拆解**：

| # | Bug | 位置 | 后果 |
|---|---|---|---|
| **Bug 1** | 首网建仓路径（`surviving=[]` 分支）完全缺少小黑屋/趋势护盾检查 | L595 `else:` 直接进入买入 | 断头台关入小黑屋后，下一轮 surviving=[] 触发首网，绕过所有护盾 |
| **Bug 2** | 断头台卖单下单失败（`seq=-58`）时仍设 `sold=True` | L524 `_add_to_blacklist + sold=True` 无条件执行 | 卖单未发出格却被从 surviving 剔除 → surviving=[] → 触发首网 |
| **Bug 3** | `_blacklist` 是模块级内存变量，重启后丢失 | 无持久化机制 | 进程重启（看门狗复活）后小黑屋归零，立刻可买入 |

**修复方案（三处联动）**：

**[FIX 1] 首网路径补齐双重护盾**
```python
# 原代码（缺失检查）：
if is_meltdown: ...
else: _place_order(... BUY 首网 ...)   # ← 直接买！

# 修复后（三重门）：
if is_meltdown: ...
elif _is_blacklisted(code):             # ← 补充：小黑屋拦截
    write_grid_telemetry("BLACKLIST_REJECT"...)
elif is_fatal_downtrend(code):          # ← 补充：趋势护盾
    write_grid_telemetry("DOWNTREND_REJECT"...)
else: _place_order(... BUY 首网 ...)
```

**[FIX 2] 断头台 sold 标记与卖单成功提交绑定**
```python
# 原代码（无条件 sold=True）：
_place_order(...sell...)
_add_to_blacklist(code)
sold = True   # ← 即使卖单 seq<0 也设 sold！

# 修复后（绑定 seq 成功）：
seq_sl = _place_order(...sell...)
if seq_sl > 0 or sell_qty <= 0:
    _add_to_blacklist(code)
    sold = True      # ← 仅卖单成功提交才剔除格
else:
    # 卖单失败：格保留在 surviving，下轮重试
    print("⚠️ 卖单下单失败，格保留，小黑屋不激活")
```

**[FIX 3] 小黑屋持久化（重启不丢失）**
- 新增 `BLACKLIST_FILE = .state/etf_grid_blacklist.json`
- `_add_to_blacklist()` 关入小黑屋后立即 `_save_blacklist_to_disk()`（原子写入）
- `__main__` 启动时调用 `_load_blacklist_from_disk()`，恢复尚未过期的惩罚条目
- 写盘：`json.tmp → os.replace()` 原子替换（铁律四）

**四大铁律合规检查**：
- 铁律一：sold 标记与 seq>0 绑定，卖单失败时格保留在 surviving ✅
- 铁律二：账本决策只读自身 etf_grid_positions.json ✅
- 铁律三：卖出量读账本 bought_qty，不重算 ✅
- 铁律四：_BLACKLIST_LOCK 保护内存写入，json.tmp 原子替换写盘 ✅

**验证**：`py_compile etf_ou_grid_executor.py` → **Exit 0**

---

### 变更文件：`hawkes_executor.py`（死锁修复 — 45s 时间斩仓永不触发）

**[BUG] 根因：`on_order_trade` BUY 回调中 `_holdings_mu` 死锁，账本写不进去**

**故障现象**：Hawkes 买入成交后，`_micro_exit_monitor` 读到的账本始终为 `{}`（空），`holding` 记录永远不存在，导致 45s 时间斩仓条件永远无法触发，持仓被无限期持有。

**根因分析（死锁链路）**：

```
on_order_trade(BUY) 被 QMT 回调线程调用
  └─ with _holdings_mu:          ← ① 加锁（threading.Lock，不可重入）
       └─ h = _load_holdings()   ← ② _load_holdings 内部再次 with _holdings_mu
                                       ← ③ 同一把锁已被持有，当前线程永久阻塞！
```

Python `threading.Lock()` **不可重入**，同一线程两次 `acquire` 会立即死锁。外层 `with _holdings_mu:` 已持有锁，内部调用 `_load_holdings()` / `_save_holdings()` 又各自 `with _holdings_mu:` → **永久死锁**。

**影响链**：
- `on_order_trade` BUY 回调卡死 → 账本永远写不进 `holding`
- `_micro_exit_monitor` 每秒读账本 → 始终 `{}`，无 holding → 不触发任何止盈/止损/时间斩仓
- `_current_exposure` 正常充值 → 锁C逻辑正常触发敞口平仓（这是唯一能平仓的路径）

**[FIX] 拆分无锁内部版本**

```python
# 新增无锁版（仅供已持有 _holdings_mu 的调用方使用）：
def _load_holdings_unlocked() -> dict: ...
def _save_holdings_unlocked(h: dict): ...

# 带锁版（供外部所有调用方）：
def _load_holdings() -> dict:
    with _holdings_mu:
        return _load_holdings_unlocked()   # 委托无锁版

def _save_holdings(h: dict):
    with _holdings_mu:
        _save_holdings_unlocked(h)         # 委托无锁版

# on_order_trade BUY 分支修复：
with _holdings_mu:
    h = _load_holdings_unlocked()          # ← 改为无锁版，消除死锁
    ...
    _save_holdings_unlocked(h)             # ← 改为无锁版
```

**验证**：`py_compile hawkes_executor.py` → **Exit 0**

---

## 📅 2026-04-21 — 断电后系统恢复 + 幽灵持仓清除 + Oracle Arena 评测器

### 变更文件：`fat_fish_master.py`

**[OPS] 预言机测谎模块暂时禁用（L338-L403）**

- 原因：TimesFM 服务 `http://10.10.8.20:8000` 尚未完成 `oracle_validator.py` 回测验证
- 操作：将 66 行预言机调用代码整块注释，候选池仍以物理三铁律（突破+放量+未延伸）筛选，按动能排序取前N
- 解除条件：`oracle_arena_results.csv` 回测完成，Model_A 方向准确率 > 随机水准后解注释

**验证**：`py_compile fat_fish_master.py` → **Exit 0**



### 变更文件：`oracle_validator.py`（[NEW]）

**[FEAT] 双预言机竞技场历史回测评测器**

| 维度 | 设计 |
|---|---|
| 数据来源 | **[NEW] 直接读 `Data/Market_Daily/*.parquet`（离线，无需 miniQMT）** |
| 标的 | 9 只宏观 ETF（8/9 有 parquet，511260.SH 被 junk_pattern 过滤需补录） |
| 时间漫游 | 按真实交易日推进，每日截取前 20 根 K 线（`iloc[pos-20:pos]`） |
| 无未来函数 | 传给 API 的序列严格不含当日，T+5 用 `iloc[pos+5]` |
| 并发 | `ThreadPoolExecutor(max_workers=18)`，Model_A/B × 8 ETF 完全并发 |
| 失败降级 | 超时/连接失败/API不可达 → None，不阻断主循环 |
| 落盘 | `oracle_arena_results.csv`，tmp→os.replace() 原子写入 |

**离线验证结果（2026-04-21 11:32）**：
- 8 只 ETF × 554 个交易日（2024-01-02 → 2026-04-20）
- 加载耗时 < 0.1s，无需 miniQMT 进程

**验证**：`py_compile oracle_validator.py` → **Exit 0**；离线数据加载自检 **通过**




### 变更文件：`.state/fat_fish_slots.yaml`

**[BUG] 胖鱼防线幽灵持仓（Ghost Position）：516110.SH 从未实际成交**

**根因**：
- `fat_fish_executor.py` 在 2026-04-16 尾盘对 516110.SH 下单成功（`order_stock()` 返回有效 seq）
- 执行器在发单成功后**立即预写** `fat_fish_slots.yaml`（乐观写入，未等待 `on_order_trade` Fill 回调）
- 委托实际**未成交**（价格未触碰 / 流动性不足 / 收盘撤单），但账本已写入
- `fat_fish_guard.py` 此后每日把这条记录当作真实持仓，占用一个槽位并监控止损

**证据链**：
| 日志文件 | 字段 | 结论 |
|---|---|---|
| `20260416_fat_fish_executor.log` | 有 `[执行突击]` + `[槽位落盘]`，**无任何 `on_order_trade` Fill 回调** | 下单但未成交 |
| miniQMT 实仓查询 | `query_stock_positions` 返回 5 只，**不含 516110.SH** | 实仓物理确认为空 |

**[FIX]** 清除幽灵持仓：
```diff
- slots:
-   516110.SH:
-     buy_date: '2026-04-16'
-     buy_price: 1.349
-     highest_price: 1.349
-     shares: 14800
-     stop_loss_price: 1.307
+ slots: {}
```

备份文件：`.state/fat_fish_slots.yaml.bak_20260421_poweroff`

**[PATTERN] 乐观预写陷阱（Optimistic Ledger Write Anti-Pattern）**：
> `fat_fish_executor.py` 的槽位落盘在 `order_stock()` 成功后立即触发，属于"乐观预写"模式。
> 铁律一要求只有 `on_order_trade` Fill 回调才能写账本。
> 胖鱼防线的正确架构应为：下单 → 注册 pending → Fill 回调 → 写槽位。
> **后续改版优先级：fat_fish_executor.py 改为 Fill-Based 写槽位。**

---

### [OPS] 断电恢复流程记录

**断电时间**：2026-04-21 上午（具体时刻未知，系统 10:41 重新上线）

**恢复流程**：
1. autopilot_master 于 10:41:52 自启（WOL / 手动上电后系统自启）
2. Watchdog 10:41:53 检测到 QMT 进程失踪，触发紧急恢复
3. 10:44:14 Watchdog 报告 QMT 进程恢复成功
4. 10:48 手动启动额外 autopilot_master 实例确认引擎调度

**手动重启引擎（因 autopilot timeflag 已触发，watchdog 不再自动重拉）**：
| 引擎 | PID | 状态 |
|---|---|---|
| fat_fish_guard | 12444 | ✅ 运行中，130MB 活跃 |
| hawkes_executor | 12084 | ✅ 运行中，live log 持续更新（10:50 确认）|
| momentum_vector_executor | 13072 | ✅ 运行中，10:49 连接 QMT 成功 |

**清理过期锁文件**：
- `executor.lock`（原 PID=6872 已死亡）→ 已删除

**MacroRotation Sentinel 数据缺失**（待自愈）：
- 9 只宏观标的全报"本地缓存无数据"，根因：断电后 miniQMT 未重建 tick 订阅缓存
- 影响：Sentinel 守卫本次巡逻失效，持仓未被监控
- 预期：11:30 自动触发下一次 Sentinel，miniQMT 重启后缓存将自动重建

---

### 变更文件：`fat_fish_executor.py`（Fill-Based 架构修复）

**[ARCH] 乐观预写反模式 → Fill-Based 记账重构**

| 对比维度 | 旧架构（乐观预写） | 新架构（Fill-Based）|
|---|---|---|
| 槽位写入时机 | `order_stock()` seq>0 即写 | `on_order_trade()` 成交回调后写 |
| 未成交的委托 | 账本有记录，实仓为空 → **幽灵持仓** | pending 被 sweep/废单回调清除，账本不变 |
| 成交价记录 | 用发单时 lastPrice 估算 | 用实际 `trade.traded_price`（精确） |
| 并发安全 | 无锁 | `_ff_pending_lock` + `_ff_slots_lock` 双锁 |

**[FEAT] 三大新增组件**

1. **`FatFishCallback(XtQuantTraderCallback)`** — Fill 回调类
   - `on_order_trade(BUY)` → 写 `_ff_filled_slots`（主线程稍后落盘）
   - `on_order_trade(SELL, 含 Fat_Fish 标记)` → 写 `_ff_sold_codes`（稍后清槽位）
   - `on_order_error()` → 从 `_ff_pending` 清除，不写账本

2. **`_sweep_pending(trader, acc)`** — 超时 60s 补录函数
   - 物理查 `query_stock_trades()`，按 `order_id` 匹配
   - 有成交量 → 补写槽位；无成交量 → 视为废单，不写账本

3. **阶段3：等待 Fill + 落盘流程**
   - 轮询 `_ff_pending` 是否清空（2s 间隔，最多 60s）
   - 超时调 `_sweep_pending`
   - 收集 `_ff_filled_slots` → `save_slots()`（原子写入）
   - 收集 `_ff_sold_codes` → 从 slots 中删除对应条目

**全局状态变量（模块级）**：
```python
_ff_pending: dict[int, dict]    # {seq: {code, shares, buy_price, atr_14, sent_at}}
_ff_pending_lock: threading.Lock
_ff_filled_slots: dict[str, dict]  # {code: slot_dict}，回调线程写、主线程读
_ff_slots_lock: threading.Lock
_ff_sold_codes: set              # 卖出确认 code
_ff_sold_lock: threading.Lock
PENDING_TIMEOUT_SEC = 60
```

**四大铁律合规检查**：
- 铁律一：发单后注册 `_ff_pending`，仅 `on_order_trade` 回调后写 `fat_fish_slots.yaml` ✅
- 铁律二：账本决策只读 `fat_fish_slots.yaml`，不查 QMT 全局持仓（除卖出时验证可用量） ✅
- 铁律三：卖出量来自 `query_stock_positions` 物理查仓 `can_use_volume` ✅
- 铁律四：`_ff_pending_lock` / `_ff_slots_lock` / `_ff_sold_lock` 三锁保护 ✅

**验证**：`py_compile fat_fish_executor.py` → **Exit 0**

---

## 📅 2026-04-20 — Hawkes 实盘激活


### 变更文件：`hawkes_executor.py`

**[OPS] 实盘激活（收盘后配置）**

| 参数 | 旧值 | 新值 |
|---|---|---|
| `FIRE_PAUSED` | `True`（沙盘模式） | **`False`（实盘开火）** |
| `MARKET_OPEN_TIME` | `13:00:00`（下午测试临时值） | **`09:30:00`（正式开盘时间）** |

**验证**：`py_compile hawkes_executor.py` → **Exit 0**

---



### 变更文件：`hawkes_executor.py`

**[BUG] Bug1：冷启动 PermissionError — `os.makedirs("logs", ...)` 相对路径触雷**

**根因**：第 124 行模块顶层（`__main__` 之外）使用相对路径 `"logs"`，当 autopilot 以非本项目根目录为 cwd 启动子进程时，`"logs"` 解析到系统路径，触发 `WinError 5 Access is denied`。

**现象时间线**：
- 09:22:12 启动 → crash（PermissionError）
- 09:22:20 第1次复活 → crash（同）
- ...共 5 次 crash，09:22:41 第6次才以正确 cwd 启动成功

**[FIX]** 引入 `_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))`，所有路径常量（`LOGS_DIR / STATE_DIR / HOLDINGS_FILE / T0_POOL_CSV / _LOG_FILE / _TELEM_FILE / _PAPER_LEDGER_FILE`）全部改为绝对路径拼接，彻底消除对 cwd 的依赖。

---

**[BUG] Bug2：`513050.SH` 锁A永久致盲 — Fill SELL 回调丢失未兜底**

**根因**：09:22:47 开盘第一秒，`513050.SH` 触发止损，发出卖单 `seq=1098965464`，但 `on_order_trade` SELL 回调从未到达（QMT 回调丢失或订单未成交通知）。`_pending_locks` 中 `513050.SH` 永远未被解锁，全天 3000+ 次 Tick 全部进入"锁A致盲"路径，该标的丧失全天开火能力。

**[FIX] 三处联动**：

| 位置 | 修复内容 |
|---|---|
| 全局状态 | 新增 `_pending_lock_timestamps: dict` + `_PENDING_LOCK_TIMEOUT_SEC = 180` |
| `_pending_locks.add(code)` | 同步记录时间戳进 `_pending_lock_timestamps` |
| `_clear_holding_and_unlock()` | `discard` 时同步 `.pop(code, None)` 清理时间戳 |
| `_micro_exit_monitor()` 监控循环 | 每秒顺带扫描 `_pending_lock_timestamps`，超过180s 且账本无 `holding/exiting` 记录 → 强制 `discard` + N8N 告警 |

**[PATTERN] 锁A超时守卫模式**（新设计模式）：
> 所有使用「悲观在途锁 + Fill 回调解锁」架构的执行器，都应配备超时守卫，防止回调丢失导致标的永久失能。守卫粒度：`_PENDING_LOCK_TIMEOUT_SEC = 180s`（远超正常成交延迟，避免误触发）。

**验证**：`py_compile hawkes_executor.py` → **Exit 0**

---

**[FEAT] Bug3：无开盘时间门控 — 集合竞价阶段可被触发**

**根因**：Hawkes 09:22 启动后立即可响应 Tick 信号，无 09:30 时间门控。集合竞价（09:15~09:30）阶段盘口宽、价格跳动大，λ 和 OBI 均可能被虚假大单击穿，导致在错误时间开火（今日 09:22:47 止损案例即源于此）。

**[FIX]** 新增两个时间常量，并在 `_fire_engine_check` 中 λ 计算之后、`LAMBDA_THRESHOLD` 检查之前插入时间门控：

```python
MARKET_OPEN_TIME   = datetime.time(9, 30, 0)   # 连续竞价开始
MARKET_CLOSE_TIME  = datetime.time(14, 57, 0)  # 新仓截止（收盘前3分钟）

# λ 引擎在 09:30 前正常热身（不 skip process_tick），但 return 不开火
if _now_t < MARKET_OPEN_TIME:
    return   # 集合竞价：λ 已更新，静默不开火
if _now_t >= MARKET_CLOSE_TIME:
    return   # 收盘前3分钟：不开新仓
```

**设计要点**：`process_tick()` 在时间门控之前已调用，引擎状态正常热身。时间门控只拦截「开火决策」，不影响「信号探测」，保证 09:30 连续竞价一开始引擎即可立即响应真实信号。

**验证**：`py_compile hawkes_executor.py` → **Exit 0**

---

## 📅 2026-04-17 — 悲观并发锁（Shadow Ledger）



### 变更文件：`hawkes_executor.py`（终版防连发架构）

**[ARCH] 悲观并发锁设计**

JSON 账本只由物理成交回调（`on_order_trade`）写入，绝不被预估污染。  
在途封条（`_in_flight`）纯内存 `set`，O(1) 查找，负责幽灵时间窗口防护。

```
order_stock() 成功
    _in_flight.add(code)   ← 在途封条即时生效
        │
        ├─ on_order_trade(buy) → JSON 写 holding + _in_flight.discard(code)
        └─ on_order_error      → _in_flight.discard(code)（零磁盘 I/O）

on_tick fire 信号
    ① code in _load_holdings()  → return  （已成交，JSON 持仓锁）
    ② code in _in_flight        → return  （在途，内存封条锁）
    ③ 冷却期 60s               → return  （兜底）
```

**[OPS] 验证（10:40）**：30s 内 4 次 FIRE，各为不同标的，无同标的连发。  
**验证**：`py_compile hawkes_executor.py` → **Exit 0**

---


### 变更文件：`hawkes_executor.py`（三处联动修改）

**[BUG] 根因：持仓锁依赖异步回调，高频场景下有窗口期**

`on_order_trade` 是异步回调，委托发出到成交通知有毫秒~秒级延迟。在此窗口期内 `hawkes_holdings.json` 仍为空，持仓锁失效，下一个 λ 脉冲就再次开火，引发连续超买。

**[ARCH] 修复方案：三处联动**

| 位置 | 旧行为 | 新行为 |
|---|---|---|
| `_execute_fire` | 只注册 `_hawk_pending`（内存） | 同步写磁盘 `status=pending`，持仓锁即时封闭 |
| `on_order_trade(buy)` | 写 holding（无锁） | 加 `_holdings_lock`，`pending → holding` VWAP 升级 |
| `on_order_error` | 只清内存 pending | 同时清磁盘账本 `pending` 条目，释放持仓锁 |

**[PATTERN] 高频记账三阶段状态机**

```
order_stock() → {status: "pending"}  ← 持仓锁立即封闭
    ↓ on_order_trade
               → {status: "holding"} ← VWAP 实际成交价覆盖
    ↓ on_order_error
               → 账本条目删除        ← 持仓锁立即释放
```

**验证**：`py_compile hawkes_executor.py` → **Exit 0**

---


### 变更文件：`hawkes_executor.py`（两道物理保险：持仓锁 + 冷却期）

**[FEAT] 保险一：单标的持仓锁（Position Lock）**

在 `on_tick` 开火判断的最顶端（优先级最高），SIGNAL_DETECTED 遥测之前：

```python
if _code in _load_holdings():
    return   # 该标的还有未平仓位，λ 飞到火星也绝不开第二枪
```

**根因**：防火墙 1 在 `_execute_fire()` 内部才检查，但在此之前 SIGNAL_DETECTED 遥测已写入，且下一次重启时 `_hawk_pending` 是空的，防火墙失效。把锁提到 `on_tick` 物理上最早处，无论进程状态如何都绑死。

**[FEAT] 保险二：点火冷却期（Cooldown Timer）**

```python
FIRE_COOLDOWN_SEC = 60   # 配置区

_last_fire_time: dict[str, float] = {}   # 全局状态

# on_tick 中：
_now = time.time()
if _now - _last_fire_time.get(_code, 0.0) < FIRE_COOLDOWN_SEC:
    return
...
_last_fire_time[_code] = _now   # 开火后更新时间戳
```

**作用**：即使持仓锁因账本延迟（回调未到）而短暂失效，60 秒冷却期也是最后一道物理防线，杜绝连发。

**[OPS] 触发顺序（优先级从高到低）**：
```
保险一持仓锁 → 保险二冷却期 → FIRE_PAUSED → 防火墙0FIRE_PAUSED → 防火墙1已持仓 → 防火墙2ask1合法 → 手数计算 → 下单
```

**验证**：`py_compile hawkes_executor.py` → **Exit 0**

---

### 变更文件：`hawkes_executor.py`（盘中超买事故：账本重建）

**[BUG] 根因：多次 kill-restart 导致 `on_order_trade` 回调丢失**

09:52~09:53 间，进程因调试被多次 kill，每次重启时 `hawkes_holdings.json` 为空 → 持仓锁（彼时不存在）无法拦截 → Hawkes 对同一标的连续开火 20+ 次，累计建仓：
- 513120.SH 37500手 / 513090.SH 48600手 / 159892.SZ 24200手
- 513300.SH 4300手 / 513880.SH 5300手 / 513050.SH 7900手

**[FIX] 紧急账本重建（物理持仓 → hawkes_holdings.json）**：
通过 `query_stock_positions` 读物理真相，重建账本，让 Micro-Exit 接管止盈止损监控。

**已安装两道物理保险，此类事故不会再发生。**

---

### 变更文件：`hawkes_executor.py`（盘中崩溃修复：AttributeError）

**[BUG] `xtconstant.STOCK_TYPE` 不存在 → 启动即崩**

该版本 xtquant 已移除 `STOCK_TYPE` 常量，正确模式为 `StockAccount` 对象传入 `subscribe()`（对齐 `t0_multigrid_executor.py`）：

```python
# ❌ 旧（崩溃）
ACCT_TYPE = xtconstant.STOCK_TYPE        # AttributeError
_acc = _xt_trader.subscribe(ACCOUNT_ID)  # 返回 None

# ✅ 新（对齐 T0 引擎写法）
from xtquant.xttype import StockAccount
_acc = StockAccount(ACCOUNT_ID)
sub_result = _xt_trader.subscribe(_acc)
if sub_result != 0: ...   # 非零即失败
```

**[OPS] 盘中 09:46 手动拉起验证**：进程 PID=4352，`20260417_hawkes_live.log` = 609 字节，白名单校验通过（7只全部在 T+0 池），QMT 握手中。

**[BUG] `XtQuantTrader.__init__()` got an unexpected keyword argument `session_id`**

该版本 xtquant 的 `XtQuantTrader` 接受位置参数而非关键字参数，对齐 `t0_multigrid_executor.py` 写法：

```python
# ❌ 旧（崩溃）
_xt_trader = XtQuantTrader(QMT_PATH, session_id=1188)

# ✅ 新（位置参数 + 动态 session_id 防冲突）
_xt_trader = XtQuantTrader(QMT_PATH, int(time.time()))
```

**[OPS] 09:53 盘中验证通过**：QMT 连接成功 → 7只 Tick 全部订阅 → 513120.SH λ=87.793 触发首炮，FIRE 7500手 @ ask1=1.3300，capital≈9975元。策略正式进入实盘运行模式。

---

### 变更文件：`hawkes_executor.py`（日志架构修复）

**[BUG] 根因：`logging.basicConfig()` 单例静默失效**

| 症状 | 原因 |
|---|---|
| `20260416_hawkes_live.log` 大小 = 0 | 文件被创建但进程立刻退出或日志被吞 |
| `20260417_hawkes_live.log` 不存在 | 今天从未被 AutoPilot 调度启动 |

**根本原因**：`basicConfig()` 在同一 Python 进程中只有第一次调用生效（Python logging 单例机制）。`autopilot_master.py` 在进程启动时已调用 `basicConfig()`（第 110 行），若 hawkes 作为子进程被 Popen 拉起时共享同一解释器环境，`basicConfig` 调用静默无效，所有 `_logger.info()` 被 root logger 吞掉，导致日志文件为空。

**[FIX] 修复：独立 logger + 专属 FileHandler + propagate=False**

```python
# ❌ 旧代码（单例陷阱）
logging.basicConfig(handlers=[FileHandler(...), StreamHandler(...)], ...)
_logger = logging.getLogger("hawk")

# ✅ 新代码（独立专属 Handler，绕开 basicConfig 单例限制）
_logger = logging.getLogger("hawk")
_logger.setLevel(logging.INFO)
if not _logger.handlers:   # 防止看门狗重启时重复添加 Handler
    _fh = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    _sh = logging.StreamHandler(sys.stdout)
    _logger.addHandler(_fh)
    _logger.addHandler(_sh)
_logger.propagate = False   # 不传播到 root logger，防止重复输出
```

**验证**：`py_compile hawkes_executor.py` → **Exit 0**

---

### 变更文件：`strategy_registry.yaml`（Hawkes 接入 AutoPilot 调度）

**[FEAT] Hawkes 刺客执行器正式注册到 morning_strategies**

旧状态：`hawkes_executor.py` **从未在 strategy_registry.yaml 中注册**，AutoPilot 的 `task_morning()` 永远找不到它，导致进程从未被拉起。

新增条目（位于 `morning_strategies` 末尾，`胖鱼波段防线` 之后）：

```yaml
- name: "Hawkes 刺客执行器"
  script: "hawkes_executor.py"
  enabled: true
  watchdog: true      # 看门狗保护：Tick 桥接进程崩溃后自动复活
  description: "Hawkes 点过程大单流涌现探测，10000元侦察弹追击，+15元止盈/-30元止损"
```

启动时间：`task_morning()` → `09:22` 触发，与 T0、Sniper、胖鱼防线同批拉起，全天看门狗守护。

---

## 📅 2026-04-16 — Momentum 管道：trade_rule 注入 + N8N 接入

### 变更文件：`momentum_master.py`（补丁）

**[FEAT] 输出 JSON 新增 `trade_rule` + `name` 字段**
- 在步骤 4（逐标的评分）结束后，查询 `t0_absolute_pool.csv` 白名单写入 `trade_rule: "T+0"/"T+1"`
- 同时调用 `get_instrument_detail` 写入 ETF 中文名 `name`（供下游日志/N8N 可读）
- 日志展示字段新增交易制度（如 `交易制度: T+0`）

**[FEAT] N8N 推送接入（策略三件套到位）**
- 扫描成功：`🏆 动量司令部 扫描完毕 TOP3`，消息体含 🟢T+0/🟡T+1 标识 + M-Score + RSI + ATR + 成交额
- 市场极冷（无满足标的）：`🚨 动量司令部 市场极冷告警`

**[ARCH] `_load_t0_set()` 函数**：参照 `quant-v4-patterns §12.5` 铁律，utf-8-sig 读取，跳过非9位代码行

**验证**：`py_compile momentum_master.py` → **Exit 0**

---

### 变更文件：`momentum_vector_executor.py`（重构 T+0 判断逻辑）

**[ARCH] 废弃运行时查白名单，改为读 `trade_rule` 字段（架构下沉）**

| 旧架构 | 新架构 |
|---|---|
| Executor 启动时自行加载 `t0_absolute_pool.csv` | Master 扫描时统一写入 `trade_rule` 到 `momentum_slots.json` |
| `is_t0_eligible(code)` 每次查内存集合 | 直接读 `slot["trade_rule"]` / `info["trade_rule"]` |
| 持仓账本字段 `t0_eligible: bool` | 持仓账本字段 `trade_rule: "T+0"/"T+1"` |

**[ARCH] 数据流向**
```
momentum_master.py 扫描时
  → 查 t0_absolute_pool.csv
  → 写入 momentum_slots.json["trade_rule"]
  → (入场)写入 _PENDING["trade_rule"]
  → (成交)写入 momentum_holdings.json["trade_rule"]
  → _is_t1_locked() 直接读取，无需再查任何文件
```

**[FEAT] 入场日志新增 `[T+0]`/`[T+1]` 标识**：便于快速识别制度

**验证**：`py_compile momentum_vector_executor.py` → **Exit 0**

## 📅 2026-04-16 — 右侧动量向量执行器 momentum_vector_executor.py 新建

### 变更文件：`momentum_vector_executor.py`（新建）

**[FEAT] 右侧动量向量执行器上线（momentum_master.py 的下游执行层）**

| 模块 | 说明 |
|---|---|
| 上游 | `momentum_slots.json`（TOP-3 ETF 候选，含 M-Score）|
| 账本 | `momentum_holdings.json`（独立账本，不查 QMT 全局持仓）|
| HWM | `momentum_hwm.json`（每只持仓的最高水位线）|
| T+1信号 | `momentum_t1_signals.json`（次日集合竞价执行队列）|
| 遥测 | `YYYYMMDD_momentum_telemetry.csv`（6种事件类型）|

**[ARCH] 入场协议（鱼身捕捉窗口）**
- 开火窗口：09:30 ~ 10:30（鱼身）；14:30 后绝对不开新仓
- 双重确认：① 最新价 > 分时 VWAP；② 日内涨幅 > 1%
- 下单方式：ask1 扫单（FIX_PRICE，保证快速成交）
- 发单后只注册 `_PENDING`，不动账本（铁律一）

**[ARCH] 退出协议（三层防线）**
- 移动止盈：现价 < HWM × 96% → 立即清仓（趋势终结信号）
- 硬止损：现价 < 成本价 × 95% → 无条件清仓
- T+0/T+1 分叉处理：
  - T+0（白名单内）→ 直接 `order_stock` 卖出
  - T+1 当日触发 → 记录 `T1_SIGNALS`，次日 09:25 集合竞价市价执行

**[PATTERN] T+0 白名单查表铁律（quant-v4-patterns §12.5 新增）**

```python
# 永远通过读 t0_absolute_pool.csv 白名单判断 T+0 资格
# 永不使用代码前缀推导（如 513xxx → T+0），永不猜测
def is_t0_eligible(code: str) -> bool:
    _load_t0_pool()   # 从 .state/t0_absolute_pool.csv 加载，模块级缓存
    return code in _T0_POOL
```

> 根因：代码前缀规则经常有例外（如同一前缀下 T+0 与 T+1 并存），
> 只有交易所官方白名单 `t0_absolute_pool.csv` 才是物理真相。

**[ARCH] 四大铁律合规检查**
- 铁律一：发单注册 `_PENDING`，仅 `on_stock_trade` 回调写账本 ✅
- 铁律二：入场/退出只读 `momentum_holdings.json`，不查 QMT 全局持仓 ✅（退出时物理查仓仅用于确认可卖量，不用于决策）
- 铁律三：卖出数量来自账本 `qty`（物理查仓确认），非按现价重算 ✅
- 铁律四：`_IO_LOCK`（账本读写）+ `_PENDING_LOCK`（订单注册）双锁保护 ✅

**[OPS] Pending 超时 Sweeper**：30s 扫描一次，超时委托物理查成交量补录，剩余未成则撤单释放资金

**验证**：`py_compile momentum_vector_executor.py` → **Exit 0**

## 📅 2026-04-16 — 截面动量司令部 momentum_master.py 创建 & N8N 推送规范固化

### 变更文件：`momentum_master.py`（新建）

**[FEAT] 截面动量雷达（Cross-Sectional Momentum Radar）上线**

| 模块 | 说明 |
|---|---|
| 输入 | `.state/oracle_v2_universe.json`（精英 ETF 候选池）+ 本地 QMT 日线缓存 |
| 算法 | M-Score = 线性回归斜率 / 年化波动率（截面夏普比率近似，无主观指标） |
| 输出 | `.state/momentum_slots.json`（供下游 Executor 消费） |
| 触发时机 | 每日盘后（15:30+）或盘前（09:00前）一次性运行 |

**[ARCH] 三道防线设计（顺序不可颠倒）**

| 防线 | 手段 | 参数 |
|---|---|---|
| 第一道 | 流动性底线（利用 universe 中的 avg_amount_5d） | ≥ 5亿 |
| 第二道 | 正斜率过滤（slope > 0 才进入赛圈，拒绝下行趋势） | slope > 0 |
| 第三道 | RSI 鱼尾过滤（拒绝极度超买接盘，盘中高位追买） | RSI < 75 |
| 最终产出 | M-Score 降序排名，取 TOP_N = 3 只 | 最强前 3 名 |

**[ARCH] 核心算法（`calculate_momentum_score`）**
```python
# 1. 归一化：(price - price[0]) / price[0]  → 跨品种公平比较
# 2. 斜率：scipy linregress(x, normalized)  → 趋势强度
# 3. 波动率：std(daily_ret) × sqrt(252)     → 风险惩罚
# 4. M-Score = slope / vol                  → 截面夏普近似
# 5. RSI(14)：常规公式，gains/losses rolling 14天
```

**[OPS] 关键配置（全部集中于模块头部「配置区」）**
- `LOOKBACK_DAYS = 250`（约1年，与 qmt_daily_sync 对齐）
- `WINDOW = 20`（动量计算窗口，约1个月）
- `TOP_N = 3`（只取最强前 3 名）

**[OPS] 落盘规范**：`tmp → os.replace()` 原子替换（防并发写损坏，符合铁律四）

**[PATTERN] 截面动量管道（Cross-Sectional Momentum Pipeline）**
> 见 quant-v4-patterns §12「截面动量司令部标准模式」

**验证**：`py_compile momentum_master.py` → **Exit 0**（2026-04-16 16:13 落盘确认）

---

### [OPS] N8N 推送为策略基本要素固化

**[PATTERN] N8N Webhook 推送是所有策略模块的必备组件**，与遥测 CSV、日志文件并列为「策略三件套」。

> 见 quant-v4-patterns §13「N8N Webhook 推送标准规范」

**已实盘验证推送节点的策略模块**：

| 策略 | 推送节点 | 文件 |
|---|---|---|
| ETF_OU_Grid Master | 席位解算成功 / 失败 | `etf_ou_grid_master.py` |
| ETF_OU_Grid Executor | 系统熔断/时间止损/止盈/爆破/首网/加仓（6个节点） | `etf_ou_grid_executor.py` |
| Macro Rotation | 有行动（换仓/止损）才推，无变化不推（防刷屏） | `macro_rotation_executor.py` |
| Sniper | 超时委托撤单告警（N8N 是最后兜底通知） | `sniper_entry_executor.py` |

**❌ momentum_master.py 尚未接入 N8N 推送** — 待下次改版补入：
- 推送节点：`🏆 动量扫描完毕`（含 TOP3 代码、M-Score）
- 静默情况：市场极冷（无满足底线标的）时也推一次告警

## 📅 2026-04-16 — Hawkes 刺客系统 V2 实盘版

### 变更文件：`sniper_entry_executor.py` + `sniper_exit_guard.py`（遥测对齐补丁）

**[FEAT] Sniper 四路遥测，对齐 Hawkes 设计标准**

| event_type | 写入位置 | 核心判据 |
|---|---|---|
| `SIGNAL_DETECTED` | `execute_sniper_entry`：`seq > 0` 发单成功即写 | 逼近率、动量分、ask1、下单价 |
| `POSITION_OPENED` | `on_stock_trade`：全量成交后写（`total_qty >= ordered_qty`） | VWAP 均价、逼近率、动量分（从 pending 携带） |
| `HOLDING_LOG` | `sniper_exit_guard` while True 末尾，每 300 秒写一次 | 现价、入场均价、浮动盈亏 % |
| `POSITION_CLOSED` | `_write_sniper_telemetry`（既有 Auditor，含 MFE/MAE 计算）| 出场价、原因、pnl %、MFE/MAE|

**[ARCH] 实现细节**
- `_write_sniper_telem()` 写入 `.state/sniper_telemetry.csv`（与 exit_guard 共享同一文件）
- `PENDING_ORDERS[seq]` 新增 `approach_rate`/`momentum_score` 字段供回调读取
- `_holding_log_last` 计时器：`time.time()` 比较，避免引入独立线程
- 完全不阻断主逻辑，所有写入异常静默 catch

**验证**：`py_compile sniper_entry_executor.py` → Exit 0；`py_compile sniper_exit_guard.py` → Exit 0

### 变更文件：`hawkes_executor.py`（遥测三路系统补丁）

**[FEAT] 三路遥测日志 → `YYYYMMDD_hawkes_telemetry.csv`**

| event_type | 触发时机 | 核心字段 |
|---|---|---|
| `SIGNAL_DETECTED` | Hawkes 引擎开火**前一刻** | λ强度、单笔手数、ask1/bid1、主动方向 |
| `POSITION_OPENED` | QMT `on_order_trade` 成交回调 | VWAP 入场价、λ（来自 pending）、持仓手数 |
| `HOLDING_LOG` | 每 5 分钟后台轮播 | 现价、浮动盈亏（元）、持仓手数 |
| `POSITION_CLOSED` | 卖出成交回调（先记录再清账） | 成交价、入场均价、最终实现盈亏 |

**[ARCH] 关键设计细节**
- `_TELEM_LOCK`：CSV 文件写入线程安全（与 pending/holdings 完全独立）
- λ 通过 `_hawk_pending[seq]["lam"]` 携带到回调，无额外全局变量
- 卖出路径：**先写 POSITION_CLOSED → 再清账本**，避免复盘时数据为空
- `HoldingLog` 线程为 daemon，主线程退出时自动结束

**[OPS] 复盘分析示例**
```
时间         entry_price  5min后现价  pnl    结论
09:32:01  →  1.2340      1.2290     -50    买点即被套（入场前信号不稳）
09:47:00  →  1.2290      1.2380     +90    盈利后续跌到止损线 → 卖点迟钝
```

**验证**：`py_compile hawkes_executor.py` → **Exit 0**


**[FEAT] 从探测器升级为完整实盘执行器**

| 模块 | 说明 |
|---|---|
| `HawkTraderCallback.on_order_trade` | Fill-Based 入账（铁律一：禁止乐观更新）|
| `_execute_fire()` | 开火 → `order_stock(BUY, FIX_PRICE, ask1)` → 注册 `_hawk_pending` |
| `_micro_exit_monitor()` | 守护线程，1秒扫描，触发止盈/止损 |
| `_execute_exit()` | 物理查仓 `query_stock_positions` → 全额市价卖（铁律三）|
| `_validate_whitelist()` | 启动前校验 `HAWK_CODES` 全部在 `t0_absolute_pool.csv` 中 |

**[ARCH] 四大铁律合规**：
- 铁律一：发单注册 `_hawk_pending`，仅 `on_order_trade` 回调写账本
- 铁律二：持仓决策只读 `hawkes_holdings.json`，不查 QMT 全局持仓
- 铁律三：退出时物理查仓 `query_stock_positions`，全额卖出
- 铁律四：`_hawk_pending_lock + _holdings_lock` 双锁保护

**[OPS] 关键参数**：
- 开火资金：10,000 元（侦察弹）
- 止盈：账面盈利 ≥ +15 元 → 立即市价清仓
- 止损：账面亏损 ≤ -30 元 → 立即物理斩首
- `HAWK_CODES`：用户自行填写（留空占位，防止系统自主决策标的）

**[OPS] 架构决策回顾**：
- 防火墙联动改动（T0/T1/Rotation）已按用户指示回滚，因为：
  1. Hawkes 白名单（跨境/商品 ETF）与 T0/Rotation 标的不重叠，无踩踏风险
  2. T1 已取消，修改无意义
- 后续如白名单有标的与其他引擎重叠，手动同步即可

**验证**：`py_compile hawkes_executor.py` → **Exit 0**

## 📅 2026-04-16 — Hawkes Engine v2：平方根法则 + 做市商过滤网

### 变更文件：`hawkes_engine.py`

**[ARCH] 核心物理手术：废弃线性累加，实施 Square Root Law of Market Impact**

**问题根因**（实盘压测发现）：
- `513120.SH` 出现 87121 手单笔成交，线性公式下 `Δλ = 87121 × α = 极大值`
- 即使将 α 调小（Curve-fitting），代入同样量级的单笔仍会炸穿任何合理阈值
- A 股 ETF 微观结构中，数万手单笔极大概率是**做市商内部对倒 / ETF 一二级申赎延时播报**，不吃盘口流动性，不产生 FOMO 余震——将此类数据计入 Hawkes 动能是致命污染

**两项架构级修复**：

| 修复 | 旧版 | 新版 |
|---|---|---|
| 极端胖尾压缩 | `decay_sum += volume`（线性） | `decay_sum += sqrt(volume)`（平方根法则）|
| 机构对倒拦截 | 无 | `whale_cap_limit=20000`，超过直接物理拒绝 |

**参数更新**（数学推导结果，非 Curve-fitting）：

| 参数 | 旧值 | 新值 | 原因 |
|---|---|---|---|
| `alpha` | 0.1 | **1.5** | sqrt 压缩后量纲变小，需放大才有灵敏度 |
| `beta` | 0.5 | **1.2** | 实测 ETF 余震周期短，需更快衰减 |
| `trigger_level` | 15.0 | **25.0** | 匹配新量纲下的合理阈值 |
| `whale_cap_limit` | 无 | **20000** | 做市商过滤网上限（手） |

**靶场验证结果**：
- 场景一：1200 手连续扫单 → λ=52.9 → 🚨 正确开火
- 场景一：15s 后 400 手散单 → λ=3.0 → 正确蛰伏（衰减到位）
- 场景二：87121 手巨单 × 2 → `[WHALE拒绝]` λ=1.0 不变 → 完全隔离
- 场景二：随后 1500 手真实大单 → λ=59.1 → 正常开火

**验证**：`py_compile hawkes_engine.py` → **Exit 0**；靶场两场景全部通过

## 📅 2026-04-16 — Hawkes 刺客系统：Tick 桥接器新建

### 变更文件：`hawkes_engine.py`（用户新建）、`hawkes_executor.py`（新建）

**[FEAT] FastHawkesEngine + Tick 桥接器上线**

#### `hawkes_engine.py`（用户编写，只读）
- O(1) 指数衰减递归算法，`process_tick(timestamp_sec, price, volume, buy_flag)` 接口
- `buy_flag=1`（主动买）且 `volume >= vol_threshold` 才注入动能；其余 tick 只计算自然衰减
- 返回 `{"fire": bool, "lambda": float, "price": float}`

#### `hawkes_executor.py`（新建）

| 模块 | 实现方式 |
|---|---|
| 行情订阅 | `xtdata.subscribe_quote(code, period='tick', callback=on_tick)` 独立回调/标的 |
| 闭包绑定 | `_make_tick_callback(code)` 工厂函数 + 默认参数 `_code=code`，规避晚绑定陷阱 |
| 时间戳转换 | tick `time`(ms) ÷ 1000 → 秒级浮点；QMT 不给时 fallback `time.time()` |
| 增量成交量 | `cum_volume - _last_volume[code]`，开盘激增/重连回退防御（`delta <= 0` → skip） |
| 主动方向判断 | `lastPrice >= ask1` → `buy_flag=+1`；`lastPrice <= bid1` → `-1`；盘口中间 → `0` |
| 开火响应 | `print()` 模拟，ANSI 红色加粗警报行，严禁 IO/requests |

**[OPS] 参数选择（测试版 ETF 池）**
- 标的：`159518.SZ / 159941.SZ / 512890.SH`（T+0 高流动性 ETF）
- 大单门槛：200 手（低于股票，匹配 ETF 流动性特征）
- 触发阈值：λ ≥ 12.0

**[BUG] 修复：subscribe_quote tick 回调数据格式**
- **发现**：实测 `subscribe_quote(period='tick', callback=fn)` 回调时，`data[code]` 是 **`list`**（最新 tick 在末尾），而非 `dict`
- **对比**：`get_full_tick()` 返回的 `dict` 是单个对象，两者格式不同
- **关键字段确认（通过 `get_full_tick` 探针）**：
  - `volume`：当日累计成交量（手）
  - `pvolume`：上一 tick 累计量
  - `askPrice/bidPrice`：五档列表
  - `time`：毫秒时间戳
- **修复**：在 `on_tick` 回调中改为 `raw = data.get(_code); tick = raw[-1] if isinstance(raw, list) else raw`

**[OPS] 监控标的更换**（用户调整）：
- 旧：`159518.SZ / 159941.SZ / 512890.SH`
- 新：`518680.SH（黄金ETF） / 159792.SZ / 513120.SH`

**[OPS] 下午 13:00 进入实盘 Tick 测试模式**
- 进程 PID 已后台驻留，等待开盘 Tick 自动流入
- 开火报警：红色加粗 `[Hawkes 刺客] {code} 脉冲击穿阈值！`

**验证**：`py_compile hawkes_executor.py` → **Exit 0**

## 📅 2026-04-15 — ETF_OU_Grid Master + Executor：N8N 全链路推送

### 变更文件：`etf_ou_grid_master.py`、`etf_ou_grid_executor.py`

**[FEAT] N8N Webhook 全链路覆盖**（参照系统统一规范）

#### 两文件共同新增（模式完全一致）
```python
from dotenv import load_dotenv
try:
    import requests; _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False
load_dotenv()
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")

def send_webhook(title, message):  # 失败静默，timeout=5
    ...
```

#### `etf_ou_grid_master.py` 推送节点

| 场景 | 标题 |
|---|---|
| 席位解算成功 | `🛸 ETF_OU_Grid 席位已解算` |
| 无席位入选 | `🚨 ETF_OU_Grid 席位解算失败` |

消息体包含：席位数、每席位 code/名称/效率/步长。

#### `etf_ou_grid_executor.py` 推送节点（6 个）

| 事件 | 标题 | 推送条件 |
|---|---|---|
| 系统熔断 | `🚨 系统级熔断` | `total >= 200,000`，每轮一次 |
| 时间止损 | `💀 时间止损触发` | `days_held > halflife×2` |
| 浅水区止盈 | `💰 浅水区止盈` | `profit >= step`（grid≤2）|
| 深水区爆破 | `🌋 均值回归爆破` | `reversion >= -0.5%`（grid>2）|
| 首网开仓 | `🌊 首网开仓` | 首格买入触发 |
| 深潜加仓 | `⚓ 深潜加仓` | 第 N 格买入触发 |

**静默节点（不推送，防刷屏）**：`HOLD_SHALLOW` / `HOLD_DEEP` / `LOCKED_T1` / `MELTDOWN_REJECT`（后者已由熔断大推送覆盖）

**验证**：`py_compile executor + master` → **双 Exit 0**

## 📅 2026-04-15 — ETF_OU_Grid Master：本地数据读取优化 + 时间戳校验

### 变更文件：`etf_ou_grid_master.py`

**[PERF] 移除 `download_history_data2` 网络拉取**：
- 旧：`xtdata.download_history_data2(candidates, period='1d', ...)` → 盘前与 QMT 服务器争抢带宽
- 新：直接 `xtdata.get_market_data_ex(...)` 读本地 QMT 缓存目录（`datadir` 已由 `qmt_daily_sync.py` 在 15:30 同步完毕）
- 结果完全一致，消除了网络超时风险

**[FEAT] `_last_trading_day_str()`**：
- 跳过周末（周六→周五，周日→周五）推算最近交易日
- 用于 `end_time` 参数（避免请求未来日期），以及新鲜度核对基准

**[FEAT] `_validate_local_data(market_data, candidates)`**：
- 遍历所有标的，取 DataFrame 最后一行索引（兼容 int ms 时间戳 和 str 格式）
- 与 `_last_trading_day_str()` 比对：落后则打印 `⚠️ [本地数据老旧告警] `
- **仅告警，不中断**：盘前 09:20 触发时即便数据是昨日的也能继续解算

**[CHANGE] 回看窗口 `LOCAL_LOOKBACK_DAYS = 250`**（原 180 天），与 `qmt_daily_sync.py` 保持一致

**盘前运行时序保障**：
```
15:30 qmt_daily_sync.py → 落盘当日全宇宙日线
次日 09:20 etf_ou_grid_master.py → 直接读本地，_validate 确认最新
次日 09:30 etf_ou_grid_executor.py → 使用昨晚写好的 etf_grid_slots.json
```

**验证**：`py_compile` → **Exit 0**

## 📅 2026-04-15/16 — T1 Grid 退役 / ETF_OU_Grid 正式接替（战略切换）

### 变更文件：`autopilot_master.py`、`t1_grid_executor.py`

**[ARCH] 战略切换：T1 Grid → ETF_OU_Grid**

| 维度 | 旧（T1 Grid） | 新（ETF_OU_Grid） |
|---|---|---|
| 选股逻辑 | 人工维护 YAML 白名单 | 动态宇宙 → OU 半衰期 + Hurst 筛选 + 同质化互斥锁 |
| 资金颗粒度 | 5,000/格，20,000/标的 | 15,000/格，200,000 总熔断墙 |
| 风控维度 | 空间熔断 + 时间衰减 | T+1锁 + 时间止损(2×halflife) + 系统熔断 |
| 遥测 | 无 | 9 个探针 → `oracle_telemetry_grid.csv` |

**[OPS] autopilot_master.py 时间轴变更**：

| 时间 | 旧任务 | 新任务 |
|---|---|---|
| 09:20 | ~~09:22 T1_Master（阻塞核算）~~ | ✅ ETF_OU_Grid Master（阻塞解算席位） |
| 09:30 | ~~09:30 T1_Grid_Executor（守护进程）~~ | ✅ ETF_OU_Grid Executor（守护进程） |

**[OPS] 路径常量新增**：
```python
ETF_OU_GRID_MASTER   = _DIR / "etf_ou_grid_master.py"
ETF_OU_GRID_EXECUTOR = _DIR / "etf_ou_grid_executor.py"
```

**[OPS] T1 Grid 安全关闭 SOP**：
1. 2026-04-16 开盘前手动运行 `t1_emergency_reset.py --dry-run` 确认清仓量
2. 确认无误后运行 `t1_emergency_reset.py --live`，清仓所有 T1 存量持仓
3. T1 账本写入 `grid=0 / avail=0`，autopilot 不再调度 T1 任何脚本

**[OPS] t1_grid_executor.py 头部打退役公告**：明确禁止重新启动，保留源码历史追溯。

**验证**：`py_compile autopilot + t1_exec` → **双 Exit 0**

## 📅 2026-04-15 — ETF_OU_Grid Executor v3：总资金墙 + 时间止损

### 变更文件：`etf_ou_grid_executor.py`（v3）、`etf_ou_grid_master.py`（补字段）

**[FEAT] 总仓位熔断保险丝（MAX_SYSTEM_CAPITAL = 200,000）**：
- 遍历 slots 之前先计算全局已部署资金 = `格数 × CAPITAL_PER_GRID`
- `total_deployed_capital >= 200,000` → `is_meltdown = True`
- 后续所有 BUY_FIRST / BUY_NEXT 被 `MELTDOWN_REJECT` 探针拦截，不发单

**[FEAT] 时间止损斩仓（TIME_STOP_MULTIPLIER = 2.0）**：
- 平仓循环优先级最高（在 T+1 锁之后、Hybrid Protocol 之前）
- `days_held > halflife_days × 2.0` → 打印 `💀 [时间止损]`，`sold = True`
- 遥测：`TIME_STOP` 探针，含持仓天数信息

**[FEAT] `halflife_days` 字段注入 Master**：
- `etf_ou_grid_master.py` 的 `qualified_assets.append()` 新增 `halflife_days` 字段
- 来源：`cir_params['rough_halflife_days']`（已含 Hurst 粗糙度惩罚的修正半衰期）
- Executor 通过 `slot.get('halflife_days', 5.0)` 读取，默认 5 天保守兜底

**[CHANGE] 单格资金重命名与调整**：`GRID_CASH=20,000` → `CAPITAL_PER_GRID=15,000`

**新增遥测探针（共 9 个）**：

| 探针 | 触发时机 |
|---|---|
| `TIME_STOP` | 持仓超过 `halflife × 2` 天，强行斩仓 |
| `MELTDOWN_REJECT` | 系统总仓位 ≥ 20 万，拒绝新开/加仓 |

**验证**：`py_compile executor + master` → **双 Exit 0**

## 📅 2026-04-15 — ETF_OU_Grid Master 三连修 + 同质化互斥锁

### 变更文件：`etf_ou_grid_master.py`

**[BUG] CRITICAL-5 / _load_t0_set**：文件是 `代码,名称` CSV，但用 `json.load()` 打开 → `JSONDecodeError`
修复：改用 `csv.reader`，逐行取首列，`utf-8-sig` 编码支持 BOM；自动跳过表头行

**[BUG] CRITICAL-5 / get_instrument_detail**：传入 `is_dict=True` 关键字参数不存在 → `TypeError`
修复：循环内逐只调用，降级时从 `universe.json` 的 `name` 字段兜底

**[BUG] HIGH-2 / get_market_data_ex**：第一个位置参数传了 `candidates`（代码列表），被当作 `field_list` → 返回空数据
修复：`field_list=['close','open','high','low','volume']` + `stock_list=candidates`（关键字传参）

**[FEAT] 同质化互斥锁（Homogeneity Mutex Lock）**：

实测发现 3 席位中：席位1=`159518 标普油气ETF嘉实`，席位3=`513350 标普油气ETF富国`——两只同为标普油气，高度同质化，失去分散意义。

互斥锁逻辑：
1. 按效率降序遍历 `qualified_assets`
2. `_extract_core_class(name)` 提取核心基因关键词（`油气/纳指/标普/黄金/半导体/...` 共 21 类）
3. 命中 `seen_classes` → 打印 🚫 拦截，`continue`
4. 未命中 → 打印 ✅ 放行，记录基因并入席

**[PATTERN] 关键词无命中时以全名为键**：保证名称完全不同的标的不会意外互斥（两种完全不同的主题 ETF 各取效率最高的一只）

**验证**：`py_compile` → **Exit 0**

## 📅 2026-04-15 — ETF_OU_Grid 静默手术：Hurst NaN装甲 + Executor v2全量探针

### 变更文件：`hurst_engine.py`、`etf_ou_grid_executor.py`

**[BUG] CRITICAL-4 / hurst_engine.py**：A 股一字跌停 diff全0 → var=0 → log(0)=-inf → NaN > 0.45 = False → 死水标的穿透防御网。修复：`if variance == 0: variance = 1e-8`

**[BUG] CRITICAL-1 / executor**：T+1锁代码游离模块级 → NameError启动崩。修复：整体移入函数体。

**[BUG] CRITICAL-2 / executor**：买入不写 `buy_date` → 历史持仓永久锁死。修复：抽屉盒模式，每次买入同步写 `buy_date` + `trade_rule`。

**[FIX] HIGH-3 / executor**：账本写回改为 `tmp → os.replace()` 原子操作，防双进程腐蚀。

**[FEAT]** 埋入 7 个遥测探针 → `oracle_telemetry_grid.csv`（utf-8-sig）：`LOCKED_T1 / HOLD_SHALLOW / SELL_SHALLOW / HOLD_DEEP / SELL_DEEP / BUY_FIRST / BUY_NEXT`

**验证**：`py_compile` 双 Exit 0

## 📅 2026-04-15 — Round 1 候选池扩容 60 → 150（保障最终 60 席到位）

### 变更文件：`tools/refine_core_universe.py`

**[BUG] 根因**：`top_n=60` 时 Round 1 取前 60 只 ETF，其中约 50% 是债券/货币 ETF（日波动 < 0.5%），被 Round 3 死水过滤器淘汰，导致最终只有 30 席而非目标 60 席。

**[FIX]** `top_n: 60 → 200`（用户要求 200，比 150 有更大缓冲）

| 轮次 | 旧 | 新 |
|---|---|---|
| Round 1 候选池 | Top 60 | **Top 150** |
| Round 3 预期通过 | ~30（实测） | ~60（目标） |
| `final_seats` | 60（不变） | 60（不变） |

**设计原理**：流动性 Top 150 中债券/货币 ETF 占比约 40-50%（经实测），保留 150 只候选才能确保过滤后剩余 ≥ 60 只权益/跨境 ETF 填满席位。

**验证**：`py_compile` → **Exit 0**

## 📅 2026-04-15 — 遥测 CSV 历史数据原地修复 + 迁移脚本

### 变更文件：`tools/migrate_telemetry_csv.py`（新建）、`.state/oracle_telemetry_macro.csv`（数据修复）

**[BUG] 深层根因**：旧版 `_write_telemetry` 以 `utf-8` 无 BOM 追加写入时，Windows 终端的 `sys.stdout` 是 GBK 编码。由于 `csv.writer` 直接调用 `f.write()`，而文件打开时的 `encoding='utf-8'`，Python 正确写入了 UTF-8 字节。**但是**，历史上某些时刻代码用的是 `encoding` 与实际写入字节不一致的路径（GBK 终端 print 被重定到文件），导致 `持有/熔断卖出` 等中文值以 GBK 字节写入后再被 UTF-8 误读，出现 `鎸佹湁/鐔旀柇鍗栧嚭` 乱码。

**[FIX] 两步修复**：

| 步骤 | 操作 | 结果 |
|---|---|---|
| 1 | `migrate_telemetry_csv.py` 读取旧 CSV → 插入 `名称` 列 → utf-8-sig 写回 | 44 行历史保留，名称列补全 |
| 2 | 内联脚本扫描 Action 乱码 → 替换回正确中文 | `鎸佹湁` → `持有`，`鐔旀柇鍗栧嚭` → `熔断卖出` |

**[FEAT] migrate_telemetry_csv.py 能力**：
- 自动尝试 `utf-8-sig / utf-8 / gbk / gb18030` 四种编码读取
- 检测是否已有 `名称` 列（幂等，多次运行安全）
- 原地备份为 `oracle_telemetry_macro.bak.csv` 再写回
- 从 xtdata 批量查询唯一标的名称

**[OPS] 当前 CSV 状态（修复后）**：
- 行数：44 行（全量保留）
- 编码：`utf-8-sig`（BOM头，Excel/WPS 直接打开不乱码）
- 列：`Timestamp, Code, 名称, P0, Q20, Q50, Q80, Odds_Ratio, Action`

## 📅 2026-04-15 — oracle_telemetry_macro.csv 乱码修复 + 名称列

### 变更文件：`macro_rotation_executor.py`、`macro_risk_monitor.py`

**[BUG] 根因**：两个文件的 `_write_telemetry` 均以 `encoding='utf-8'` 写 CSV。Windows 的 Excel 用系统码页（GBK）打开 UTF-8 无 BOM 文件时，中文 Action 字段（`持有`/`熔断卖出`/`空仓`等）全部显示乱码。

**[FIX] 修复**：
- `encoding='utf-8'` → `encoding='utf-8-sig'`（带 BOM 头，Excel 自动识别 UTF-8）
- 新增 **`名称` 列**（列位于 `Code` 之后），通过 `get_instrument_detail` 查询 ETF 中文简称

**[ARCH] 共用辅助函数**：两个文件各自新增 `_name_cache: dict` + `_get_name(code)` 模块级函数，查询结果内存缓存，同一进程中不重复请求。

**CSV 新字段顺序**：

| 旧 | 新 |
|---|---|
| `Timestamp, Code, P0, Q20, Q50, Q80, Odds_Ratio, (Momentum_Pass,) Action` | `Timestamp, Code, **名称**, P0, Q20, Q50, Q80, Odds_Ratio, (Momentum_Pass,) Action` |

> [!IMPORTANT]
> 旧 CSV 文件如仍需正确显示中文，建议用文本编辑器另存为 UTF-8 with BOM，或用 Python `pandas.read_csv(..., encoding='utf-8')` 读取（程序侧无影响）。

**验证**：
- `py_compile macro_risk_monitor.py` → **Exit 0**
- `py_compile macro_rotation_executor.py` → **Exit 0**

## 📅 2026-04-15 — 最终宇宙扩容至 60 席 + 同步输出 CSV

### 变更文件：`tools/refine_core_universe.py`

**[FEAT] 双重调整：席位扩容 + 人类可读 CSV 落盘**

| 项目 | 旧 | 新 |
|---|---|---|
| `final_seats` 默认值 | 40 | **60** |
| 最终宇宙输出文件 | 仅 JSON | JSON + **同名 CSV** |
| CSV 额外字段 | — | `名称`（中文简称） |

**CSV 字段完整清单**：`排名 / 代码 / 名称 / 现价 / 5日均成交额（亿）/ MA250 / 乖离率_pct / 波动率_pct / ATR14_pct / 历史天数 / 更新日期`

**输出路径**：
- 程序读取用 → `.state/oracle_v2_universe.json`（无变化）
- 人类查看用 → `.state/oracle_v2_universe.csv`（`utf-8-sig`，Excel 直接打开）
- 流动性大榜 → `.state/top100_liquidity.csv`（无变化）

**[OPS]** `名称` 字段通过 `_get_etf_names()` 批量查询，与 Top100 CSV 复用同一辅助函数，不引入新依赖。

**验证**：`py_compile` → **Exit 0**

## 📅 2026-04-15 — refine_core_universe Top100 流动性榜 CSV 输出

### 变更文件：`tools/refine_core_universe.py`

**[FEAT] Round 1 结束后同步输出 Top 100 流动性 CSV（含 ETF 中文名称）**

| 新增内容 | 说明 |
|---|---|
| `_get_etf_names(codes)` | 批量调用 `get_instrument_detail` 获取 ETF 简称，返回 `{code: name}` 字典 |
| `_TOP100_CSV_RELPATH` | 输出路径常量 `.state/top100_liquidity.csv` |
| Top 100 CSV 落盘逻辑 | Round 1 流动性排名完成后，立即取前 100 名，逐一查名称，组装 DataFrame，以 `utf-8-sig`（Excel 友好）写盘 |
| Top10 预览增强 | 控制台 Top 10 打印行新增名称列 |
| 最终汇总日志 | 末尾 `=====` 汇总块新增 Top 100 CSV 路径一行 |

**CSV 字段**：`排名 / 代码 / 名称 / 5日均成交额（亿）`

**[PATTERN] `get_instrument_detail` 名称获取**：返回值为 dict，中文名 key 为 `InstrumentName`，未获取时降级为空字符串，不崩溃。

**验证**：`py_compile` → **Exit 0**

## 📅 2026-04-15 — _TimeFlag 落盘持久化升级（消灭重启双触发 Bug）

### 变更文件：`autopilot_master.py`（`_TimeFlag` 类）

**[BUG] 根因**：`_TimeFlag` 是纯内存 set，autopilot 自我重启后 `_fired` 清零，新进程在同一分钟内看到同一 `hhmm` 再次满足 `>= hhmm` 条件 → 同一天同一节点双触发 → `oracle_telemetry_macro.csv` 出现重复批次

**[ARCH] 修复：落盘持久化版**

| 特性 | 旧版 | 新版 |
|---|---|---|
| 存储 | 内存 `set` | 内存 `set` + `.state/timeflag_state.json` |
| 重启恢复 | ❌ 清零 | ✅ 读取今日已触发 key |
| 触发逻辑 | `now >= hhmm` (永久满足) | `hhmm <= now < hhmm+30min` (30分钟宽容窗口) |
| mark | 内存写 | 内存写 + 立即落盘 |

**[OPS] 同步清理**：`oracle_telemetry_macro.csv` 中 `2026-04-15 14:42:18` 重复批次（9行）已删除

**[ROOT CAUSE] 今日记录不全**：代码在 10:36 更新（5次/天调度），但 autopilot 进程未重启，全天仍用旧代码（单次 14:42 触发）。明日重启后生效。

**[验证]**：`py_compile autopilot_master.py` → **Exit 0**

## 📅 2026-04-15 — 双模态预言机架构升级 + Sniper 参数调整


### 变更文件 1：`macro_rotation_executor.py`（rotation_v2）

**[ARCH] 双模态（Shield + Spear）选股架构升级**

```
旧：全量 Oracle → Odds 排序 → Top2
新：全量 Shield → 只有多头排列才进入 Oracle → Odds 排序 → Top2
```

| 组件 | 函数 | 逻辑 |
|---|---|---|
| 🛡️ 物理防爆墙 | `_momentum_shield()` | MA20 > MA60 且 Price > MA60，SAFE_ASSET 免检 |
| 🗡️ 高维收割机 | `_dual_slot_decision(df_rank, momentum_map)` | Shield 过滤后，再按 Odds+Yield 双门槛选 Top2 |
| 全局熔断 | — | 若无任何标的通过 Shield，双槽强制切 SAFE_ASSET |
| 遥测 | `_write_telemetry(..., momentum_map)` | 新增 `Momentum_Pass` 列 |

- **核心铁律**：120 bars 日线数据已满足 MA60 计算，无需新增下载
- **执行时序**：Shield 在 Oracle 请求之前计算，不浪费 Oracle 算力扫描空头标的
- **验证**：`py_compile` → **Exit 0**

### 变更文件 2：`sniper_entry_executor.py`（用户调整）

**[OPS] Sniper 参数调整**
- `SNIPER_TOTAL_CAPITAL`: 9万 → **10万**
- `TARGET_COUNT`: 3 → **2**（集中火力，双狙）
- **验证**：`py_compile` → **Exit 0**

## 📅 2026-04-15 — tools/etf_name_fetcher.py 新增 T+0/T+1 分类器


### 变更文件：`tools/etf_name_fetcher.py`（新建）

**[FEAT] ETF 全称查询 + T+0/T+1 交易制度分类器**
- **背景**：xtdata `get_instrument_detail()` **不暴露** T0/T1 字段（两只代表 ETF 全字段对比确认），必须用 A 股监管规则推断
- **规则引擎 `classify_t0_t1(code)`**：

| 类型 | 代码前缀 | 交易制度 |
|---|---|---|
| 跨境/QDII ETF | SH: `513`, `520` | T+0 ✅ |
| 黄金 ETF | SH: `518` | T+0 ✅ |
| 债券/货币 ETF | SH: `511` | T+0 ✅ |
| 跨境 SZ ETF | `159xxx` 白名单（港股通/QDII） | T+0 ✅ |
| A 股股票型 ETF | SH: `510`, `512`, `515`, `517`, `588` | T+1 ❌ |
| A 股主题 SZ ETF | `159xxx` 未在白名单 | T+1 ❌（保守） |

- **双池扫描**：同时读取 `fixed_t0_target.yaml`（T0池）+ `oracle_v2_universe.json`（ETF宇宙），合并去重，标注来源
- **输出**：控制台打印 + `.state/etf_names.csv`（含 code/regime/source/name 四列）
- **实测**：44 只合并标的，T+0=23 | T+1=21 | 未知=0
- **验证**：`py_compile` → **Exit 0**

## 📅 2026-04-15 — EOD 数据流水线升级（二步→三步串行）


### 变更文件：`autopilot_master.py`

**[FEAT] task_eod() 插入达尔文 ETF 精选步骤**

```
旧：daily_sync → 1m_download
新：daily_sync → refine_core_universe → 1m_download
```

| 步骤 | 脚本 | 作用 |
|---|---|---|
| Step 1 | `qmt_daily_sync.py` | 拉取最新日线数据落盘 |
| **Step 2** | `tools/refine_core_universe.py` | 基于最新日线重算 ETF 宇宙，更新 `.state/oracle_v2_universe.json` |
| Step 3 | `qmt_1m_downloader.py` | 用最新宇宙下载 1m 分钟线（含新入选 ETF） |

- **数据连贯性保证**：三步全部 `run_blocking()`，严格串行，Step 2 拿到的是 Step 1 刚落盘的最新日线
- 新增常量 `REFINE_UNIVERSE_SCRIPT = _DIR / "tools" / "refine_core_universe.py"`
- **验证**：`py_compile` → **Exit 0**

## 📅 2026-04-15 — qmt_1m_downloader 双数据源并集下载


### 变更文件：`qmt_1m_downloader.py`

**[FEAT] 新增 oracle_v2_universe.json 下载数据源**
- **旧架构**：只读 `fixed_t0_target.yaml`（T0 核心标的）
- **新架构**：同时读取两个数据源，取**并集**后统一下载

| 数据源 | 路径 | 内容 |
|---|---|---|
| T0 核心标的 | `.state/fixed_t0_target.yaml` | 人工维护的 T0 交易标的 |
| ETF 宏观宇宙 | `.state/oracle_v2_universe.json` | 达尔文机制每日更新的 30 只 ETF |

- **并集逻辑**：`set(yaml_codes) | set(oracle_codes)`，自动去重，两边重叠的标的不重复下载
- **路径修正**：`FIXED_TARGETS_PATH` / `ORACLE_UNIVERSE` 改为 `_DIR` 相对构建，防止 CWD 漂移
- **新增函数**：`get_targets_from_oracle_universe()` — 读取 JSON `universe` 数组，提取 `code` 字段
- **日志输出**：`📋 合并后总目标: N 只（T0=X, ETF宇宙=Y, 去重后=N）`
- **验证**：`py_compile` → **Exit 0**

## 📅 2026-04-15 — refine_core_universe 输出路径统一到 .state/


### 变更文件：`tools/refine_core_universe.py`

**[OPS] 输出文件归位 `.state/`**
- 旧路径：`Z:\QuantpC_Workspace\Data\oracle_v2_universe.json`（外部目录，跨驱动器绝对硬编码）
- 新路径：`.state/oracle_v2_universe.json`（相对路径，自动拼接 `_PROJECT_DIR/.state/`）
- 统一与所有策略状态文件（`macro_slots.json`、`sniper_holdings.json` 等）共处同一目录
- **验证**：`py_compile` → **Exit 0**

## 📅 2026-04-15 — Sentinel 监测频率升级（每日 1 次 → 5 次）


### 变更文件：`autopilot_master.py`（`task_loop` 调度引擎）

**[FEAT] 宏观全量雷达调度频率升级**
- **旧架构**：Sentinel 每天只在 14:42 触发一次（周一~四），无法捕捉早盘/午前/午后的赔率突变
- **新架构**：每整点触发一次，共 5 次/天，生成全天宏观景气全景图

| 时间节点 | Key | 周一~四行为 | 周五行为 |
|---|---|---|---|
| 09:30 | `sentinel_0930` | ✅ 全量雷达扫描 | ⏩ 跳过（进攻日） |
| 10:30 | `sentinel_1030` | ✅ 全量雷达扫描 | ⏩ 跳过 |
| 11:30 | `sentinel_1130` | ✅ 全量雷达扫描 | ⏩ 跳过 |
| 13:00 | `sentinel_1300` | ✅ 全量雷达扫描 | ⏩ 跳过 |
| 14:00 | `sentinel_1400` | ✅ 全量雷达扫描 | ⏩ 跳过 |
| **14:42** | `macro_rotation_attack` | ⏩ 跳过（守卫已触发） | ✅ **进攻执行（executor）** |

- **设计原则**：
  - `_TimeFlag` 每个节点使用独立 key，保证每天每时间点只触发一次
  - `for` 循环遍历 5 个 `(key, hhmm)` 对，代码结构紧凑
  - 进攻时间（14:42）**不变**，只有周五才触发 `macro_rotation_executor`
  - 周五整点不触发守卫（节省 Oracle 算力，留给 14:42 进攻全量扫描）
- **遥测效果**：每个交易日 `oracle_telemetry_macro.csv` 将产生 **5 批 × 9 行 = 45 行**全景数据，可完整还原全天宏观赔率漂移曲线
- **验证**：`py_compile autopilot_master.py` → **Exit 0**

## 📅 2026-04-15 — oracle_telemetry_macro.csv 全量雷达升级 + tz-naive 切片铁律


### 变更文件 1：`macro_risk_monitor.py`（The Sentinel）

**[FEAT] 雷达永远开机 — 全量遍历架构（从"持仓扫描"升级为"全场扫描"）**
- **问题**：守卫原本只对当前 2 只持仓槽位请求 Oracle，遥测 CSV 每次仅写 2 行，缺乏全局视野。
- **用户铁律（三阶段架构）**：
  - **第一阶段**：永远遍历完整 `MACRO_POOL`（9 只），全量压榨 Oracle 算力，生成雷达全景图
  - **第二阶段**：只对 `hold_codes`（2 只持仓）做熔断裁决（严禁周一~四进攻）
  - **第三阶段**：无差别全量落盘——无论 `持有`/`熔断卖出`/`idle`，9 只全部写入 CSV
- **核心改动**：
  - 模块级新增 `MACRO_POOL` 常量（9 只，与 executor 完全一致）
  - `actions` 字典初始化为 `{code: 'idle' for code in MACRO_POOL}`（全量基底）
  - `_fetch_sentinel_ammo(hold_codes)` → `_fetch_sentinel_ammo(MACRO_POOL)`（全量拉取）
  - 熔断裁决循环仍只迭代 `hold_codes`，覆写对应 code 的 action
  - CSV 由 2 行/次 升级为 **9 行/次**，完整呈现宏观全景
- **验证**：`py_compile macro_risk_monitor.py` → **Exit 0**

### 变更文件 2：`sniper_exit_guard.py` + `tools/patch_telemetry_mfe.py`

**[BUG] Fix-5 — tz-naive 切片铁律（彻底消除切割窗口偏移至昨日的问题）**
- **故障**：MFE/MAE 显示的是**昨天**的最高/最低价，不是今天的。根因为 tz-aware index 与 tz-naive cutoff 混打，Pandas 内部时区换算导致切割基准偏移 +8 小时
- **铁律（写入知识库）**：
  > 涉及 `>/</>=/<=` 切片，两端必须都是 `pd.to_datetime()` 朴素时间戳，禁止与 tz-aware 混用
- **实现**：
  - 重命名为 `_parse_qmt_index_naive()`
  - 1m index：`pd.to_datetime(idx.astype(str), format="%Y%m%d%H%M%S")` — **不加时区**
  - 1d index：`epoch-ms → UTC → CST → .tz_localize(None)` — **剥离时区**
  - cutoff/entry_day：`pd.to_datetime(str)` — **朴素时间戳**
- **验证回填**：重置 3 条坏记录 → 重跑 `patch_telemetry_mfe.py`，Bar数从 284 → **296**（多 12 根，证明窗口从正确时间点切割）

## 📅 2026-04-15 — Sniper Telemetry MFE/MAE 全零 Bug 三重修复


### 变更文件 1：`sniper_exit_guard.py`（`_write_sniper_telemetry` 函数）

**[BUG] MFE/MAE 早盘平仓记录全为 0.0**
- **故障现象**：`sniper_telemetry.csv` 中今日早盘（09:32~09:37）3条记录的 `intraday_high/mfe_pct/intraday_low/mae_pct` 全为 `0.0`，昨日同文件的 3 条则正常。
- **根因1 — 未订阅 1m K 线**：`_write_sniper_telemetry` 内调用 `get_market_data_ex(period="1m")`，但 exit_guard 只在主循环的 `subscribe_quote(code, period='tick')` 处订阅了 tick，从未订阅 1m。QMT 分钟 K 线需先订阅才有本地缓存，未订阅时 `get_market_data_ex` 返回空 DataFrame → 走 except → 写 0.
- **根因2 — 异常静默吞掉**：`except Exception as _e: print(f"...写入 0: {_e}")` 只打印 `_e` 字符串，丢失完整 traceback，无法诊断实际报错类型。
- **根因3 — 调用时未传 `entry_timestamp`**：`_write_sniper_telemetry` 的 `entry_timestamp` 参数在调用处未传入，cutoff 退化为 `entry_date 09:25`。若 1m bar 全部在 cutoff 之前（今日 bar 尚未缓存），切割后空表，MFE/MAE 置零。
- **根因4（Fix-4，运行时才暴露）— QMT 1m index 格式非 epoch-ms**：`get_market_data_ex(period="1m")` 返回的 DataFrame index 实际为 `YYYYMMDDHHMMSS` 格式整数（如 `20260414093500`），而非毫秒 epoch。旧代码用 `pd.to_datetime(index, unit="ms")` 转换，触发 `OutOfBoundsDatetime`，被 except 静默吞掉后写 0。日线(`1d`)的 index 才是 epoch-ms。


**[FIX] 三重修复 + Fix-4 + Fix-5（tz-naive 铁律）**

1. **Fix-1 — 主动订阅 1m**：`xtdata.subscribe_quote(code, period="1m", count=-1)`
2. **Fix-2 — 1m 空时降级日线**：切割为空时自动 fallback 到 `period="1d"`
3. **Fix-3 — 完整 traceback 暴露**：`print(_tb.format_exc())`  
4. **Fix-4 — QMT 1m index 格式为 YYYYMMDDHHMMSS**：旧 `unit="ms"` 触发 `OutOfBoundsDatetime`，改为 `pd.to_datetime(idx.astype(str), format="%Y%m%d%H%M%S")`
5. **Fix-5 — tz-naive 统一铁律（切割窗口偏移根治）**：tz-aware + tz-naive 混打导致切割基准实际偏移 +8h，从昨天的数据开始切。彻底重写 `_parse_qmt_index_naive`：
   - 1m index → `format="%Y%m%d%H%M%S"` → **不加任何时区**
   - 1d index → `epoch-ms → UTC → CST → .tz_localize(None)` 剥离时区
   - cutoff / entry_day → `pd.to_datetime(str)` → **naive 朴素时间戳**
   - **铁律**：涉及 `>/</>=/<=` 切片的两端，永远两端都是 `pd.to_datetime()` 朴素时间戳，禁止与 tz-aware 混用



**[FEAT] 调用处传入 `entry_timestamp`**：
- 调用 `_write_sniper_telemetry` 时，从 `info.get('entry_ts', '')` 读取精确入场时间戳，传入 `entry_timestamp` 参数，切割精度从「入场日 09:25」提升至分钟级。

**验证**：`py_compile sniper_exit_guard.py` → **Exit 0**

---

### 变更文件 2：`sniper_entry_executor.py`（`_write_holding` 函数）

**[FEAT] 持仓账本新增 `entry_ts` 字段**
- `_write_holding()` 写入 `sniper_holdings.json` 时，新增 `"entry_ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")` 字段，记录精确到秒的实盘成交时刻。
- exit_guard 的 Auditor 从 `info.get('entry_ts', '')` 读取此字段，作为 1m K 线绝对时间切割的基准，取代原先固定的「入场日 09:25 fallback」。
- 向后兼容：新字段仅追加，旧账本（无 `entry_ts`）在 get 时返回空字符串，Auditor 自动 fallback 到 09:25 逻辑，不破坏现有数据。

**验证**：`py_compile sniper_entry_executor.py` → **Exit 0**

---

## 📅 2026-04-14 — Sniper Auditor 探针升级 + 进场执行器双刀手术


### 变更文件 1：`sniper_exit_guard.py`（`_write_sniper_telemetry` 函数手术）

**[BUG] MFE/MAE 数值与实盘肉眼行情不一致（日线级糊涂账）**
- **故障现象**：`sniper_telemetry.csv` 中记录的 `intraday_high` / `intraday_low` 与外部行情软件（同花顺/迅投）显示的建仓后极值明显不符。
- **根因**：旧逻辑拉取 `period="1d"` 日线（取最近 10 根），通过 `df.index >= entry_date.replace("-","")` 字符串比较做日期过滤。日线 bar 包含全天 OHLC，无法切割到"下午 2:50 之后"的分钟级精度；且与外部软件默认使用前复权坐标系不一致（无 `dividend_type`）。

**[FEAT] 高精度-v2：1m 分钟线 + 绝对时间切割**
- 拉取 `period="1m"`, `count=1000`（约 4 个完整交易日），`dividend_type="front"` 前复权，与外部软件坐标系完全对齐
- QMT 分钟线 index 为整数毫秒时间戳，转换：`pd.to_datetime(index, unit="ms").tz_localize("UTC").tz_convert("Asia/Shanghai")`
- 切割基准：优先用 `entry_timestamp`（精确到秒）；不传则 fallback 到 `entry_date 09:25:00`（集合竞价结束）
- 物理极值：`df_trade[df_trade.index > cutoff]["high"].max()` / `["low"].min()`
- 函数签名新增可选参数 `entry_timestamp: str = ""`，向后兼容（不传则使用 fallback）
- 新增诊断日志：`[Auditor] 分钟线切割`，打印切割基准/有效 Bar 数/最高最低值

**验证**：`py_compile sniper_exit_guard.py` → **Exit 0**

---

### 变更文件 2：`sniper_entry_executor.py`（进场执行器双刀手术）

**[FEAT] 手术A — 限价单升维为越档扫单（ask3 Sweep Order）**
- **问题**：旧逻辑 `buy_price = round(ask1 * 1.01, 2)` 仅在 ask1 基础上上浮 1%，盘面快速拉升时可能挂在盘外，产生 Pending 委托超时未成，导致账本漏记（即昨日 301373 竞态 Bug 的前置诱因之一）。
- **修复**：
  - 拿取 askPrice 列表中的 ask1 / ask3（卖一 / 卖三价）
  - `sweep_price = ask3 if ask3 > ask1 else ask1`（优先越档扫卖三，ask3 无效时 fallback ask1）
  - `buy_price = min(round(sweep_price, 2), up_limit - 0.01)`（受涨停板天花板保护，永不撞板）
  - 日志标注 `[ask3越档扫单]` / `[ask1]` 便于复盘区分
- **效果**：一次性吃掉三档卖盘，成交概率大幅提升，极大降低 Pending 率。

**[FEAT] 手术B — 冷血清道夫机制（The Sweeper）**
- **问题**：旧逻辑在超时后只打印警告，未执行撤单 + 补写账本，导致：① 幽灵挂单持续占用资金；② 回调丢失时账本漏记持仓（即 301373 / 301526 屡次复现的"账本缺失"根因）。
- **修复（三步原则，顺序不可乱）**：
  1. **Step B-1 物理补录**：`query_stock_trades` 物理查询真实成交量，若实盘成交 > 回调记录 → 用实盘数据覆写账本（修复回调丢失）
  2. **Step B-2 撤单释放**：若 remaining > 0 → `cancel_order_stock` 无条件撤单 + N8N 告警，释放冻结资金
  3. **Step B-3 终稿锁定**：若 total_fill > 0 → 再次调用 `_write_holding` 确保账本最终态正确（`_write_holding` 内置 `_HOLDINGS_LOCK`，重复调用安全）
- **record_action 标记**：Sweeper 兜底写入的记录 action 字段为 `买入(Sweeper兜底)`，便于事后区分正常成交与 Sweeper 补写。

**验证**：`py_compile sniper_entry_executor.py` → **Exit 0**

---

## 📅 2026-04-13 — Sniper 竞态写账 Bug 修复 + 301373 账本补录

### [BUG] sniper_entry_executor.py — 多笔成交回调并发覆盖（竞态）

**故障现象**：301373.SZ 凌玮科技确认成交（实盘 300股 @ 93.615），但 `sniper_holdings.json` 中完全缺失该记录，仅存 002824.SZ 和 301526.SZ 两条。

**根因分析（完整时间线）**：
- 14:46 Sniper 击发 3 笔委托：002824（seq=1099086530）、301373（seq=1099086535）、301526（seq=1099086549）
- `on_stock_trade` 回调在不同线程中触发，`_write_holding()` 内部采用**读-改-写**模式（非原子）
- 002824 的回调**两次**触发（日志证据：`真实成交已入账：500股 @ 27.12` 出现2次），第二次写盘时读到的 JSON 仅含 {002824}（301373 尚未写入），直接覆盖掉了 301373 的写入结果
- 30秒超时后系统打印 `⚠️ [Sniper 确认超时] 单号 1099086535 凌玮科技 仍处于待定状态`，但实盘已成交，只是账本被覆盖

**修复**：`_write_holding()` 内全量逻辑用 `threading.Lock()` 保护为原子操作
```python
# 新增全局锁（与 threading import 同级）
_HOLDINGS_LOCK = threading.Lock()

# _write_holding 内部用 with _HOLDINGS_LOCK 串行化
with _HOLDINGS_LOCK:
    holdings = json.load(...)
    holdings[code] = {...}
    json.dump(holdings, ...)
```

**验证**：`py_compile sniper_entry_executor.py` → **Exit 0**

### [OPS] sniper_holdings.json — 手动补录 301373.SZ 凌玮科技
- 通过 `tools/_query_positions.py` 直连 QMT 实盘物理核查，确认实盘持仓：`volume=300, can_use_volume=0（T+1锁定）, open_price=93.615`
- 手动补录记录：`buy_price=93.615, qty=300, ordered_price=94.18, ordered_qty=300, date=2026-04-13`
- `date` 设为今日（2026-04-13），`can_use_volume=0` 证明 T+1 锁定，exit_guard 明日可正常执行时间死线平仓

### [PATTERN] 多笔回调并发写 JSON 必须加锁
- **适用场景**：任何在 `XtQuantTraderCallback.on_stock_trade()` / `on_order_stock()` 中写 JSON 状态文件的代码
- **铁律**：QMT 回调在独立线程触发，多笔委托可同时成交，JSON 读-改-写**必须**用 `threading.Lock()` 串行化
- **已修复文件**：`sniper_entry_executor.py`（引入 `_HOLDINGS_LOCK`）
- **潜在风险**：T0 executor 的 `on_order_trade` 回调已改为 Fill-Based 架构（有 `_t0_pending_lock`），已合规；其他回调处需排查

### [BUG] sniper_entry_executor.py — 分笔成交 qty 只记最后一笔

**故障现象**：002824.SZ 委托1000股，分两笔各500股成交，账本 qty=500（只记了最后一笔），而非合计1000股。

**根因**：`on_stock_trade` 每次回调直接用 `response.traded_volume`（本笔数量）写账本，完全覆盖前一笔的记录。

**修复（参照 T0 Fill-Based 抽屉累积架构）**：
- `PENDING_ORDERS` 每条新增 `fill_qty_total=0` / `fill_amount_total=0.0` 抽屉字段
- `on_stock_trade` 每次回调：`fill_qty_total += this_qty`，`fill_amount_total += this_price × this_qty`
- 用 `vwap = fill_amount_total / fill_qty_total` + 合计量覆写账本
- `fill_qty_total >= ordered_qty` 才触发最终通知 + `status='filled'`，否则 `status='partial'` 继续等待
- 超时告警新增已成交量信息：`状态=partial | 已成交=X/Y股，请手动核实`

**账本手动修正**：002824.SZ qty: 500 → **1000**（两笔500+500之和，VWAP=27.12）

**验证**：`py_compile sniper_entry_executor.py` → **Exit 0**

---

## 📅 2026-04-10 — 实盘运营事件（尾盘）

### [FEAT] autopilot_master.py — 非交易日日志抑制
- **问题**：周末/假日开机测试时，`logging.basicConfig` 在模块顶层无条件执行，创建空日志文件污染 `logs/` 目录
- **修复**：在 `task_loop()` 入口调用 `is_trading_day()`；非交易日时遍历 root logger 的 handlers，关闭并移除所有 `FileHandler`，仅保留控制台输出
- **副作用**：非交易日 N8N「循环启动」通知也同步抑制，不产生虚假告警

### [OPS] autopilot_master.py — 停用 MCP 服务器 + py 脚本关机
- **MCP 服务器**：`task_mcp_server()` 改为 `pass`（空操作），`task_loop()` 中的调用注释掉；改用外部方案
- **23:00 关机**：`task_shutdown()` 触发块注释掉；改用 Task Manager 强制关机，不再依赖 Python 脚本

### [BUG] sniper_exit_guard.py — 日期字段三层防护
**根因**：603158（昨日买入）因账本 `date` 被 reconcile 错写为今日，T+1 防线误封，14:45 时间死线未触发，导致需手动平仓
- **Layer 1**（`reconcile_positions_with_real`）：幽灵补录分支用 `can_use_volume` 推断日期（>0 → 写昨日；=0 → 写今日）；已知持仓分支确保 `date` 字段不丢失
- **Layer 2**（主循环运行时）：每轮巡逻检测 `date==今日` + `can_use_volume>0` 的矛盾状态，自动纠正为昨日并写盘，打印 `🔧 [日期自愈]`
- **Layer 3**（T+1 防线）：使用纠正后的 `date` 判断，确保前日买入标的在 14:45 时间死线/止盈止损正常触发


### [OPS] macro_rotation_executor.py — 自读自写 Bug + N8N 误导修复
**[BUG] macro_slots.json 写入时序错误（自读自写循环）**
- **根因**：状态文件在**决策阶段**（QMT 连接前）提前写入，下单阶段再读同一文件作为"上周期"→ `prev == new` → 误判"无变化"→ 0 笔交易，但状态已记录为"已持有"
- **修复**：状态快照在函数入口处读出保存为 `_prev_slots_snapshot`；状态文件写入移至**所有下单完成后**（原子写）
- **[BUG] N8N 无条件推送**：无论有无交易，始终推送同一标题 → 用户误判为成交通知。修复：有换仓 → `✅ 换仓成交`；无变化 → `📊 持仓不变（无交易）`
- **[OPS] macro_slots.json 两次手动重置**（早盘委托撤销后 + 14:45 自读自写后）

### [OPS] 今日 14:53 macro_rotation_executor 手动击发（实盘成交）
- Slot_A: 518880.SH 黄金ETF 5000股 @ 9.972，seq=1090633271
- Slot_B: 510300.SH 沪深300 10700股 @ 4.644，seq=1090633276
- 预言机赔率：518880 Odds=2.65 / 510300 Odds=1.62

### [OPS] Sniper 603158.SH（腾龙股份）手动平仓 + 账本清理
- **未触发自动平仓原因**：T+1 防线（L234 `date == today → continue`）跳过今日所有买入标的，导致 14:45 时间死线也不触发
- **用户今日手动平仓**，账本 `sniper_holdings.json` 已手动清空为 `{}`
- **次日守卫启动**：`reconcile_positions_with_real()` 会物理核查，若实盘已清则幽灵抹除，无需人工干预

### [BUG] autopilot 14:42 双实例导致 macro_rotation 调度静默
- 双实例（PID 9792/10128）竞争 `tf.mark("macro_rotation")`，其中一个抢先标记导致另一个跳过
- 14:44 手动触发后确认正常，下周五 autopilot 调度框架问题待监控

## 📅 2026-04-10 — FatFish 卖出链路审计（守卫原子写修复）

### 变更文件：`fat_fish_guard.py`（防御性修复）

**[BUG] `remove_slot_and_save()` 非原子写 — 止损斩仓后进程崩溃可产生幽灵仓位**
- **风险场景**：守卫触发止损斩仓 → 调用 `remove_slot_and_save()` → 写一半时进程崩溃（watchdog 重启）→ `fat_fish_slots.yaml` 文件损坏或半写 → 守卫重启后读到已平仓的槽位 → 对已清空的代码持续监控但找不到 can_use_volume → 状态不一致
- **修复**：`open(SLOTS_FILE)` → `tmp + os.replace(tmp, SLOTS_FILE)`，与执行器 `save_slots()` 的写法保持一致
- **卖出链路整体审计结论**：
  - ✅ `fat_fish_guard.py` 盘中 0.5s 轮询止损：读 `fat_fish_slots.yaml` → 正确
  - ✅ `fat_fish_master.py` 14:40 逃顶信号：写 `eod_sell` 到 `fat_fish_orders.yaml` → 正确
  - ✅ `fat_fish_executor.py` 14:50 执行卖出：读 `eod_sell` from `orders.yaml` → 正确（卖出路径不受 signals.json Bug 影响）
  - ✅ 执行器买入成功后写 `fat_fish_slots.yaml` → 守卫立即可感知（轮询读取，无缓存）
  - ⚠️ **修复点**：守卫斩仓后的槽位释放写操作从普通写改为原子写
- **验证**：`py_compile fat_fish_guard.py` → **Exit 0**；`py_compile fat_fish_executor.py` → **Exit 0**

## 📅 2026-04-10 — FatFish 执行器信号文件盲区修复（Root Cause Found）

### 变更文件：`fat_fish_executor.py`（逻辑 Bug）

**[BUG] 执行器只检查 orders.yaml，上架 signals.json 后买入永远不触发**
- **故障现象**：20260408 大脑 14:40 生成 3 条信号（159363.SZ/159967.SZ/159509.SZ），执行器 14:46/14:50 三次启动均打印"今日指令为空（无买无卖），系统静默退出"，买入从未执行。
- **根因**：执行器入口判断为：`if not orders or (not orders.get('buy') and not orders.get('eod_sell')): return`
  - 大脑在 2026-04-01 已重构为「买入信号写 `fat_fish_signals.json`、卖单写 `fat_fish_orders.yaml`」的隔离架构
  - 但执行器仍用旧逻辑：读 `orders.yaml['buy']`（永远为空） + `eod_sell`（无卖出时也为空）→ **两者均空则直接退出，signals.json 完全被绕过**
  - 这是一个架构重构遗留的「接口不一致 Bug」——大脑输出口变了，消费者没同步更新检测入口
- **修复**：
  - 入口改为双文件检测：**`orders.yaml` 不存在 AND `signals.json` 不存在** 才真正退出
  - 提前 `load_signals()` 进行空检测，有信号就进入买入阶段
  - 后续 `buy_signals` 直接复用，不再二次 `load_signals()` 调用
  - `current_slots = None` → `current_slots = {}` 防止后续 `code in current_slots` 崩溃
- **验证**：`py_compile fat_fish_executor.py` → **Exit 0**

## 📅 2026-04-10 — FatFish 阶段 3 预言机高维拦截注入

### 变更文件：`fat_fish_master.py`（微创植入）

**[FEAT] 物理三铁律 → 预言机测谎 双重过滤链**
- **植入位置**：阶段 3 `candidates.sort()` 之后、`selected = candidates[:N]` 之前
- **执行流程**：
  1. 物理三铁律筛出 `candidates`（突破 20 日新高 + 放量 1.5x + 未过度延伸）
  2. 提交预言机（`/predict_batch`）：用 `get_market_data_ex(count=119)` 取本地 K 线 + tick 缝合今日第 120 根
  3. **Odds_Ratio < 1.2 → 一票否决**（假突破，打印 `🚨 [假突破拦截]` + 具体赔率）
  4. **Odds_Ratio ≥ 1.2 → 测谎通过**（真突破，将赔率写入 `cand['odds_ratio']` 供后续排序参考）
  5. 预言机**超时 >10s 或连接断裂 → `candidates = []`**，取消本次所有右侧开仓以保全本金
- **降级安全**：预言机返回里没有某标的数据 → 按假突破处理（conservative fallback）
- **不影响原流程**：测谎完成后仍按 `momentum_score` 降序排序，取 `available_slots` 个 → 写 `fat_fish_signals.json`
- **import 补齐**：顶部新增 `import json` / `import requests`
- **验证**：`py_compile fat_fish_master.py` → **Exit 0**

## 📅 2026-04-10 — 换仓去重逻辑修复 + 测试数据清理（午盘）

### 变更文件：`macro_rotation_executor.py`（换仓对比逻辑）

**[BUG] 重复买入风险 — executor 不感知上周期已持有的标的**
- **故障场景**：11:46 执行器提交了 518880.SH + 510300.SH 的买单，但**用户在午盘手动撤销了委托**，实际未建仓。
  - ✅ 旧轮动 512890.SH（红利ETF）**已成功卖出**
  - ❌ 新槽位 518880.SH + 510300.SH **委托被手动撤销，未成交**
- **根因**：下单块直接调用 `_buy_slot()` 而不比对上周期 `macro_slots.json` 记录的已持有标的。
- **修复（换仓四步协议）**：
  1. **读上周期**：读取 `macro_slots.json` 的 `prev_a / prev_b`
  2. **对比变化**：构建 `slots_to_sell` 集合（变化的且非国债标的）
  3. **物理卖出**：用 `can_use_volume` 真实数量，FIX_PRICE Taker 卖出变化槽（真实持仓为零则跳过）
  4. **选择性买入**：已持有且不变的槽跳过买入，新/替换标的正常买入
- **[OPS] 状态修正**：`macro_slots.json` 手动重置为 `{slot_a:"", slot_b:""}` 反映真实空仓状态。14:42 执行器读到空槽 → `prev_a=""` → `if prev_a and ...` 全部 `False` → **不会误触发卖出** → 正常全量建仓。
- **[OPS] 测试数据清理**：删除根目录 `oracle_telemetry_macro.csv`（含 2 次测试跑：11:23 + 11:33）；`.state/oracle_telemetry_macro.csv` 保留（11:46 遥测快照，非实际成交）
- **验证**：`py_compile macro_rotation_executor.py` → **Exit 0**

## 📅 2026-04-10 — 时间线控制 + Sentinel 真实下单补全

### 变更文件：`macro_rotation_executor.py`、`macro_risk_monitor.py`、`autopilot_master.py`、`strategy_registry.yaml`

**[FEAT] 进攻/防守日时间线双轨制上线**
- **时间线规则（绝对机械化）**：
  - 进攻日（建仓/换仓）：每**周五 14:42**。全量请求预言机 9 只标的，执行"阈值洗牌"（Odds≥1.5 且 Q50_Yield>0.2% 按赔率填槽，不足则防守国债）。
  - 防守日（熔断扫描）：**周一至周四 14:42**。不找新猎物，只给现有持仓 2 只体检，Odds<1.0 或 Q50_Yield<0 触发平仓→切国债。

**[FEAT] macro_rotation_executor.py — 时间线守卫**
- 函数入口新增日期+时间双重检查：非周五 → `return`；周五但非 14:40-14:58 窗口 → `return`
- 防止手动误触发或调度器异常时间段执行

**[FEAT] macro_risk_monitor.py — 真实下单补全（TODO→实盘）**
- 新增 `_get_qmt_session()` / `_send_n8n()` / `_sell_slot()` / `_buy_safe_asset()` 四个函数
- 执行链：懒加载 QMT 会话 → 物理查 `can_use_volume` → FIX_PRICE Taker 平仓 → sleep(2) → 买入国债 → 原子更新 `macro_slots.json` → N8N 推送
- 遥测探针保持不变（追加写入 `oracle_telemetry_macro.csv`）
- 三文件 `py_compile` → **全部 Exit 0**

**[ARCH] autopilot_master.py — 调度槽重构**
- 新增 `MACRO_EXECUTOR` / `MACRO_SENTINEL` 路径常量
- `14:42` 新调度帧：周五 → `task_macro_rotation()`；周一至周四 → `task_macro_sentinel()`
-  `14:55` 旧 rotation 帧：改为静默忽略（历史兼容）
- 新增 `task_macro_rotation()` / `task_macro_sentinel()` 两个任务函数

**[OPS] strategy_registry.yaml — 旧轮动双停用**
- `ETF 轮动信号机` + `ETF 调仓执行器` 均 `enabled: false`
- autopilot 14:55 帧同步废弃，不再调用任何旧轮动脚本

## 2026-04-10 rotation_v2 正式切换实盘成交记录

### 变更文件：macro_rotation_executor.py（接入真实下单模块）

**[FEAT] rotation_v2 正式上线，完成首次实盘切换**
- 新增 `get_qmt_session()` / `_liquidate_old_rotation()` / `_buy_slot()` / `_send_n8n()` 函数
- 执行流程：预言机扫描 -> 双槽决策 -> 遥测落盘 -> 槽位状态落盘 -> QMT 下单
- 实盘成交记录（2026-04-10 11:46）：
  - [卖出] 512890.SH 红利ETF 84200股 @ 1.181 (旧rotation，seq=1090613305) 释放约99,440元
  - [买入] Slot_A 518880.SH 黄金ETF 5000股 @ 9.990 (赔率2.62，seq=1090613306)
  - [买入] Slot_B 510300.SH 沪深300 10800股 @ 4.629 (赔率1.72，seq=1090613307)
- 旧 `rotation_holdings.json` 已自动清零，旧系统 `etf_rotation_executor.py` 不再调度

## 📅 2026-04-10 — macro_risk_monitor.py The Sentinel 物理熔断守卫（新建）

### 变更文件：`macro_risk_monitor.py`（新建）、`macro_rotation_executor.py`（补丁）

**[FEAT] The Sentinel — 周一至周四物理熔断守卫**
- **架构角色**：
  - **周五（进攻窗口）**：运行 `macro_rotation_executor.py`，全量 9 只标的扫描，决定 Slot_A/B 填充物
  - **周一至周四（守卫模式）**：运行 `macro_risk_monitor.py`，仅扫描当前持仓，执行"拔插头"或"持续持有"
- **熔断阈值（绝对机械化）**：`Odds_Ratio < 1.0` OR `Q50_Yield < 0`
  - 物理意义：向上非对称性消失，或中位数预测已看跌，标的不再具备槽位价值
- **执行动作**：立即市价平仓 → 资金切换至 `511260.SH`（国债）；**严禁进攻**，即使当日赔率=10.0 也不允许在周一买新标的
- **遥测探针**：守卫模式下同样追加落盘 `oracle_telemetry_macro.csv`（`mode='a'`），Action 字段区分 `持有/熔断卖出/持有(国债)/数据缺失`，为后期复盘"逃顶有效性"和"误伤率"留存原始证据
- **槽位状态持久化**（`macro_rotation_executor.py` 补丁）：执行器决策后原子写入 `.state/macro_slots.json`，守卫启动时读取该文件确定巡逻目标，防止守卫无状态运行。
- **自动休息**：守卫在启动时判断 `weekday >= 5`（周五/周末），自动跳过并提示运行执行器
- **验证结果**：双文件 `py_compile` → **双 Exit 0**；执行器落盘 `macro_slots.json`，守卫识别周五自动休息逻辑正常

## 📅 2026-04-10 — macro_rotation_executor.py rotation_v2 重构

### 变更文件：`macro_rotation_executor.py`（原 oracle_test.py 正式命名 + 重构）

**[ARCH] rotation_v2：双槽位资金分配 + 遥测探针**
- **双槽位决策模块**：
  - 总仓 100,000，Slot_A / Slot_B 各 50,000，物理隔离，禁止叠仓
  - 硬性门槛：`Odds_Ratio ≥ 1.5` 且 `Q50_Yield > 0.2%`，符合者按 Odds_Ratio 降序填槽
  - 不足填入 `511260.SH`（国债）防守，两槽均防守时触发"双槽全防守"
  - 实测：2 只符合门槛：`518880.SH`(赔率2.60) → Slot_A，`510300.SH`(赔率1.71) → Slot_B
- **遥测探针模块（Telemetry Probe）**：
  - 每次获取预言机结果后，无论是否交易，立即追加写入 `oracle_telemetry_macro.csv`
  - **`mode='a'`（追加）**，永不覆盖历史，为 T+5 真实收益率回测留存原始证据
  - **P0 = `get_full_tick()` 实时 `lastPrice`**（发单那一刻市价，非昨收），用于复盘计算真实实盘滑点
  - 字段：`Timestamp, Code, P0, Q20, Q50, Q80, Odds_Ratio, Action`
- **验证结果**：9/9 只标的就绪，9 条遥测落盘，Exit 0。

## 📅 2026-04-10 — oracle_test.py 静态底座 + 动态缝合重构

### 变更文件：`oracle_test.py`

**[ARCH] fetch_macro_ammo 双段式张量组装架构**
- **需求**：盘中调用 `download_history_data2` 严重阻塞（每标的 5s 超时 × 9 只 = 45s），彻底不可用。
- **新架构**（两步走）：
  1. **静态底座**：`get_market_data_ex(count=119)` 纯读本地缓存，无任何网络调用，耗时约 10ms。
  2. **动态缝合**：`get_full_tick(macro_pool)` 一次批量拉取所有标的当前 `lastPrice`，作为第 120 根 K 线追加；`lastPrice=0`（盘后/休市）时自动用昨收价补位。
- **[CLEAN]** 删除 `_download_with_timeout` 死函数（无调用方）。
- **[FIX]** 脚本顶部加 `sys.stdout.reconfigure(encoding='utf-8')` 防 Windows GBK 吞 Emoji 崩溃。
- **验证结果**：9/9 只标的就绪，0 超时，每只精确 120 根（119 静态 + 1 动态），Exit 0。

## 📅 2026-04-10 — oracle_test.py 列表缺逗号致 RuntimeError

### 变更文件：`oracle_test.py`

**[BUG] MACRO_POOL 列表缺逗号 → QMT RuntimeError**
- **故障现象**：`get_market_data_ex` 拉取全部宏观标的时，QMT 内部抛出 `RuntimeError`（无任何提示信息）。
- **根因**：用户在 `'511260.SH'` 末尾缺少逗号，与下一行 `'513050.SH'` 紧邻。Python 的**字符串字面量隐式拼接**规则将两者合并为 `'511260.SH513050.SH'` 这个畸形代码进入列表，传给 QMT 后触发 RuntimeError。
- **注意**：此类 Bug `py_compile` **无法检出**（语法合法，运行时语义错误）。
- **修复**：在 `'511260.SH'` 末尾补加逗号。
- **验证**：`py_compile oracle_test.py` → **Exit 0**；QMT 不再 RuntimeError。
- **附注**：盘中 `download_history_data2` 超时属正常现象（QMT 交易时段带宽优先给行情推送），降级读本地缓存是正确行为，无需修复。

## 📅 2026-04-10 — Sniper 平仓日志增强（止盈/止损/时间死线）

### 变更文件：`sniper_exit_guard.py`

**[BUG] 止盈触发后日志无价格/数量/时间详情**
- **故障现象**：`20260410_sniper_exit_guard.log` 只有启动头部，002655.SZ 止盈卖出（09:52:42）在日志文件中无任何记录，只能在 `.state/action_logs/action_20260410.jsonl` 里看到不完整的结构化记录（缺 cost_price/sell_price/pnl_pct 等字段）。
- **根因**：
  1. `print()` 只打印了 `code / 单号 / 理由`，没有触发价、成本价、卖出挂单价、数量、浮盈%、时间戳。
  2. `record_action()` 的 `extra` 字典只有 `qty` 和 `seq`，缺少复盘所需的全部字段。
- **修复**：
  - `print` 改为多行格式，含：时间戳 / 标的代码+名称 / 理由 / 触发价 / 成本价 / 浮盈% / 卖出挂单价 / 数量 / 序列号。
  - `record_action extra` 补全：`name / cost_price / sell_price / pnl_pct / exit_ts`。
  - N8N 推送内容同步将 `卖出价` 字段名改为 `触发价`（语义更准确）。
- **验证**：`py_compile sniper_exit_guard.py` → **Exit 0**

## 📅 2026-04-01 — 新增 Log 精炼器工具

### 变更文件：`tools/log_refinery.py`（新增）

**[FEAT] AutoPilot 日志精炼器 — 大模型分析专用**
- **需求**：688,764 行原始日志（67.1 MB，22 个文件）无法直接喂给大模型分析；价差锁/盘口保护/Tick无效等轮询噪音占 99%+ 的体积。
- **架构**：两级过滤：① `_SKIP_RE` 噪音黑名单（优先，命中即丢弃）；② `_KEEP_RE` 关键事件白名单（大文件 >1000 行时启用精筛，小文件去噪即保留）。
- **尾盘熔断限频**：T0 的 `尾盘熔断` 每 10 分钟限留一行（防 T0 日志 40MB 进入输出）。
- **输出**：单一 Markdown 文件，按策略 `##` 章节分割，带时间戳文件名，输出至 `tools/log_refined/`，支持 `--date / --log-dir / --out-dir` 参数。
- **验证结果**：2026-04-01 全量日志 → **67.1 MB 压缩至 85 KB，压缩比 718:1**，保留 1,080 行关键事件。
- **语法验证**：`py_compile` → Exit 0

## 📅 2026-04-01 — AutoPilot V2 矩阵三项底层修复

### 变更文件：`t1_grid_executor.py`、`sniper_entry_executor.py`

**[BUG] T1 执行器 UnboundLocalError: `one_grid_qty` referenced before assignment**
- **根因**：第 1257 行 `if False:` 死代码块将 `one_grid_qty = 0` 物理锁死永不执行，但后续 L1263 的 `available_shares >= one_grid_qty` 却仍然引用它，导致每次轮询进入老版卖出分支时必然崩溃。
- **修复**：将 `if False:` 整块删除，改为无条件 `one_grid_qty = 0`（作为安全初始化基准值，老版逻辑后续条件 `one_grid_qty > 0` 自然将其短路跳过）。

**[BUG] Log Bomb — T1 主循环高频状态日志每秒无节制写盘**
- **根因**：`盘口保护窗口`/`Firewall`/`账本未初始化`/`冷却期`/`base_price=0`/`Tick无效`/`卖出强校验` 七类非关键状态 `log()` 调用，在 `while True` 3s 轮询中每标的每帧都打印，8 只标的 × 7 类 × 每3秒 ≈ 日志暴增，单文件可在数小时内达到 30MB+。
- **修复**：在统计初始化区块新增 `_log_throttle: dict[str, float]` 节流字典 + `_throttled_log(key, msg)` 闭包函数（60s 同 key 最多落盘一次）；将七类高频重复信息改调 `_throttled_log`，买入/卖出/报错等关键路径保持直接 `log()` 穿透。日志量理论降低 **60× 以上**。

**[BUG] Sniper 涨停板撞墙 — 委托价超出涨停价被柜台物理拒单**
- **根因**：`buy_price = round(ask1 * 1.01, 2)` 计算后虽有 `if buy_price > up_limit: buy_price = up_limit` 的强制压顶，但 **`>` 是严格大于**，`==` 时（即恰好等于涨停价时）不触发覆盖，最终 `buy_price == up_limit` 仍然送进 `order_stock`，被 QMT 柜台以「超过涨跌停价格」物理拒单。
- **修复**：在 `order_stock` 调用前插入独立涨停熔断判断：`if up_limit > 0 and buy_price >= up_limit: print(...) continue`，用 `>=` 兜底物理天花板，放弃本次买入并打印 `[涨停熔断]` 诊断日志。
- **验证**：三个文件 `py_compile` → **全部 Exit 0**

## 📅 2026-04-01 (T0 引擎收盘空转问题修复)

### 变更文件：`t0_multigrid_executor.py`

**[BUG] 收盘后引擎持续空转，日志暴涨至 40MB+**
- **故障现象**：交易时间结束后（20:46），两个日志文件 `20260401_t0_multigrid_executor.log`（40MB）和 `20260401_092208_t0_multigrid_executor.log`（21MB）仍在持续写入，每秒约 1.6KB。文件修改时间戳显示为当日 20:46，并非"2040年"（为显示截断误读）。
- **根因1**：`while True:` 主循环完全没有收盘退出条件，14:00 烙铁段仅冻结买入，引擎自身永不退出。
- **根因2**：`[尾盘熔断]` 日志行无节流保护，每 0.5s 每标的各打一行，2 只标的每秒 4 条，全程无上限。
- **修复1（收盘自动退出）**：在 `while True:` 主循环顶部（第一个 `if` 判断位置）加入 `>= "1530"` 时间判断，满足条件时 `log` + `print` + `break`，彻底退出主循环。
- **修复2（日志节流）**：`[尾盘熔断]` 的 `print` 改为 per-code 节流：读取 `rs.get('_fuse_log_time', 0)`，距离上次打印 `>= 60` 秒才打印，并更新时间戳到 `rs['_fuse_log_time']`。时间跨度从"每 0.5s 打一次"延长到"每 60s 打一次"，日志量减少 120 倍。
- **验证**：`py_compile t0_multigrid_executor.py` → **Exit 0**

## 📅 2026-03-09 (Initial Setup)
- **T0 稳定性升级**: 修复了 `atr_multiplier` 的 KeyError 崩溃，并实现了“物理全扫”逻辑防止孤儿资产。
- **统计套利实战化**: 引入 6 万元动态资金池与等权配额系统，废弃 100 股试单模式。
- **安全过滤升级**: 实现了双向极值（涨跌停）拦截，清退高溢价 QDII LOF 标的。
- **策略隔离墙**: 建立了 T0 与 Sniper/轮动策略的防火墙与分类日志系统。


## 📅 2026-03-09 (Automated Sync)
- **AutoPilot 自愈熔断机制增强** (`autopilot_master.py`)
  - 新增崩溃次数上限 (`MAX_SELF_RESTART`) 与熔断逻辑，超出后停止自拉起并发送告警，提升系统稳定性。

- **持仓检查与状态持久化** (`check_real_pos.py`)
  - 优化持仓查询流程，增加数据同步等待时间，并将结果持久化至 `.state/real_holdings.json` 文件，便于后续参考。

- **ETF列表构建与名称补充** (`get_etf_list.py`)
  - 增强 ETF 名称获取逻辑，通过 `xtdata.get_instrument_detail` API 作为后备方案补充缺失名称，提升数据完整性。

- **知识管理自动化同步优化** (`knowledge_manager.py`)
  - 改进 `Skill Log` 写入逻辑，增加日期重复性检查，避免同一天的内容被重复记录，确保日志清晰。

- **统计套利执行器风控强化** (`stat_arb_executor.py`)
  - 在 `StatArb` 策略中新增 `is_gap_break` 函数，通过监控基准指数跳空幅度（>3%）实现宏观熔断，当日禁开新仓。

## 📅 2026-03-10 (Architectural Shift: NAV & N8N Integration)
- **绝对物理清算 (NAV Accounting)** (`trade_settlement.py`)
  - 弃用不准确的单利模拟估算，改为使用大厂对冲基金的净值差额法。
  - 通过比对 `query_stock_asset` 的每日现金与总资产，精确倒推券商真实扣减的每一分钱手续费，并完美计入包含跨天及手动标的在内的所有净值波动盈亏。
- **全系统穿透层会计核算** (`accounting_audit.py`)
  - 每日 16:08 扫描 `query_stock_trades` 流水，基于策略标签硬性切分手动作业与自动策略。将非策略手工单标记为 `Unknown_Manual`，彻底避免自动防线（T0/Sniper）误平手工作业。
- **复盘引擎云端化** (`quant_debrief.py`)
  - 核心架构调整：应需求，移除了本地 LiteLLM 的调用推理。
  - 现在 `quant_debrief.py` 作为纯粹的“原始数据管道”，负责聚合 T0/Sniper 动作切片和 NAV 清算底稿，原样以 JSON 报文格式直推 N8N Webhook，所有的归因分析与智能生成均转移至 N8N 云端架构执行。

## 📅 2026-03-11 (Bug Fix: Orphan Asset Liquidation)
- **孤儿资产手续费 Bug 修复** (`t0_multigrid_executor.py`)
  - **故障现象**：早盘 `513140.SH`（港股金融ETF）不在当日 YAML 目标中，被识别为孤儿资产（`sell_only`），但产生了 56 笔连续委托（每笔仅 100 股），造成大量不必要的手续费。
  - **根因分析**：双重 Bug——① 孤儿的 `trade_amount=0` 导致 `buy_qty=0`，fallback 到固定 100 股；② 卖出代码块无价格触发条件，每个 0.5s tick 都下单。
  - **修复方案（方案A）**：`sell_only` 孤儿资产走独立分支，直接查询 QMT 实盘真实持仓量（`query_stock_positions`），**一次性发出全量 Taker 委托**后立即移出状态机。不再等网格均值回归。
  - **新行为**：下次遇到孤儿资产，只产生 **1 笔委托**，当日生效（下次 Autopilot 重启后加载新代码）。

## 📅 2026-03-11 (Bug Fix: StatArb Pipeline — stat_arb_executor & pair_researcher)

### `stat_arb_executor.py` — 致命 Bug 修复

- **根因（无交易记录）**：`xtconstant` 模块从未导入，但所有下单路径均引用 `xtconstant.STOCK_BUY/SELL`，在触发条件满足时抛出 `NameError`，被 `safe_execute_and_lock` 的 `try/except` **静默吞掉**，无任何日志痕迹。
  - **修复**：`from xtquant import xtdata` → `from xtquant import xtdata, xtconstant`

- **诊断方法论**：无交易记录时，优先检查 action_log（`.state/action_logs/`），再比对执行日志（`logs/`），若两者均无 StatArb 条目则指向代码级静默崩溃，而非标的不符合。

### `pair_researcher.py` — 4 个 Bug 修复

1. **ADF_P_Value 字符串排序（严重）**：原代码将 `p_value` 存为 `f"{p_value:.8e}"` 字符串，`sort_values()` 按字典序排，导致 CSV 中**协整关系最弱的标的对排首位**，executor 优先选最差配对。修复：直接存浮点数。

2. **OLS hedge_ratio 按列名取值 KeyError（严重）**：`sm.add_constant(X)` 传入 Pandas Series 时，返回 DataFrame 列名为 `["const", code_B]`，`.params[code_B]` 在 index 不对齐时抛 `KeyError`。修复：传 `X.values`（numpy），改用 `params[1]` 位置索引。同时增加 `hedge_ratio <= 0` 过滤（同向标的无套利意义）。

3. **data 字典未判断 key 存在性（中等）**：`if not data[code].empty` 在 `code` 不存在于 `data` 时直接 `KeyError`。修复：`if code in data and not data[code].empty`，并添加空列表时的 `RuntimeError` 快速失败。

4. **theta 极小值除法溢出（轻微）**：`theta` 为接近零的极小负数时，`-log(2)/theta` 产生极大值绕过 `MAX_HALF_LIFE=30` 过滤。修复：添加 `abs(theta) < 1e-10` 零保护。

### 架构认知：xtquant 客户端-服务端模型

- xtquant 是 **C/S 架构**，非直接文件读取库。所有操作（包括读历史 K 线）均通过 TCP 连接本地 miniQMT 进程（`127.0.0.1:58610`）完成。
- **离线能力边界**：miniQMT 进程在线 + 本地数据仓库已缓存 → 历史数据读取不需券商联网。实时 Tick / 下单 → 必须券商服务器在线。
- **最佳实践**：`pair_researcher.py` 应在每日收盘后（如 17:00）由 `autopilot_master.py` 调度运行，无需实时行情，安全刷新次日 CSV 配对参数。

### 配对排序策略决策（Ernest P. Chan 框架）

- **正确优先级**：① ADF p值（协整显著性） > ② 半衰期（资金效率） > ③ Hedge Ratio 合理性（0.3~3 为佳）
- **半衰期越短不一定越好**：<5 天往往是市场微观结构噪声而非真正协整，且 ADF 检验可信度低时短半衰期是假信号。
- **当前配置**：`MIN_HALF_LIFE=5, MAX_HALF_LIFE=30`，按 ADF p值升序排列，是合理的现实策略。


## 📅 2026-03-16 (Automated Sync)
- **AutoPilot 主循环自愈熔断机制增强** (`autopilot_master.py`)
  - 增加了崩溃重启次数上限 (`MAX_SELF_RESTART`)，超出后触发熔断并发送告警，需人工干预。

- **胖鱼策略执行后物理归档** (`fat_fish_executor.py`)
  - 执行器完成订单执行后，将指令文件 (`ORDERS_FILE`) 移动至历史归档目录 (`history/`)，实现防重复执行的物理隔离。

- **胖鱼防线斩仓失败告警升级** (`fat_fish_guard.py`)
  - 当斩仓指令被QMT交易网关拒绝时，除记录日志外，新增发送紧急告警 (`send_n8n_alert`) 通知人工介入。

- **Sniper策略执行器初始化流程优化** (`sniper_entry_executor.py`)
  - 明确了 `XtQuantTrader` 启动后必须等待至少3秒再连接 (`connect`) 的规范，并增加了回调注册 (`register_callback`) 和账户订阅 (`subscribe`) 的步骤。

- **策略注册表更新与清理** (`strategy_registry.yaml`)
  - 将 `20CM 游资雷达` 策略标记为 `enabled: false` 并注明已废弃，因其功能已由新版 `sniper_entry_executor` 内置的全市场扫描替代。

## 📅 2026-03-16 (Manual Bug Fix — v4.1 三连杀修复)

### Bug 1：T0 买入后手动清仓误判 (`t0_multigrid_executor.py`)

- **现象**：10:52 买入 `520500.SH` 5400 股后，同一分钟触发 `🌖 [手动清仓识别]`，`current_lots` 被归零，系统"遗忘"了刚买进的仓位，导致全天无法高抛，最终 5400 股隔夜持有。
- **根因**：`query_stock_positions` 在买单提交后短暂返回 `volume=0`（T+1 结算延迟未反映），代码无条件将 `current_lots` 清零。
- **修复**（Line 1091-1105）：清零前先检查 `rs.get('volume', 0)` — 若内部账本记录非零持仓，则判定为 QMT T+1 结算延迟误判，打印警告并 `continue`，不归零。只有内部账本也为零时才真正执行手动清仓识别。
- **应急修复**：将 `grid_state.json` 中 `520500` 的 `current_lots: 0 → 1` 使明日启动时物理对账能找到真实持仓并修正。

### Bug 2：grid_state.json 孤儿残留 — 159506 `current_lots: 64` 但实盘已清空

- **现象**：`159506.SZ` 下午卖出后账本 `current_lots=64`，收盘后 EOD reconcile 发现 100 股残差并清除，但 `current_lots` 未归零，留存至明日。
- **修复**：手动将 `grid_state.json` 中 `current_lots: 64 → 0`。
- **经验**：T0 `sell_save` 中 `current_lots -= 1` 在卖出队列快速执行时可能出现计数偏差，需配合物理对账机制兜底。

### Bug 3：轮动策略跨周持仓漏录 (`etf_rotation_executor.py`)

- **现象**：上周轮动执行失败，`511260.SH` 实际持有但从未写入 `rotation_holdings.json`。本周执行时：① executor 不认识 `511260` → 未卖出；② T0 的轮动防火墙靠 `rotation_holdings.json` 识别，找不到 `511260` → 被错误识别为 V4 孤儿在早盘卖出。
- **设计修复**：新增 `_sync_physical_holdings()` 函数，在执行任何买卖前先物理扫描券商真实持仓，将未记录的持仓（排除 T0/Sniper 管辖标的）补录入 `rotation_holdings.json`（标记 `_synced: True`）。
- **卖出闭包 Bug 顺带修复**：原 for 循环中 `def sell_save(): del holdings[code]` 是晚绑定，所有闭包共享循环末尾的 `code`。修复：改为 `def sell_save(_c=code): del holdings[_c]` 提前绑定。
- **资金分配修复**：原逻辑 `100k ÷ 总目标数` 再过滤，保留仓的配额被浪费。新逻辑：`freed_budget（卖出释放的资金）÷ 新买标的数`，保留仓不重新分配资金。

### 三种轮动场景验证设计

| 场景 | 期望行为 |
|---|---|
| 持A+B，新目标B+C | 卖A，保留B不动，C 获得 50k（freed_budget） |
| 持A+B，新目标B | 卖A，保留B不动，无新买入 |
| 持A+B，新目标C | 卖A+B，C 获得全部 100k |

## 📅 2026-03-15 (Fat Fish Strategy — Architecture Design)

### 胖鱼波段策略 (`fat_fish_master.py` / `fat_fish_guard.py` / `fat_fish_executor.py`)

整体架构是三进程协作的**独立策略系统**，由 `autopilot_master.py` 在 14:40 / 14:50 调度：

#### 三文件职责分工

| 文件 | 触发时间 | 职责 |
|---|---|---|
| `fat_fish_master.py` | 14:40 | 数据融合 + 因子计算 + 生成 orders.yaml |
| `fat_fish_guard.py` | 09:30 ~ 14:57（watchdog 保护）| 0.5s 实时轮询止损防线 |
| `fat_fish_executor.py` | 14:50 | 读取 orders.yaml，先卖后买，归档指令 |

#### Data Fusion 核心架构（`fat_fish_master.py`）

- **T-1 历史数据**：读取 `Market_Daily/*.parquet`（已进本地缓存的完整历史）
- **实时 Tick 缝合**：14:40 批量拉取全池 `get_full_tick()`，将 14:40 快照构造为"今日预估行"，成交量按 `240/220` 线性外推，追加至历史 DataFrame末尾
- **计算因子**：纯向量化，单标的约 0.4s（18 因子）
  - ATR-14 止损基准
  - 20 日新高突破 + 1.5x 放量确认
  - RSRS Z-score（情绪高潮因子）
  - Momentum Score（价量乘积，横截面排序）

#### 开仓三铁律（缺一不可）

```
① close > max_high_20（突破 20 日新高）
② vol > 1.5 × ma_vol_20（放量至少 1.5 倍）
③ (close - ma_20) ≤ 2 × atr_14（未过度延伸,追高保护）
```

#### 棘轮止損防线（`fat_fish_guard.py`）

- `stop_loss_price = highest_price - 2 × ATR_14`（追踪高点，只涨不降）
- 防线击穿触发 9 折限价扫盘（`FIX_PRICE`），不用市价单防止被交易所拒单
- 斩仓失败（`seq <= 0`）发送 N8N 紧急告警

#### 右侧逃顶信号（`fat_fish_master.py`，每日 14:40 检查）

```
RSRS Z-score 近 5 日最大值 > 1.2（情绪高潮）
AND close < MA_10（趋势破位）
→ 释放槽位，14:50 executor 执行尾盘清仓
```

#### 状态机文件链路

```
fat_fish_slots.yaml  ←  master 写入槽位状态 / guard 读取止损线
fat_fish_orders.yaml ←  master 生成买卖指令 / executor 消费后归档至 history/
```

#### 资金隔离

- `TOTAL_SLOTS = 3`，`CAPITAL_PER_SLOT = 20,000`，最大占用 6 万元
- 每日 08:50 全局资金防火墙：Fat Fish 资金池与 T0 Grid、Sniper、Rotation 完全隔离

#### MAX_SELF_RESTART 熔断设计极限与自愈思路

- `WATCHDOG_MAX_RESTART = 5`：策略进程连续崩溃 5 次后停止复活，发送 N8N 告警
- **设计理由**：无限重启可能放大资金损失（如策略逻辑 Bug 导致连续下错单），熔断是有意的安全设计
- **自愈建议**：若希望无人值守自愈，可在 N8N 的 Webhook 流程中加入自动回调 `autopilot_master.py --restart <strategy>` 的能力，或将熔断后的恢复时间延长到 30～60 分钟后再尝试一次（避免突发故障 vs 持续性 Bug 的区别）

## 📅 2026-03-17 (Bug Fix: T0 尾盘残留持仓 — 双重修复 v4.2)

### Bug：520500 连续多日留下 200 股残留持仓 (`t0_multigrid_executor.py`)

- **现象**：`520500.SH` 每日 T0 期间建格后，收盘时留下 200 股（2 格）过夜残留，次日被识别为孤儿。
- **根因1（主凶）**：`14:45` 买入熔断代码用 `continue` 语句直接跳到下一标的，**同时跳过了后续的网格卖出分支**，导致任何在 14:45 后仍有持仓的 active 标的既不买也不卖，格子被冻结至收盘。
  - **修复**：将整个买入 block 改为 `_buy_allowed` 标志位控制（布尔旗帜），`14:45` 熔断仅将 `_buy_allowed = False` 并打印提示，不再执行 `continue`，代码自然流向卖出分支。
- **根因2（次凶）**：代码缺乏收盘强制清仓保险，价格未涨到网格上轨的格子会永久等待直到收盘。
  - **修复**：在卖出分支中新增 `14:55` 强制全清：当 `_now_hhmm() >= "1455"` 且 `current_lots > 0`，无条件按实盘 volume 全量市价卖出，发 N8N 告警并记录 `action_log`。
- **验证**：`python -c 'import ast; ast.parse(...)'` 语法检查通过。


## 📅 2026-03-17 (Automated Sync)
- **新增健康检查接口** (`dr_receiver.py`)
  - 新增 `/health` 接口，用于外部监控系统（如 N8N）定时探活，确认灾备探针存活状态。

- **优化 ETF 轮动调仓执行器** (`etf_rotation_executor.py`)
  - 在调仓买入逻辑中，增加了对盘口异常和资金不足情况的处理与日志输出，提升了策略执行的健壮性。

- **完善胖鱼策略执行器归档机制** (`fat_fish_executor.py`)
  - 在执行完毕后，将当日订单文件自动移动到历史归档目录，并记录归档文件名，实现了执行记录的物理隔离与防重复执行。

- **增强胖鱼防线斩仓失败告警** (`fat_fish_guard.py`)
  - 在斩仓下单被拒时，除了记录日志，还增加了向 N8N 发送紧急告警的逻辑，以便人工及时介入处理风险。

- **改进 T0 网格执行器重连逻辑** (`t0_multigrid_executor.py`)
  - 优化了 QMT 交易网关的重连失败处理流程，在多次重连失败后明确提示用户手动介入，并停止自动运行，避免无效循环。


## 📅 2026-03-18 (Automated Sync)
- **AutoPilot 主循环自愈熔断机制增强** (`autopilot_master.py`)
  - 在主循环崩溃后，增加了重启次数上限 (`MAX_SELF_RESTART`) 检查和熔断逻辑，超出上限后将停止自拉起并发送告警，需人工干预。

- **盘中持仓对账引擎增加退出码** (`intraday_reconcile.py`)
  - 对账脚本 (`intraday_reconcile.py`) 在独立运行时，现在会根据对账发现的异常数量 (`n_issues`) 返回相应的退出码 (`0` 或 `1`)，便于外部脚本或任务调度器判断执行结果。

- **miniQMT 自动登录流程优化** (`start_miniQMT.py`)
  - 优化了 miniQMT 自动登录流程，在检测到进程已运行时，会跳过启动直接验证连接；增加了交易网关握手后的额外等待时间，提升了连接稳定性。

- **T0 多网格执行器增加重连失败处理** (`t0_multigrid_executor.py`)
  - 在 `T0 Executor` 中，当 `XtQuantTrader` 重连失败时，增加了更明确的错误信息打印和告警发送，并区分了重连失败和最终放弃运行的逻辑分支。

- **MCP 服务器增加单实例保护** (`MCP\mcp_server.py`)
  - MCP 服务器启动时，会先检测端口 `8000` 是否已被占用，若已有实例在运行则静默退出，避免了端口冲突和重复启动。

## uuD83DuDCC5 2026-03-18 (T1 Grid — slot_capital 架构 Bug 发现)

### Bug 发现：T1 `slot_capital` 颗粒度严重错误 (`t1_master.py` / `t1_grid_executor.py`)

- **现象**：T1 网格策略多只 ETF 出现 4W 持仓（理论上限 2W），整体超买约 15 万元。
- **根因**：`slot_capital = total_capital / n = 160000/8 = 20000`，但 executor 的 `calc_qty()` 用它计算**每格**买入量，每格实际买入 2 万元，而非设计上的"2 万是单标的所有格子的上限"。正确: `每格 = 2W / MAX_GRIDS_DOWN(4) = 5000 元`。最大 4 格才是 2 万封顶。
- **副作用**：字段名 `slot_capital` 未区分"单格额度"还是"封顶额度"，概念混乱。

### T1 资金架构重构 (`t1_master.py` / `t1_grid_executor.py`)

- **顶层常量**（唯一真相，不从 yaml 读）：
  - `TOTAL_CAPITAL = 160000`
  - `MAX_GRIDS_DOWN = 4`
- **字段变更**：删除 `slot_capital`，引入 `symbol_max_limit = 20000`（标的封顶）和 `per_grid_capital = 5000`（单格下单金额）。
- **executor 买入三重校验**：
  - ① `buy_qty = int(5000 / price / 100) * 100`（正确颗粒度）
  - ② Hard Cap：`(持仓 + 拟买) × price > symbol_max_limit` 则拒绝加仓
  - ③ 防跳空瀑布：`current_grid` 每次触发严格只 +1，无论跌穿多少格

---

## uuD83DuDCC5 2026-03-19 (T1 Grid — 紧急修复 + 实盘切换)

### 今日故障（早盘）

- **超买**：前日 bug 未修复，多只 ETF 跳空穿仓打满 4 格，总超买约 15 万元。
- **启动失败**：前日设置 23:00 冷关机（`shutdown /s`），WOL 唤醒后 Windows 要求桌面密码 → autopilot 未自动拉起，09:16 手动补拉。

### WOL 冷启动免密修复（运维）

- **问题根因**：`shutdown /s` 是完全关机，WOL 唤醒后 Windows 要求输密码。
- **修复**：`task_shutdown()` 改为 `os.system("shutdown /h")`（休眠）。休眠后 WOL 唤醒可直接恢复桌面，无需登录。
- **前提**：需以管理员身份运行 `powercfg /hibernate on` 启用休眠。

### 手动拉起 autopilot SOP（断电/重启后）

```powershell
# 1. 确认 MiniQMT 拉起且验证码已填（等 30s）
# 2. 执行（_TimeFlag 会自动追回过时任务）
python autopilot_master.py --task loop
```

### 紧急手术脚本 (`t1_emergency_reset.py`)

- 明早 09:30 后执行，物理修正超额持仓：
  - **模块 1**：QMT 获取真实持仓，`target = int(5000/price/100)*100`（1 格底仓），超额以买一价 Taker 卖出。
  - **模块 2**：重算 MA20/ATR，账本强制归一为 `grid=1 / available=target / locked=0`。
- **安全**：默认 `DRY_RUN=True`；`--live` 参数实盘，有 10 秒倒计时确认。

### 午休切换新 executor（11:56）

- 11:55 重跑 `t1_master.py`，全 8 只标的账本写入 `per_grid_capital=5000` / `symbol_max_limit=20000`。
- Kill 旧 PID 13332（`Process.Kill()`），清锁文件，watchdog 11:56:55 自动重启新 PID=7008，11:57:03 进入盘中轮询，新资金逻辑生效。


## 📅 2026-03-19 (Automated Sync)
- **AutoPilot 主循环自愈熔断机制** (`autopilot_master.py`)
  - 新增崩溃次数上限 `MAX_SELF_RESTART`，超出后停止自拉起并发送熔断告警。

- **Sniper 执行器启动流程优化** (`sniper_entry_executor.py`)
  - 规范 QMT 连接流程：`start()` 后强制等待 5 秒再 `connect()`，并注册专属回调。

- **T1 紧急重置脚本增强** (`t1_emergency_reset.py`)
  - 实盘模式下增加 15 秒等待，确保成交回报稳定后再进行账本覆写。

- **T1 Grid 执行器异常处理** (`t1_grid_executor.py`)
  - 完善 `finally` 块，确保执行器退出时释放进程锁并断开 QMT 连接。

- **T1 Master 日志输出细化** (`t1_master.py`)
  - 在解冻与 ATR 核算阶段，输出每只标的的详细风控参数与状态字段。

### 下午补充（同日）

#### T0 买入截止时间调整
- 将日内买入截止从 14:45 提前至 **14:00**，规避下午单边瀑布风险。
- 修改位置：`t0_multigrid_executor.py` L939/L941 _buy_allowed 条件与日志打印。
- 14:55 收盘强制清仓逻辑**不变**。

#### 潮汐资金池（虚拟杠杆）
- 新增顶层常量 `CAPITAL_LEVERAGE = 3.0`（L68），CEO 可随时调整。
- 启动时查询 QMT 真实现金，计算 `virtual_capital = (cash - 10000) * CAPITAL_LEVERAGE`。
- 按比例缩放各标的 `trade_amount`（向下取整至 100 的整数倍），动态匹配账户购买力。
- 安全保护：虚拟资金 < 30000 时放弃缩放，使用 YAML 原始配置。

#### 投资备注规范化
- 废弃 V4.x 系列备注，统一改为 T0_Grid 策略名 + 语义化备注：

| 旧备注 | 新备注 |
|--------|--------|
| V4.0 / Buy | T0_Grid / T0_Buy |
| V4.0 / Sell | T0_Grid / T0_Sell |
| V4_EOD / Sell | T0_Grid / T0_EOD |
| V4_Orphan / Sell | T0_Grid / T0_Orphan |
| V4_TP / Absolute | T0_Grid / T0_TP |
| V4_Abyss / Abyss | T0_Grid / T0_SL |

#### 518680 T0 下午亏损复盘
- 13:45～14:38 累计触发 3 格买入（连续下行 ~2.8%），EOD 强平亏损约 634 元。
- 根因：黄金 ETF 下午跟随海外金价单边下行，均值回归速度远慢于 A 股宽基。
- 应对：用户已手动修改 `fixed_t0_target.yaml` 降低 max_lots，配合 14:00 截止有效遏制类似风险。

#### 513810 T+1 误判复盘（非 Bug）
- 现象：14:52 卖出委托报"证券数量不足"。
- 根因：513810 为当日手动买入，A 股 T+1 规则下当日买入股票 vailable_volume=0，无法当日卖出，属正常限制。
- 513810 **不在 T1 白名单**，T1/T0 executor 均未发出该卖单，纯属手动操作被交易所正常拦截。


## 2026-03-20 (Manual Debrief - Three Firewall Bug Fix Day)

### Core Incident: T0/Rotation Killing T1 Positions

#### 故障现象
- 09:30 系统拉起后，513190/159502/159545/159552（T1 白名单）同时出现
  T1_EmgReset_Sell + TO_Orphan + Rotation/Exit 三种标签卖单。
- 14:55 轮动执行后，T1 底仓再次被 Rotation 以 Exit 卖出。
- 多笔 251005 证券数量不足报错（重复发单被拒，无超卖，正常兜底）。

#### 根因
三套策略防火墙均未登记 T1 Grid 领土：

| 防火墙位置 | 保护了 | 漏掉 |
|---|---|---|
| t0_multigrid_executor.py — reconcile_positions_with_real() | Rotation+Sniper | T1 |
| t0_multigrid_executor.py — run_multi_grid() 孤儿检测 | Rotation+Sniper | T1 |
| etf_rotation_executor.py — _load_excluded_codes() | T0+Sniper | T1 |

#### 三处修复（统一模式）

在每处 protected_codes 构建时加入 t1_grid_ledger.yaml：

    t1_ledger = os.path.join(STATE_DIR, 't1_grid_ledger.yaml')
    if os.path.exists(t1_ledger):
        t1 = yaml.safe_load(open(t1_ledger, encoding='utf-8')) or {}
        protected_codes |= set(t1.keys())

#### 防火墙架构纪律（Critical Rule）

> 每新增一个策略，必须同步更新所有其他策略的防火墙函数。

当前账本防火墙文件汇总：

| 账本文件 | 保护策略 |
|---|---|
| rotation_targets.yaml + rotation_holdings.json | Rotation（双源） |
| sniper_holdings.json | Sniper |
| t1_grid_ledger.yaml | T1 Grid（2026-03-20 新增） |
| grid_targets.yaml | T0 Grid |

---

### T1 空间熔断关闭（CEO 战略指令）

t1_grid_executor.py Executioner L651 修改：

    # 修改前
    trigger_space = (current_grid >= MAX_GRID_DEPTH and last_price <= hard_stop)
    # 修改后
    trigger_space = False  # CEO指令：关闭空间熔断，4格装死扛单，拒绝割肉
    trigger_time  = (idle_days >= 15 and available_shares > 0)  # 唯一触发不变

T1 是长线底仓策略，短期空间跌破不割；15 天无动作才触发机械清仓。

---

### 休眠替代关机（运维修复）

task_shutdown() 从 shutdown /s /t 0 改为 shutdown /h（休眠）。
冷关机唤醒后 Windows 要求密码，autopilot 无法自拉起。
休眠保留登录状态，唤醒无需密码。
前提：powercfg /hibernate on + 控制面板关闭唤醒需密码。

---

### T1 紧急手术（09:32）

运行 t1_emergency_reset.py --live（先 dry-run 确认）。
4 只标的超额强平，全部成交，账本归一 grid=1，executor 正常买回底仓。
SOP：每次系统重启后账本与实盘不符，先 dry-run 再 live。

---

### 今日汇总
- 3 处防火墙漏洞修复，明日起 T1 标的受三重保护。
- 5 条报警（251005x4 + 断连x1）：重复发单被拒，无实质影响。


## 📅 2026-03-24 (Automated Sync)
- **新增交易网关握手等待逻辑**：在登录成功后增加 3 秒等待，确保交易网关完成握手后再进行连接验证，提升连接稳定性（**miniQMT 启动模块**）。
- **优化已运行 miniQMT 的处理流程**：若检测到进程已在运行，则跳过启动直接验证连接，减少重复启动开销（**miniQMT 启动模块**）。
- **增强错误处理与日志输出**：在截图保存失败时静默处理，避免异常中断流程，同时完善各步骤的日志提示（**miniQMT 启动模块**）。
## 📅 2026-03-25 (T0 Fill-Based Accounting 重构)

### 背景

T0 引擎存在三个致命架构缺陷：
1. `reconcile_positions_with_real()` 启动时调用 `query_stock_positions` 读取**全账户**持仓，误把 T1 的 3000 股记入 T0 自己的底仓。
2. 主循环 `buy_save()` 和 `sell_save()` 在发单后立即乐观更新 `current_lots ±1`，不等实际成交。
3. 主循环多处（深渊阻断/孤儿检测/卖出保护/卖出量计算）直接调用 `query_stock_positions`，读到其他引擎持仓产生干扰。

### 三条强制物理定律

#### 物理定律 1：物理致盲
废除 `reconcile_positions_with_real` 对 `querystock_positions` 的调用。重写为零库存基准：

```python
def reconcile_positions_with_real(yaml_targets: dict) -> dict:
    state = {}
    for code, cfg in yaml_targets.items():
        state[code] = { "current_lots": 0, "volume": 0, ... }  # 强制零库存
    return state
```

**日内买多少，才有多少**。T0 永远从 0 开始。

#### 物理定律 2：Fill-Based 延迟记账
新增模块级全局变量：

```python
import threading
runtime_state: dict = {}
_t0_pending_lock = threading.Lock()
_t0_pending: dict[int, dict] = {}
PENDING_TIMEOUT_SEC = 60
PENDING_SWEEP_SEC   = 30
```

所有买卖单改为注册到 `_t0_pending`：

```python
seq = xt_trader.order_stock(acc, code, xtconstant.STOCK_BUY, ...)
if __name__ == '__main__':
    main()

## 📅 2026-03-31 (新增 auto_history_downloader.py — 历史成交下载 & 清洗)

### 变更文件：`auto_history_downloader.py`（新建）

- **[FEAT] 历史成交数据下载 + 清洗工具**
  - 从 QMT `query_history_trades` API 拉取指定日期范围成交记录
  - 合并分笔成交（按 `合同编号` groupby），VWAP 重算加权均价
  - 物理算费引擎：ETF(15x/51x/58x) 万0.5低消0.5，个股万0.5低消5.0
  - 输出 `utf-8-sig` CSV，Excel 可直接打开

- **[OPS] 零硬链接设计**
  - `QMT_PATH` / `ACCOUNT_ID` 全部从 `.env` 读取（`python-dotenv`）
  - 输出目录默认 `data/deal_reports/`，可通过 `.env` 中 `DEAL_REPORT_DIR` 覆盖
  - 无任何账号/路径硬编码在脚本中

- **[OPS] CLI 参数支持**（`argparse`）
  - `--start YYYYMMDD`：默认当月1号
  - `--end   YYYYMMDD`：默认今天
  - `--out   path.csv`：自定义输出路径

- **[OPS] QMT 连接规范对齐**
  - `start()` 后 `time.sleep(1)` 再 `connect()`（握手等待规范）
  - 订阅失败单独抛 `ConnectionError`（与其他执行器保持一致）
  - `close()` 安全包裹 `xt_trader.stop()`

### 用法
```powershell
# 下载当月（默认）
python auto_history_downloader.py

# 下载 3 月全月
python auto_history_downloader.py --start 20260301 --end 20260331

# 指定输出路径
python auto_history_downloader.py --start 20260301 --end 20260331 --out D:\reports\march.csv
```

### 语法验证
- `py_compile auto_history_downloader.py` → **Exit 0，零错误**
th _t0_pending_lock:
        _t0_pending[seq] = {"code": code, "direction": "buy", "qty": buy_qty, ...}
```

禁止在发单后立即修改 `current_lots`。

#### 物理定律 3：on_order_trade 回调驱动
升级 `GridTraderCallback.on_order_trade`：

```python
def on_order_trade(self, trade):
    global runtime_state
    with _t0_pending_lock:
        meta = _t0_pending.pop(trade.order_id, None)
    if meta:
        lots_delta = trade.traded_volume // 100
        if meta["direction"] == "buy":
            rs["current_lots"] += lots_delta
            rs["volume"]       += trade.traded_volume
        else:
            rs["current_lots"] = max(0, rs["current_lots"] - lots_delta)
            rs["volume"]       = max(0, rs["volume"] - trade.traded_volume)
        _save_runtime_state(runtime_state, STATE_FILE)
```

### 主循环改动汇总

| 位置 | 旧版 | 新版 |
|---|---|---|
| L857-862 深渊阻断 | `query_stock_positions` 读均价 | `rs['last_buy_price']` 内部账本 |
| L955-963 买入 `buy_save()` | 乐观 `current_lots += 1` | 注册 `_t0_pending` + `base_price` 更新 |
| L990-1008 买后实盘核实 | `time.sleep(3) + query_stock_positions` | **整块删除** |
| L1020-1035 孤儿清退 | `query_stock_positions` 查数量 | `rs.get('volume')` 内部账本 |
| L1082-1104 卖出前保护 | `query_stock_positions` + T+1 误判保护 | `rs.get('volume', 0) <= 0` 判断 |
| L1193-1199 常规卖出量 | `target_pos.volume` | `rs.get('volume')` 内部账本 |
| L1201-1205 `sell_save()` | 乐观 `current_lots -= 1` | 注册 `_t0_pending` |
| `tp_save / ab_save / eod_save / sl_save` | 全清乐观写 | 注册 `_t0_pending` |

### 新增：Pending 超时巡检

```python
def _t0_sweep_stale_pending(xt_trader, acc):
    """补丁C：扫描超时 pending 订单，主动对账并补记或废单回滚。"""
```
每 30 秒在主循环中调用，对超过 60 秒未成交的委托主动查询补记。

### 验证结果

- `python -m py_compile t0_multigrid_executor.py` → **Exit 0，语法零错误**
- `query_stock_positions` 在主循环中出现次数：**0** (完全消除)
- `_t0_pending` 注册次数：**7**（买入×1 + TP×1 + SL×2 + EOD×1 + 孤儿×1 + 常规卖×1）
- 文件总行数：1202 行（原 1384 行，精简后更清晰）

### 架构意义

T0、T1、FatFish 现在实现了**仓位物理隔离**：
- T1 用 `t1_grid_ledger.yaml` 的 `grid_inventory` 精确记录每格
- T0 用 `_t0_pending` 注册表 + `on_order_trade` 回调实现日内虚拟账本
- 双方均不查询 QMT 总持仓 → 彻底消除跨引擎踩踏的根源


## 📅 2026-03-25 (T0 Fill-Based 重构 + T1 账本一致性铁律)

### Bug 修复

**[BUG-1] T0 启动 API 报错：`query_account` 不存在** (`t0_multigrid_executor.py`)
- **故障现象**：T0 启动时潮汐资金池模块每日报 `[查询账户失败]`，导致资金杠杆不随现金动态调整，全天使用 YAML 固定配置。
- **根因**：`xt_trader.query_account(acc)` 方法在 miniQMT 中不存在。
- **修复**：改为 `xt_trader.query_stock_asset(acc)`，返回 `StockAsset` 对象，读取 `.cash` 字段。
- **写入 quant-safe-patterns §11**：API 命名陷阱对照表。

**[BUG-2] T0 溢价率/iopv 自动筛选废除** (`t0_multigrid_executor.py`)
- **背景**：QDII/日经/纳指等跨境 ETF 曾因 iopv 溢价被自动过滤。现策略改为人工审核写入 YAML，不再动态筛除。
- **修复**：删除 `CROSS_BORDER_KEYWORDS` + `iopv` + `PREMIUM_THRESHOLD` 整段筛选逻辑（原约 11 行）。
- **影响**：`fixed_t0_target.yaml` 中人工维护的标的完全生效，不再被程序覆盖。

**[BUG-3] 重复函数定义（代码结构污染）** (`t0_multigrid_executor.py`)
- **故障现象**：文件中存在两份 `_save_runtime_state` 和 `_get_trader_session` 定义（历史合并遗留），Python 以最后一份为准，导致第一份的改动静默失效。
- **修复**：物理删除两处重复定义，文件从 1192 → 1138 行。

**[BUG-4] T1 账本 `current_grid` 与实盘不符（乐观指针漂移）** (`t1_grid_executor.py`)
- **故障现象**：`_write_fill_to_ledger` 卖出后只更新 `available_shares`，未更新 `current_grid`。导致账本逻辑指针与物理库存逐渐漂移。
- **修复**：在 `_write_fill_to_ledger` 末尾增加 `rec["current_grid"] = _get_max_holding_grid(inventory)`，每次成交后从物理库存推导格数，消灭乐观指针漂移。

---

### 架构重构

**[ARCH-1] T0 Fill-Based 独立记账系统** (`t0_multigrid_executor.py`)
- 废除 `reconcile_positions_with_real()` 对 `query_stock_positions()` 的调用（物理致盲）
- 启动时强制 `current_lots = 0`（零库存基准）
- 下单后注册 `_t0_pending`，仅在 `on_order_trade` 回调按实际 `traded_volume` 入账
- 新增 `_t0_sweep_stale_pending`（30s 巡检，60s 超时）兜底缺失回调

**[ARCH-2] 双层并发锁** (`t0_multigrid_executor.py`)
- `_t0_pending_lock`：保护 pending 注册字典
- `_runtime_io_lock`：保护 `runtime_state` 字典内存修改 + `_save_runtime_state` 文件落盘
- `_save_runtime_state` 内部持锁，所有调用方自动受保护

**[ARCH-3] T1 启动账本一致性自检** (`t1_grid_executor.py`)
- 新增 `_check_and_heal_ledger()`：启动步骤 1b 自动检测三态一致性
- 可愈违规（`grid=0` 但 `avail≠0`，`current_grid` 与实际不符）自动修复并持久化
- 不可愈违规（slot 缺失）发送 Webhook 告警，要求人工介入

**[ARCH-4] T1 账本人工核对与修正**
- 对照 QMT 持仓截图修正 4 个标的的 `available_shares` 和 `current_grid`
- 首次写入 `grid_inventory` 字段（含实际买入价和每格数量）
- 159552（4 格 9300 股）通过交易历史反推各格成本：格 1-3 @ 2.167，格 4 @ 2.025

---

### 四大铁律（写入 `quant-v4-patterns §11`）

| 铁律 | 核心禁令 |
|---|---|
| 一：No Optimistic Updates | 发单后禁止立刻改账本，只能通过回调按 `traded_volume` 写入 |
| 二：Blind-Isolation | 多引擎共存时禁止调用 `query_stock_positions()` 查全局持仓 |
| 三：Symmetrical Liquidation | 卖出退格必须从 `grid_inventory.filled_qty` 读取，禁止按现价重算 |
| 四：Concurrency Locks | 账本文件 / pending 字典 / runtime_state 修改必须用 Lock 包裹 |

## 📅 2026-03-26 (T0 引擎 Inventory 抽屉式记账重构 v3)

### 背景：分笔成交导致账本彻底错乱

T0 引擎在实盘中遭遇灾难级 Bug：交易所将大单拆分分笔成交，导致基于全局计数器
（`current_lots` 和 `volume`）的账本彻底错乱，最终引发 EOD 尾盘清仓完全失效，
留下巨大过夜敞口。

### 三项强制重构（`t0_multigrid_executor.py`）

#### 第一项：Inventory 数据结构重构

废除 `runtime_state` 中的 `current_lots`、`volume`、`lot_qty` 字段，引入 `t0_inventory` 抽屉字典：

```json
"t0_inventory": {
  "1": {"buy_price": 1.065, "filled_qty": 10000, "status": "holding"},
  "2": {"buy_price": 1.060, "filled_qty": 23700, "status": "holding"}
}
```

- 新增辅助函数 `_inv_lots(rs)` 和 `_inv_volume(rs)` 替代原 `rs['current_lots']` 和 `rs['volume']`。
- 新增模块级计数器 `_t0_inv_slot_counter`，每日自增管理抽屉编号。
- `reconcile_positions_with_real` 初始化时强制 `t0_inventory = {}`（零库存基准）。

#### 第二项：on_order_trade 回调路由重写

- **买入**：读取 `pending` 中的 `grid_slot`，按槽号路由写入 `t0_inventory` 抽屉，`filled_qty` 分笔累加，绝不 pop/重置。
- **卖出**：按最老 `holding` slot 递减扣减（FIFO 原则）；`filled_qty` 归零时 `status` 置 `"sold"`。
- 仅当 `filled_so_far >= target_qty` 时才 pop pending 条目。

#### 第三项：主循环与 EOD 全链重构

| 位置 | 旧版 | 新版 |
|---|---|---|
| 买单下单 | 无 slot 概念 | 注入 `slot_id` 到 `pending["grid_slot"]` |
| 深渊阻断/TP/SL/孤儿清退/高抛 | `rs['current_lots'] / rs['volume']` | `_inv_lots(rs) / _inv_volume(rs)` |
| 常规高抛卖出量 | `rs.get('volume')` 全局值 | 从最老 holding slot 读取 `filled_qty` |
| EOD 强制清仓 | `14:55` 触发，按 `volume` 全量发单 | `14:52` 触发，遍历每个 holding 抽屉精准发单，发单后**强行归零 inventory** |
| `_t0_sweep_stale_pending` 补记 | 写 `current_lots / volume` | 按 `grid_slot` 路由写 `t0_inventory` |

### 设计铁律

- **EOD 必须强行归零 inventory**：EOD 发单后，无论回调是否到来，强行 `rs["t0_inventory"] = {}`，防止次日账本携带残留格数。
- **pending 绝不 pop**：分笔成交期间 pending 条目必须保留，否则后续分笔被当外部单忽略。
- **slot_id 唯一性**：每日 `_t0_inv_slot_counter` 从 0 开始自增，每次买单 +1，全天保证每格唯一。


## 📅 2026-03-26 (t0_master.py — spread_pct 钳制器重构 & 死代码清理)

### Bug 修复

#### [BUG-1] ATR_pct 列缺失 —— spread 计算完全乱 (`t0_master.py`)

- **故障现象**：`build_master_pipeline` 在计算 `spread_pct` 时，试图从 `df_res` 中取 `ATR_pct` 列，但该列从未被写入 `all_results`。
- **根因**：`row.get('ATR_pct', row.get('Total_Return', 0.02))`：fallback 到 `Total_Return`（回测总收益率，非波动率），用它乘以 ATR 乘数等于随机值。
- **修复**：
  - 新增 `_calc_atr14_pct(df)` 辅助函数（完全容错，异常返回 0.0，由上层回退）
  - 在 `all_results.append()` 中显式加入 `"ATR_14_pct": _calc_atr14_pct(df)`
  - 钳制器从 `row.get('ATR_14_pct', 0.0)` 精确读取，异常时回退 `_FALLBACK_SPREAD=0.012`

#### [BUG-2] spread_pct 负期望底线 (`t0_master.py`)

- **故障现象**：`MIN_T0_SPREAD = 0.004`（0.4%），双边万一佣金 + 单边 1-2 Tick 滑点 ≈ 0.2-0.3%，净盈利空间仅 0.1%，长期实盘给券商打工。
- **修复**：
  - `MIN_T0_SPREAD = 0.010`（1.0%，能击穿摩擦成本墙的真正底线）
  - `MAX_T0_SPREAD = 0.030`（3.0%，适配港股/商品 ETF 极端高波动）
  - 动态公式：`raw_spread = ATR_14_pct × 0.3`（锚定日均振幅 30%），然后钳制
  - `518680.SH`（黄金 ETF）硬编码也从崩溃级 `0.004` 升至 `MIN_T0_SPREAD = 0.010`

### 架构清理

#### [CLEAN-1] 删除 `get_safe_high_vol_etfs` 死函数 (~100 行) (`t0_master.py`)

- **背景**：该函数实现了 IOPV 溢价探针 + QDII 物理黑名单 + ATR 海选三步选股，但自从标的改为 `fixed_t0_target.yaml` 人工维护后，该函数从未被 `build_master_pipeline` 调用。
- **删除内容**：`FORBIDDEN_QDII_POOL`（6 标的黑名单）、IOPV 溢价过滤逻辑、独立 ATR resample 排名逻辑
- **原则确立**：标的改为人工维护后，所有自动过滤函数立即物理删除，不留死代码

### 架构意义

`t0_master.py` 现在实现了**完整的波动率嗅觉（Volatility Anchoring）**：
1. 回测时用同一份 1m DataFrame 顺便计算 ATR_14_pct（零额外 I/O）
2. 钳制器用真实日振幅 30% 作为基准切片，自动适配高低波标的
3. 底线保证每笔盈利能击穿摩擦成本，上线保证不被过度截断港股/商品 ETF 波动

## 📅 2026-03-27 (Rotation 动态子账户对账重构 — 极限清剿前精准确权)

### 事故背景

Rotation 引擎清仓 512890：账本记录 41300 股，QMT 实盘可用 41700 股（多 400 股历史遗留孤儿）。
代码无条件信任账本，发出 41300 股卖单，400 股残留过夜。
**根因：卖出循环直接读 `holdings[code]['qty']`，从未验证物理实盘。**

### 架构重构：动态子账户（Dynamic Sub-Account）对账逻辑

修改文件：`etf_rotation_executor.py`，新增两函数 + 改造卖出循环：

#### `_get_firewall_locked_shares(code)` → `int`

读取 T1 Grid（`t1_grid_ledger.yaml → available_shares`）+ Sniper（`sniper_holdings.json → qty`）账本锁定股数之和。T0 Grid 与 Rotation 通过排除名单隔离，不共享标的，故不参与减法。

#### `_reconcile_rotation_vol(xt_trader, acc, code, holdings)` → `int`

精准确权四步：
1. **物理探针**：`query_stock_positions → int(pos.can_use_volume)`
2. **防火墙减法**：`true_rotation_vol = can_use_volume - locked_by_others`
3. **边界拦截**：若 `true_rotation_vol < 0` → CRITICAL 日志 + 返回 0，禁止卖出
4. **账本强制覆写**：不一致时覆写内存 + YAML 落盘（消灭懒惰）

Warning 格式：`⚠️ [Rotation确权] 发现账本偏差 {code}: 账本={qty}股, 物理可处置={true_vol}股...`

#### 卖出循环改造（D 节）

```python
# 旧（信任账本）      qty = info.get('qty', 0)
# 新（物理确权）
true_rotation_vol = _reconcile_rotation_vol(xt_trader, acc, code, holdings)
# 下单传 true_rotation_vol，永不遗留碎股
```

### 四种边界场景

| 场景 | 行为 |
|---|---|
| `true == 账本qty` | 一致，正常执行 ✅ |
| `true > 账本qty`（孤儿多股，今日事故） | Warning + 覆写 + 卖出物理量 |
| `total_qmt_vol == 0`（Ghost 标的） | 跳过卖出，清除账本 |
| `true < 0`（防火墙超量，逻辑异常） | CRITICAL + 归零 + 禁止卖出 |

---

### Sniper 精筛拒绝归因计数器 (`sniper_entry_executor.py`)

**背景**：精筛结束后日志只有"精筛无命中"，无法判断是哪类过滤条件在大量杀票，是彻头彻尾的黑盒。

**改动**：在精筛循环外初始化 `rejection_stats`，三个过滤条件各自 +1，若 `scored` 为空则打印死亡归因统计。

```
📭 [Sniper 雷达] 精筛全军覆没，死亡归因：
   ⛔ 已封死涨停(无买点): 82 只
   ⛔ 逼近率不足(<60%): 45 只
   ⚠️ 涨停价数据无效: 4 只
```

| 过滤条件 | 归因 Key | 图标 |
|---|---|---|
| `up_limit <= 0 or up_limit <= pre_close` | 涨停价数据无效 | ⚠️ |
| `approach_rate <= 0.6` | 逼近率不足(<60%) | ⛔ |
| `ask1 <= 0 or ask1 >= up_limit` | 已封死涨停(无买点) | ⛔ |

---

### Sniper 预筛向量化 + 候选对账 CSV (`sniper_entry_executor.py`)

**痛点1 – 算力浪费**：旧预筛以 `gain >= 7%` 为门槛，主板 7% 逼近率约 70%，创业板 7% 逼近率仅 35%——大量创业板标的进入精筛后 100% 被 approach_rate 踢出，精筛阶段的 `get_instrument_detail()` 调用全部白费。

**痛点2 – 不可核对性**：精筛完毕无任何候选股数据留存，CEO 无法人工验证当日决策逻辑。

#### 改动1：废除 7% 涨幅预筛，改为向量化逼近率前置过滤

```python
# 根据代码前缀估算涨停幅（无需逐支调 API）
def _est_limit_pct(c: str) -> float:
    return 0.20 if c[:3] in ('300', '301', '688', '689') else 0.10

up_est = pre_close * (1 + _est_limit_pct(code))
if (last_price - pre_close) / (up_est - pre_close) >= APPROACH_RATE_THRESHOLD:
    pre_filtered.append(code)
```

效果：创业板标的逼近率 < 60% 在预筛就被剔除，精筛候选池大幅缩减，API 调用减少。

#### 改动2：精筛循环同步采集对账底稿，落地 CSV

- 每支候选股建立 `rec = {代码, 名称, 现价, 涨幅%, 逼近率, 换手率%, 动量分, 精筛结果}`
- 在每个 `continue` 处写入对应拒绝原因，通过精筛的写 `'通过精筛'`
- 循环结束后写入 `logs/{YYYYMMDD}_sniper_candidates.csv`（UTF-8-BOM，Excel 可直开）
- 无论命中与否都落盘

```
✅ [Sniper对账] 已导出候选股详细数据至 20260327_sniper_candidates.csv，共 N 只，请手动验证数据。
```

**设计铁律**：对账 CSV 由精筛函数（`build_ranked_candidates`）内部自己写盘，与调用方解耦，不依赖外部调度。


## 📅 2026-03-30 (Automated Sync)
- **T0 执行器（`t0_multigrid_executor.py`）**：优化了持仓对账逻辑，在平仓时按顺序扣除已成交的持仓格数量，并更新状态为“sold”，确保持仓数据准确。
- **T1 执行器（`t1_grid_executor.py`）**：新增了异常处理与资源清理机制，在程序崩溃或手动中断时释放进程锁、停止交易连接，并发送 Webhook 告警。
- **T0 执行器（`t0_multigrid_executor.py`）**：增强了对未成交订单的废单回滚处理，避免无效订单影响后续执行。
- **T1 执行器（`t1_grid_executor.py`）**：引入了命令行参数 `--dry-run` 支持测试模式，允许只打印信号而不实际下单。
- **T0 执行器（`t0_multigrid_executor.py`）**：改进了运行时状态保存机制，在对账完成后立即持久化状态到文件，提升容错性。

## 📅 2026-03-30 (物理价差锁 — Bullet Wasting Bug 根治)

### 背景：子弹连发漏洞 (Bullet Wasting Bug)

实盘发现 `159502.SZ` 于 09:34:02 建 Grid 1 @ 1.229，仅 15 秒后 Grid 2 同样以 1.229 建仓，两格价差为零，完全违背「空间换成本」数学第一性原理。根因：价格跌穿多格触发线后，系统仅以是否满足静态触发价为准，不检验价格是否与上一格成交价拉开了足够空间。

### 架构改造：物理价差锁（Dynamic Price Spacing Lock）

核心法则：**下一格买入触发线 = min(原静态触发线, 当前最低持仓成交价 x (1 - step))**

新增辅助函数：
- `t1_grid_executor.py` — `_get_min_holding_price(inventory)`：遍历 grid_inventory 所有 holding 槽返回最低 buy_price，零持仓返回 inf
- `t0_multigrid_executor.py` — `_t0_get_min_holding_price(rs)`：遍历 t0_inventory 所有 holding 槽返回最低 buy_price，零持仓返回 inf

T1 主循环（L1100 区域）：在 effective_buy_trigger 计算后插入动态天花板拦截，价差不足时打印 `🔒 [价差锁]` 日志并收紧触发线。

T0 主循环（L910 区域）：在买入条件前计算 `_t0_effective_rail = min(静态下轨, 动态天花板)`，替换原条件。

今日场景回放验证：Grid 1 @ 1.229 成交后，dynamic_ceiling = 1.229*(1-0.014589) = 1.2111，15 秒后价格仍为 1.229 > 1.2111，Grid 2 被正确拦截。

铁律合规：四大铁律全部满足，改动仅修改触发条件，不碰任何写账路径，零线程竞争。

语法验证：`py_compile` T1 OK / T0 OK


## 📅 2026-03-31 (WOL 冷启动故障修复 + T0 日志系统重构)

### 变更文件：`t0_multigrid_executor.py`

- **[BUG] STATE_DIR 相对路径导致 PermissionError [WinError 5]**
  - **根因**：`STATE_DIR = ".state"` 是相对路径。autopilot_master 以不同 CWD 启动时，makedirs 触发权限拒绝。
  - **修复**：改为基于 `__file__` 的绝对路径，与 logs/ 目录处理方式一致。
  - **影响**：无论从哪里调用，进程锁和状态文件路径固定为脚本所在目录。

- **[BUG] 日志系统三阶段修复（Hidden 窗口模式下日志全空）**
  - **根因**：autopilot_master 用 -WindowStyle Hidden 启动 T0，stdout 被静默丢弃；主循环大量 print() 无法写入文件；历史 PowerShell redirect 留下占位空文件导致句柄竞争。
  - **修复 1**：日志文件名加入启动时间戳（HHMMSS），每次重启产生全新文件。
  - **修复 2**：run_multi_grid() 入口第一行加 log() 激活 FileHandler。
  - **修复 3**：劫持 builtins.print，将所有 print() 路由到 logging.info()（见下方 PATTERN）。
  - **影响**：巡逻明细、心跳、潮汐资金池等日志恢复正常写入。

- **[OPS] 进程锁 TTL 从 600 秒减少到 300 秒**
  - **根因**：WOL 冷启动 crash 后孤儿锁让重启等待 10 分钟。
  - **影响**：孤儿锁最多 5 分钟自动粉碎。

### 变更文件：`start_miniQMT.py`

- **[BUG] WOL 冷启动 buy.png 图像检测假阴性**
  - **根因**：15 秒等待不足（冷启动需 30-60 秒）；分辨率不一致导致 needle>haystack 异常被误判为未登录。
  - **修复 1**：静默启动等待 15s → 45s。
  - **修复 2**：图像检测失败时回退 _test_connection() API 验证，API 通则视为成功。
  - **影响**：autopilot_master 不再因分辨率失配误报并中止后续任务。

---

## 🔷 新设计模式（PATTERN）：print → logging 全局劫持

适用场景：现有代码大量 print()，需在 Hidden 模式下保持日志完整性，不逐行改代码。

实现（放在 log() 函数定义之后）：

  import builtins as _builtins
  _orig_print = _builtins.print
  def _print_to_log(*args, **kwargs):
      msg = " ".join(str(a) for a in args)
      logging.info(msg)
      kwargs.pop('file', None)
      _orig_print(*args, **kwargs)
  _builtins.print = _print_to_log

效果：所有 print() 自动同时写 FileHandler，Hidden 模式下日志不丢失，无需改任何业务代码。
已应用于：t0_multigrid_executor.py（2026-03-31）


## 📅 2026-03-31 (T0 物理价差锁双重 Bug 修复)

### 变更文件：`t0_multigrid_executor.py`

- **[BUG] safe_spread_pct 被 ATR 压低绕过 YAML 配置（主犯）**
  - **根因**：原代码 `safe_spread_pct = max(0.005, min(0.012, raw_atr * multiplier))`，
    当 raw_atr 极小时（盘中窄幅），结果被下限钳制到 0.5%，
    远低于 YAML 配置的 spread_pct（如 159506 = 1.04%）。YAML 字段实际完全被无视。
  - **修复**：`safe_spread_pct = max(yaml_spread_pct, raw_spread_pct)`
    以 YAML spread_pct 为硬下限，ATR 动态值只能上浮不能下压。
  - **影响**：159506 的 safe_spread_pct 保证 >= 1.04%，不会因盘中窄幅被压到 0.5%。

- **[BUG] pending 状态下 _t0_get_min_holding_price 返回 inf（从犯）**
  - **根因**：买单提交后，t0_inventory 需等 on_order_trade 回调才更新。
    若回调慢（需 Sweep 补记），下一格评估时 inventory 为空，
    _t0_get_min_holding_price 返回 inf，价差锁完全失效，
    实际只靠已被压低的 safe_spread_pct 守门 → 两格几乎同价建仓。
  - **修复**：_t0_get_min_holding_price v2 同时查 _t0_pending 队列：
    在 pending 中找到同 code 的 buy 订单，将其 buy_price 也纳入锚点计算。
  - **前提**：在 pending 注册时必须记录 buy_price（已同步修复）。
  - **辅助**：在主循环写入 rs['_code_ref'] = code，供函数跨域识别标的。
  - **影响**：提交买单后立即锁定价格锚点，无论回调是否到来，下一格都会被正确拦截。

### 事故复盘（159506，2026-03-31 早盘）

- 两笔买入价差仅 0.15%（1.324 → 1.322），远低于配置的 1.04%
- 根因：双 Bug 叠加 → safe_spread_pct 被压至 0.5% + pending 时 min_price=inf
- 修复后验证公式：slot_1 @ 1.324 成交后，
  下一格触发线必须 < 1.324 * (1 - 0.0104) = 1.3102，
  1.322 > 1.3102 → 会被正确拦截。



## 📅 2026-03-31 (Automated Sync)
*   **AutoPilot 主循环自愈熔断机制** (`autopilot_master.py`)
    *   新增崩溃重启次数上限 (`MAX_SELF_RESTART`)，超出后停止自拉起并发送熔断告警，需人工干预。

*   **miniQMT 启动流程优化** (`start_miniQMT.py`)
    *   增加交易网关握手等待时间，提升连接稳定性。若进程已运行，则跳过启动直接验证连接。

*   **T0 网格执行器持仓对账逻辑增强** (`t0_multigrid_executor.py`)



## 📅 2026-03-31 (Automated Sync)
*   **AutoPilot 主循环自愈熔断机制** (`autopilot_master.py`)
    *   新增崩溃重启次数上限 (`MAX_SELF_RESTART`)，超出后停止自拉起并发送熔断告警，需人工干预。

*   **miniQMT 启动流程优化** (`start_miniQMT.py`)
    *   增加交易网关握手等待时间，提升连接稳定性。若进程已运行，则跳过启动直接验证连接。

*   **T0 网格执行器持仓对账逻辑增强** (`t0_multigrid_executor.py`)
    *   在成交对账 (`sweep`) 时，新增对 `runtime_state` 中持仓记录的同步扣减和状态更新 (`holding` -> `sold`)。

*   **T1 网格执行器退出流程完善** (`t1_grid_executor.py`)
    *   在 `finally` 块中确保交易连接 (`xt_trader`) 被安全停止，并释放进程锁，提高退出时的资源清理可靠性。

*   **交易清算支持 CSV 手工模式** (`trade_settlement.py`)
    *   新增 `--csv` 命令行参数，允许手动传入成交 CSV 文件路径进行离线清算，增强调试和应急处理能力。

## 📅 2026-03-31 (T0 硬盘级持久化与断点续传 — State Persistence & Resumption)

### 变更文件：`t0_multigrid_executor.py`

- **[ARCH] 引入 `.state/t0_ledger.json` 专属账本（步骤1：文件定义）**
  - **根因**：t0_inventory 完全依赖 Python 内存，进程崩溃后重启从 `{}` 开始，盘中持仓瞬间成为"孤儿"，14:52 EOD 清仓逻辑彻底失效。
  - **修复**：新增常量 `T0_LEDGER_FILE = os.path.join(STATE_DIR, "t0_ledger.json")`，生命周期：启动时加载 → 每次成交后更新 → EOD 清仓后物理销毁。

- **[FEAT] `_save_t0_state()` 实时落盘钩子（步骤2：IO Save Hook）**
  - 将 runtime_state 中所有标的的 `t0_inventory` + `slot_counter` + `base_price` 快照写入 `t0_ledger.json`。
  - 使用 **原子写入模式**（先写 `.tmp` 再 `os.replace()`），防止进程崩溃时写到一半导致文件损坏。
  - 触发时机：`on_order_trade` 回调中每次 inventory 实质性变更后立即调用（每笔成交后实时落盘）。
  - 线程安全：内部持 `_runtime_io_lock`，与 `_save_runtime_state` 同级保护。

- **[ARCH] `reconcile_positions_with_real()` 升级为 v4（步骤3：满血复活启动逻辑）**
  - **废除**「每日启动无脑 `t0_inventory = {}`」行为。
  - **新逻辑**：启动时检测 `t0_ledger.json` → 解析格式合法性 → 仅恢复 `status=holding` 的抽屉（剔除已平仓记录，防重复卖出） → 同步恢复 `slot_counter`、`base_price`、`last_buy_price`。
  - **降级安全**：文件不存在、格式损坏、读取异常时均静默降级为零库存基准，不影响正常启动。

- **[FEAT] `_purge_t0_ledger()` EOD 物理销毁（步骤4：EOD Purge）**
  - EOD 弹层清仓发单完毕、`t0_inventory` 强行归零后立即调用，物理删除 `t0_ledger.json`（含 `.tmp` 临时文件）。
  - 确保次日系统以绝对干净的零库存状态重启，不携带任何过期抽屉记录。
  - 幂等安全：每个有持仓的标的 EOD 处理后各自调用一次，多次调用无副作用。

### 风险场景验证

| 场景 | 旧版行为 | 新版行为 |
|---|---|---|
| 14:00 断电重启 | t0_inventory={} 孤儿失控 | 从 ledger 恢复抽屉，14:52 EOD 正常清仓 |
| 正常收盘 | 账本随意留存 | EOD 后 purge，次日干净启动 |
| 崩溃时写了一半 | 文件损坏，解析 Exception | .tmp 原子替换，原文件完好，降级零库存 |
| ledger 格式异常 | — | 静默忽略，零库存启动，警告日志提示 |

### 语法验证
- `py_compile t0_multigrid_executor.py` → **Exit 0，零错误**

---

## 📅 2026-03-31

### [ARCH] 历史成交 API 探活全记录 & 每日结算重构

XtQuantTrader 历史成交 API 探活路径：

| 轮次 | 尝试 | 失败原因 |
|---|---|---|
| 1 | `query_history_trades` | 方法不存在 |
| 2 | `query_stock_trades(acc)` | 仅返回当日 |
| 3 | `query_stock_orders(start_time=epoch)` | 真实签名无日期参数 |
| 4 | `query_data('deal', epoch_start, epoch_end)` | 支持跨日但券商侧限制仍返今日 |

**结论**：国金 miniQMT 物理限制，无从 API 获取历史成交。

### [FEAT] daily_trade_settlement.py

放弃批量拉历史，改为每日 17:30 下载当日成交：
- 路径规范：`data/deal_reports/YYYYMM/deals_YYYYMMDD.csv`
- 分笔合并 VWAP + 物理算费
- autopilot_master 17:30 自动触发 `task_daily_trade()`

### 变更文件
- `daily_trade_settlement.py` — [FEAT] 新建
- `autopilot_master.py` — [FEAT] 注册 DAILY_TRADE_SCRIPT + 17:30 调度

## 📅 2026-03-31 (23:00 定时关机失效 — 诊断修复)

### 故障现象
- 23:17 用户报告：23:00 定时任务触发了，但机器未休眠/关机。
- pilot.log 显示 23:00:13 有两条"定时休眠指令已发出"日志（说明双实例运行中）。

### 根因分析

#### [BUG] `shutdown /h` 静默失败
- `os.system("shutdown /h")` 在系统未启用休眠（hiberfil.sys 不存在）时直接返回非零码，但 `os.system` 不检查返回码，导致静默失败、机器继续运行。
- `powercfg /a` 验证确认：系统只有 S1 Standby，无 S4 Hibernate 条目。

#### [INFO] 双实例问题
- pilot.log 每条消息出现两次，说明 autopilot 同时有两个实例在运行。
- 两个实例均各自触发了 shutdown，互相竞争，最终都静默失败。

### 修复方案 (`autopilot_master.py`)

**`task_shutdown()` 重构为两阶段 fallback：**

1. **阶段1**：`subprocess.call(["cmd", "/c", "shutdown /h"])` — 优先休眠，保留登录态，方便 WOL 唤醒
2. **阶段2（Fallback）**：若 `rc_h != 0`（休眠不可用）→ 改发 `shutdown /s /t 60`（60秒倒计时普通关机）
3. **兜底告警**：若关机也失败，发送 N8N 紧急 Webhook 要求人工干预
4. 所有路径均有 `log.info/warning/error` 记录，再无静默失败

**同步执行：管理员身份运行 `powercfg /hibernate on` 启用系统休眠功能（已执行）。**

### 变更文件
- `autopilot_master.py` — [BUG] `task_shutdown()` 从 `os.system` 改为 `subprocess.call` + 双阶段 fallback + N8N 告警

### 语法验证
- `py_compile autopilot_master.py` → **Exit 0，零错误**

## 📅 2026-04-01 (Architecture — Phase-out Liquidation 僵尸清道夫)

### 变更文件：`t1_grid_executor.py`

- **[FEAT] T1 全局资金总阀 `T1_TOTAL_CAPITAL = 200_000`**
  - 根因：4月换仓后，20w 资金池上限需在代码中明确可见、手动可调
  - 修复：顶部常量区新增 `T1_TOTAL_CAPITAL`（手动每月更新），与 Phase-out 释放资金日志联动
  - 影响：阀门位于文件开头，运维时无需深入代码

- **[FEAT] Phase-out 双重熔断参数**
  - 新增 `PHASEOUT_MAX_HOLD_DAYS = 20`（最大容忍持仓自然日）
  - 新增 `PHASEOUT_MAX_DRAWDOWN = 0.15`（最大单边下行容忍度 -15%）
  - 新增订单标识 `ORDER_PHASEOUT = "T1_PhaseOut"`

- **[ARCH] `_run_phaseout_scan()` 僵尸清道夫函数**
  - 根因：月度换仓后老标的（不在新白名单）长期横盘/阴跌，永久占用 20w 额度
  - 逻辑：遍历账本，识别"不在 active_candidates 但有 holding 仓位"的老标的
    - 首次发现：写入 `phaseout_watch_since` 字段，激活留党察看
    - 时间熔断：`(today - watch_since).days >= 20`
    - 空间熔断：`last_price <= min_cost * (1 - 0.15)`
    - 任一触发 → `query_stock_positions` 获取物理真实数量（V4.0 物理清仓铁律 §2）→ 买一价强平
    - 账本销毁：所有 holding slot → `force_closed`，`current_grid/available_shares` 归零
  - 日志：`🗑️ [僵尸清道夫] 老标的 XXXX 触发(时间/空间)熔断，已强平释放资金 X 元`
  - Webhook 通知：发送 Phase-out 触发报警

- **[FEAT] 行情订阅扩展（启动阶段）**
  - 根因：清道夫扫描需要老标的的实时 Tick，但老标的不在白名单中
  - 修复：启动时扫描账本，将所有有 holding 的标的（包含老标的）纳入 `xtdata.subscribe_quote`
  - 影响：老标的清道夫判定有实时价格，不会因 Tick 无效跳过

- **[FEAT] 主循环集成（步骤 5b2）**
  - 在每轮账本+防火墙加载（5a/5b）之后、逐标的处理（5c）之前调用 `_run_phaseout_scan()`
  - 清道夫触发后重新加载账本（`ledger = load_ledger()`），确保后续白名单标的读到最新状态

### 架构意义

T1 从此具备完整的新陈代谢能力：
- **新标的**：按正常网格吃波动，享受资金优先权
- **老标的**：进入"留党察看期"（`phaseout_watch_since` 起始），20天内若无反弹自救，或跌破 -15%，直接强行拔管，资金归还给 20w 流动池

### 语法验证
- `py_compile t1_grid_executor.py` → **Exit 0，零错误**

## 📅 2026-04-01 (Architecture — Fat Fish 信号与落盘物理隔离重构)

### 变更文件：`fat_fish_master.py` + `fat_fish_executor.py`

- **[BUG] 状态机过早提交（Premature State Commit）**
  - **根因**：大脑（`fat_fish_master.py`）在计算出买入候选后，直接将 `current_slots[code] = {...}` 写入内存并通过 `save_state()` 落盘 `fat_fish_slots.yaml`。导致即使火炮（执行器）尚未发单，防火拦手（`fat_fish_guard.py`）已读到「已持仓」标的，形成**幻影账本**，火炮和防线均误判为已建仓，拒绝真实开仓。
  - **影响**：每日 14:40 大脑扫描出突破信号后，14:50 火炮醒来发现槽位已占，跳过所有买入，资金永久闲置。

- **[ARCH] 信号与落盘物理隔离（Signal/Commit Separation）**

  #### 核心架构变更

  | 环节 | 旧版（Bug）| 新版（正确）|
  |---|---|---|
  | 大脑扫描到买入候选 | 直接写 `fat_fish_slots.yaml` | 只写 `.state/fat_fish_signals.json` |
  | 大脑落盘 `save_state()` | 含新槽位 | `save_slots_only()`：仅更新棘轮止损线 |
  | 火炮买入阶段 | 读 `orders.yaml` 的 `buy` 列表 | 读 `fat_fish_signals.json` 信号文件 |
  | 槽位写入时机 | 大脑扫描时（无成交保证）| **`seq > 0` 发单成功后**，火炮调用 `save_slots()` |
  | 执行完毕 | orders 文件归档 | orders 归档 + `purge_signals()` 阅后即焚 |

  #### 新增文件/函数

  **`fat_fish_master.py`**：
  - 新增 `SIGNALS_FILE = .state/fat_fish_signals.json`
  - 新增 `save_signals(signals: list)` — 原子写（`.tmp → os.replace`），大脑唯一合法输出
  - `save_state()` → 拆分为 `save_slots_only()`（只写棘轮更新）+ `save_orders()`（卖出指令）
  - 买入候选块：禁止修改 `current_slots`，改为 `buy_signals.append()`→`save_signals()`

  **`fat_fish_executor.py`**：
  - 新增 `SLOTS_FILE`、`SIGNALS_FILE` 路径常量
  - 新增 `load_signals()` — 读取大脑信号
  - 新增 `load_slots()` / `save_slots()` — 原子写槽位（火炮专属）
  - 新增 `purge_signals()` — 阅后即焚，防次日重复触发
  - 买入阶段：`for signal in buy_signals` 替代原 `for order in buy_orders`
  - 槽位防火墙：`if code in current_slots: continue`（防重复信号）
  - `seq > 0` 条件内才调用 `save_slots()`，拒单时不写账本
  - `write_probe` 变量修正：`buy_orders → buy_signals`

- **[PATTERN] 信号-落盘二段提交模式（Signal-Commit Two-Phase Pattern）**
  - 适用场景：任何"计算信号"与"实盘落单"分两个进程/时间执行的策略
  - 规则：计算进程只写轻量中间信号文件（JSON），执行进程成功发单后才写重量状态机文件（YAML）
  - 信号文件使用原子写（`.tmp + os.replace`），防止执行器读到半写文件

### 语法验证
- `py_compile fat_fish_master.py` → **Exit 0，零错误**
- `py_compile fat_fish_executor.py` → **Exit 0，零错误**


## 📅 2026-04-01 (Automated Sync)
*   **新增自愈熔断机制** (`autopilot_master.py`)
    *   主循环崩溃后，在达到最大重启次数上限 (`MAX_SELF_RESTART`) 后将停止自拉起并发送熔断告警，防止无限重启。

*   **优化历史数据下载器 CLI 接口** (`auto_history_downloader.py`)
    *   为 `--start` 和 `--end` 参数设置了更合理的默认值（当月1号和当天日期），提升了工具易用性。

*   **完善每日结算工具的输出路径处理** (`daily_trade_settlement.py`)
    *   当输出CSV文件路径被占用时，自动在文件名后添加时间戳后缀，避免文件覆盖。

*   **强化胖鱼策略的指令归档与清理** (`fat_fish_executor.py`, `fat_fish_master.py`)
    *   执行器 (`fat_fish_executor.py`) 在指令执行完毕后，将订单文件归档至历史目录并物理清空信号文件，防止次日重复读取。
    *   主脑 (`fat_fish_master.py`) 明确区分了“买入信号”（待执行）和“卖出指令”（立即执行）的落盘逻辑，职责更清晰。

*   **修复T0网格持仓对账逻辑** (`t0_multigrid_executor.py`)
    *   在订单成交对账 (`_sweep_pending`) 时，修正了从运行时状态 (`runtime_state`) 中扣除已成交数量的逻辑，确保持仓数据准确。

## 📅 2026-04-02 — Sniper 涨停价脆弱 API 物理兜底修复

### 变更文件：sniper_entry_executor.py

**[BUG] sniper_candidates.csv 逼近率、动量分大面积 NA**
- **故障现象**：20260402_sniper_candidates.csv 中所有进入精算阶段的标的，逼近率与动量分列均为空字符串，标注为"涨停价数据无效"被整批丢弃。
- **根因**：阶段二精算循环调用 xtdata.get_instrument_detail(code).get('UpLimit', 0) 获取涨停价，但该 API 在盘中返回 0 或无效值（小于昨收价），导致判断成立后整批标的被跳过，逼近率/动量分字段永远留空 ''。
- **关键矛盾**：阶段一（预筛）用前缀 _est_limit_pct() 估算可通过，阶段二（精算）依赖 UpLimit API 字段直接崩溃，两阶段数据源不一致。

**[BUG] 修复方案：calculate_limit_up_physical() 物理兜底函数（新增）**
- 300/301/688/689 前缀：x 1.20（创业板/科创板 20%）
- 60/00 前缀：x 1.10（主板 10%）
- 其他默认：x 1.10；严格 ound(..., 2) 四舍五入，与交易所一致
- **注入逻辑**：API 有效时优先用，API 无效（返回 0 或 <= 昨收价）时立即切换物理推算，拒绝 NA 污染
- **验证**：py_compile → **Exit 0**（SafeToAutoRun，自动执行）

## 📅 2026-04-02 — ETF 轮动信号机：绝对动量政审重构

### 变更文件：etf_rotation_master.py

**[ARCH] 彻底废除多层逐标的 veto，改为全量评分 + Top 1 绝对动量政审**

#### 旧逻辑架构（已废弃）
- 遍历 SYMBOL_POOL 时，每只标的独立经历三层关卡：
  1. last_price < ma120 → 淘汰（Below MA120）
  2. last_price < latest_kama → 淘汰（Below KAMA）
  3. et_20d < 0 → 淘汰（Negative Momentum）
- 通过三关的候选才进 stats_list，再取 TOP 2，不足则以 SAFE_ASSET 补位。
- **问题**：各标的独立过关，不关心「最强标的是否也陷入熊市」这一全局信号。

#### 新逻辑架构（v2 全量政审）

**第一阶段：构建全量融合数据映射（不做剔除）**
- 全部 SYMBOL_POOL 成员完整计算 ma120 / kama / score / ret_20d
- 全部落入 latest_data_map + stats_list，日志仅做诊断性标注（✅ 趋势健康 / ⚠️ 跌破半年线），不触发 continue

**第二阶段：绝对动量政审（一刀切）**
`python
df_rank  = pd.DataFrame(stats_list).sort_values(by='score', ascending=False)
top_code = df_rank.iloc[0]['code']   # 动量最强者
top_data = latest_data_map[top_code]

if top_data['last_price'] < top_data['ma120']:
    # 最强者尚且跌破半年线 → 全球风险资产均在衰退通道
    selected = [SAFE_ASSET]          # 100% 推入国债
else:
    selected = [top_code]            # 拥最强者击发
`

**设计意图**：
- 原逻辑是"先过滤，再选优"→容易在震荡市中得到 0 个有效候选，被动退避国债
- 新逻辑是"先选最强，再政审"→ 主动判断：「连第一名都跌破半年线」才是真熊市信号
- TARGET_COUNT 常量已废弃（满仓押注 Top 1，不再双标两选）

**[OPS] 顺带清理 TARGET_COUNT 常量引用**
- 旧代码：selected = df_rank.head(TARGET_COUNT)['code'].tolist()
- 新代码：直接取 df_rank.iloc[0]['code']，逻辑更清晰

**验证**：py_compile etf_rotation_master.py → **Exit 0**（自动执行）

## 📅 2026-04-02 — ETF 手续费前缀漏洞修复

### 变更文件：uto_history_downloader.py、daily_trade_settlement.py

**[BUG] 520xxx 系列沪市 ETF 手续费被误判为个股（最低 5 元 vs 正确 0.5 元）**
- **故障现象**：deals_20260402.csv 中 520500.SH 全部 6 笔手续费为 5.0，正确应为  .5（ETF 最低消费）。520500.SH（中证500ETF）、520510.SH、520780.SH 均为沪市 ETF，每次误差 4.5 元，当日虚增手续费共 31.5 元。
- **根因**：两文件中 ETF_PREFIXES = ('15', '51', '58') 漏掉了 '52' 前缀。深市 ETF 为 15x，沪市 ETF 为 51x / 52x / 58x，代码只包含了部分沪市前缀。
- **修复**：两文件同步追加 '52'，最终 ETF_PREFIXES = ('15', '51', '52', '58')，并加注释防再次遗漏。
- **数据修复**：运行一次性修正脚本，将 deals_20260402.csv 中 7 行 520500.SH 记录从 5.0 修正为正确的 ETF 费率（均为  .5，因成交额 ~9600 元时万0.5 = 0.48，触发最低消费 0.5）。
- **验证**：py_compile 两文件 → **Exit 0**；CSV 目视复核，所有 520500.SH 手续费已显示  .5。

**[OPS] quant-safe-patterns 新增 §14（待补录）**
- ETF 代码前缀对照表：15x(深市) / 51x / 52x / 58x(沪市)，52x 是最常见的漏网之鱼

## 📅 2026-04-08 — N8N 推送静默 Bug 双引擎修复

### 变更文件：`sniper_exit_guard.py`、`t0_multigrid_executor.py`

**[BUG] sniper_exit_guard.py 完全缺失 N8N 推送能力**
- **故障现象**：Sniper 平仓（止盈/止损/时间死线）触发卖单后，N8N 无任何推送，无法实时感知 Sniper 成交情况；盘中对账发现账本0股 vs 实盘持仓的异常（520500.SH +5700股、159615.SZ +9100股），推送静默导致无法及时定位。
- **根因**：`sniper_exit_guard.py` 从未 `import requests`，也没有定义 `send_n8n_alert` 函数，也没有读取 `N8N_WEBHOOK_URL` 环境变量。下单成功后只调用了 `record_action`（写本地动作日志），完全没有远程推送路径。
- **修复**：
  1. 添加 `try/except import requests`（兼容未安装时静默降级）
  2. 从 `.env` 读取 `N8N_WEBHOOK_URL`
  3. 新增 `send_n8n_alert(title, message)` 函数（失败静默，与 sniper_entry_executor 保持一致）
  4. 在 `triggered` 卖单下发后立即调用，推送标的代码、名称、触发原因、卖出价、成本价、浮盈% 和数量
- **影响**：Sniper 平仓信号现在实时可见，与 Entry 信号对称

**[BUG] t0_multigrid_executor.py 误将 Sniper 成交报告为"T0 外部成交"**
- **故障现象**：Sniper 的卖单成交后，T0 的 `on_order_trade` 回调会收到该笔成交（共用同一 QMT 账户），因为 `_t0_pending` 中没有对应记录，走 `meta is None` 分支，发出"🤖 T0 外部成交"推送，信息错误且与 Sniper 自推重复。
- **根因**：`meta is None` 分支无差别对所有非 T0 委托打印"外部成交"并推送 N8N，未区分策略归属。
- **修复**：通过 `getattr(trade, 'strategy_name', '')` 读取委托的策略标识，识别 `sniper`/`etf_rota` 等非 T0 策略，走"过路"分支（仅记本地日志，不重复推 N8N）；真正的手动单或未知策略仍发"外部成交"推送。
- **影响**：消除推送重复/误报，N8N 频道内容更精准

**验证**：
- `py_compile sniper_exit_guard.py` → **Exit 0**
- `py_compile t0_multigrid_executor.py` → **Exit 0**

## 📅 2026-04-09 — Oracle Shadow Tester 无响应修复

### 变更文件：`oracle_shadow_tester.py`

**[BUG] `download_history_data2` 永久阻塞 — 脚本无响应死锁**
- **故障现象**：`python oracle_shadow_tester.py` 运行后无任何输出，进程永久挂起（实测超过 2 分钟无响应）。
- **根因**：`xtdata.download_history_data2([code], period='1d')` 是阻塞调用，没有任何内建超时机制。若 miniQMT 客户端未登录或服务器端未响应，该调用会无限等待，导致主线程死锁。
- **修复（三层物理防护）**：
  1. **连接预检**（`_check_qmt_alive`）：脚本启动时先用 `get_market_data_ex` 对单只标的发起轻量探测，若 5 秒内无响应则立刻打印错误并 `return`，拒绝继续执行。
  2. **Download 线程熔断**（`_download_with_timeout`）：将每个标的的 `download_history_data2` 包裹在 `daemon=True` 的线程中，`thread.join(timeout=5.0)` 超时后放弃，不阻塞主线程。
  3. **降级策略**：下载超时的标的跳过，直接尝试读取本地缓存（`get_market_data_ex`），保证脚本在 QMT 无法下载新数据时仍可读取历史 K 线。
- **验证**：`py_compile oracle_shadow_tester.py` → **Exit 0**

**[PATTERN] xtquant 阻塞调用必须线程包裹**
- `download_history_data*` 系列、`get_full_tick`（网络慢时）均为潜在阻塞点，在独立工具脚本中调用时必须用 `threading.Thread(daemon=True) + join(timeout=N)` 物理熔断。

## 📅 2026-04-09 — oracle_test.py xtdata 死锁修复

### 变更文件：`oracle_test.py`（修复）

**[BUG] download_history_data2 无超时，QMT连接时永久阻塞**
- **故障现象**：脚本运行 1m13s+ 无任何输出，完全挂起。
- **根因**：原代码直接在主线程循环调用 `xtdata.download_history_data2([code], period='1d')`，该 API 无内置超时机制，QMT C/S 连接缓慢时永久挂起。
- **修复**：移植 `oracle_shadow_tester.py`（2026-04-09 已验证）的三重防护模式：
  1. **QMT 连接预检**（`_check_qmt_alive`）：用极轻量 `get_market_data_ex(count=1)` 探测连接，5s 超时，离线则快速失败。
  2. **Download 线程熔断**（`_download_with_timeout`）：逐标的包裹 `daemon=True` 线程 + `join(timeout=5.0)`，超时跳过不阻塞。
  3. **降级读本地缓存**：下载超时标的直接读 `get_market_data_ex` 本地数据，保证 7 只宏观标的均能获取。
- **同时修复**：Exception 细化，将 `requests.exceptions.Timeout` 单独捕获以区分"预言机超时"和"其他连接错误"。
- **验证**：`py_compile oracle_test.py` → Exit 0；实际运行输出：
  - ✅ miniQMT 在线确认
  - 3 只标的 download 超时降级本地缓存（正常，盘后无行情服务器）
  - 7 只标的均成功获取数据并推送预言机
  - 预言机返回：518880.SH（黄金）赔率 4.28，判决【主升浪攻击】

## 📅 2026-04-09 — tools/refine_core_universe.py 全自动 ETF 星图采集器

### 变更文件：`tools/refine_core_universe.py`（新建 + 三项修复）

**[FEAT] 达尔文机制全自动 ETF 宇宙海选器**
- **功能**：全市场 1455 只 ETF → 流动性初筛 Top60 → 250天体检 → 输出 40 席核心宇宙 `oracle_v2_universe.json`
- **三轮淘汰**：
  1. Round 1 流动性初筛（5天均量，读本地缓存）
  2. Round 2 深度体检（250天 K 线，读本地缓存）
  3. Round 3 物理双过滤（乖离率>40% 引力失控 + 日波动<0.5% 死水）
- **实测结果**：1455只 → 60只 → **39只**最终宇宙（含科创板 588x/158x 系列）

**[BUG] 三项修复**

1. **`'上交所'/'深交所'` 板块名失效 → 改为 `'SH'/'SZ'`**
   - **根因**：此版本 miniQMT `get_stock_list_in_sector` 的板块名为英文缩写，中文名返回空列表
   - **修复**：主方案改用 `SH/SZ`，并自动探测 `"场内基金"/"ETF"/"基金"` 等中文名（兼容新版 QMT）
   - **影响**：从 0 只扩展到正确锁定 1455 只（SH: 809, SZ: 646）

2. **ETF 前缀漏 `52x`（科创系列）**
   - **根因**：旧代码 `startswith(('51', '58'))` 漏掉 `52x` 前缀（同 2026-04-02 手续费漏洞根因一致）
   - **修复**：`_ETF_PREFIXES = ('15', '51', '52', '58')`，SH 侧过滤加 `52`
   - **影响**：覆盖 520xxx 系列沪市 ETF

3. **`download_history_data2(count=N)` 参数不支持 → 静默报错降级**
   - **根因**：此 QMT 版本 API 不接受 `count` 关键字参数，调用即抛 `TypeError`，被 except 捕获后降级本地缓存（无实质影响，但产生误导性错误日志）
   - **修复**：`_batch_download_with_timeout` 去掉 `count` 参数，全量下载由 `get_market_data_ex(count=N)` 截取

**[PATTERN] `get_stock_list_in_sector` 板块名探测 SOP**
- 此 miniQMT 版本正确板块名：`'SH'`（沪市全量 25791只）/ `'SZ'`（深市全量 23006只）
- `'上交所'`/`'深交所'`/`'场内基金'`/`'ETF'` 均返回空列表（版本差异）
- **最佳实践**：先循环尝试语义化名称，失败后 fallback 到 `SH/SZ`，保持跨版本兼容

**验证**：
- `py_compile tools/refine_core_universe.py` → **Exit 0**
- 实际运行：全量 1455 只 ETF 采集成功，39 只高质量宇宙落盘 `oracle_v2_universe.json`

## 📅 2026-04-09 — refine_core_universe.py JSON 带指标输出升级

### 变更文件：`tools/refine_core_universe.py`（功能升级 + 路径修复）

**[FEAT] JSON 输出升级：从纯代码列表 → 带完整指标的结构化对象**
- **旧格式**：`["511380.SH", "518880.SH", ...]`（无任何指标，无法直接决策）
- **新格式**：
  ```json
  {
    "meta": { "generated_at", "total_scanned", "final_seats", "filters" },
    "universe": [
      { "code", "current_price", "avg_amount_5d（亿）",
        "ma250", "bias_pct（%）", "volatility_pct（%）",
        "atr14_pct（%）", "history_days", "updated_at" },
      ...
    ]
  }
  ```
- **新增字段**：
  - `avg_amount_5d`：5日均成交额（亿元），衡量流动性
  - `bias_pct`：与250日均线的乖离率（%），衡量均值引力
  - `volatility_pct`：日收益标准差（%），衡量波动活跃度
  - `atr14_pct`：14日平均真实波幅占价格比（%），T0 spread_pct 核心参考
  - `history_days`：有效历史 K 线天数
  - `updated_at`：生成日期（YYYY-MM-DD）

**[BUG] 绝对路径拼接错误修复**
- **根因**：`out_path = os.path.join(_PROJECT_DIR, output_file)` 当 `output_file` 为绝对路径时，`os.path.join` 仍拼接 `_PROJECT_DIR`，导致路径错误
- **修复**：`if os.path.isabs(output_file): out_path = output_file else: 相对拼接`
- **同时**：`os.makedirs(os.path.dirname(out_path), exist_ok=True)` 自动创建目录

**实测结果**（输出至 `Z:\QuantpC_Workspace\Data\oracle_v2_universe.json`）：
- 1455 只 ETF 扫描 → Top60 流动性猛兽 → **30 只**最终宇宙
- Top 5：511380（国债 134亿）、518880（黄金 78亿）、159352（63亿）、513120（58亿）、512050（57亿）
- ATR14 范围：0.6%（低波债券）~ 8.0%（515880 期货 ETF）

## 📅 2026-04-10 — The Auditor 游走核查程序部署（止盈复盘探针）

### 变更文件：`sniper_exit_guard.py`

- **[FEAT] The Auditor — MFE/MAE 物理切面数据采集探针**
  - **背景**：当前止盈阈值 5% 是否最优缺乏历史证据；需要知道每笔交易买入后的实际空间有多大
  - **设计原则**：纯数据采集角色，不参与任何交易决策；失败静默（`try/except`），不阻断主平仓逻辑
  - **触发时机**：在 `record_action()` 调用之后，每次 Sniper 平仓时立即执行
  - **数据来源**：`xtdata.get_market_data_ex(field_list=["high","low"], period="1d", count=10)` 拉取建仓日至今的日线 high/low 极值
  - **日期过滤**：仅取 `index >= entry_date`（建仓当日及之后的 bar），有效隔离建仓前的噪音
  - **记录字段**（共 12 列）：
    | 字段 | 含义 |
    |---|---|
    | `exit_ts` | 出场时间戳 |
    | `code` / `name` | 标的代码/名称 |
    | `entry_date` / `entry_price` | 入场日期/成交价 |
    | `exit_price` | 出场触发价（lastPrice） |
    | `exit_reason` | 止盈 / 止损 / 时间死线 |
    | `pnl_pct` | 实际盈亏 % |
    | `intraday_high` / `mfe_pct` | T+N 日内最高价 / MFE（最大有利偏移 %） |
    | `intraday_low` / `mae_pct` | T+N 日内最低价 / MAE（最大不利偏移 %） |
  - **输出文件**：`.state/sniper_telemetry.csv`（追加写入，`mode='a'`，永不覆盖）
  - **验证**：`py_compile sniper_exit_guard.py` → **Exit 0**

## 📅 2026-04-16 — start_miniQMT.py：进程存在≠服务就绪假阴性修复

### 变更文件：`start_miniQMT.py`

- **[BUG] 根因**：`start_miniQMT()` 检测到 `XtMiniQmt.exe` 进程存在后，立即调用 `_test_connection()` 一次性验证。冷启动/休眠唤醒后，QMT 进程刚被拉起，xtquant 网关尚未完成初始化握手，第一次连接必然失败 → 函数立即返回 `False` → autopilot 判定 "QMT 启动失败" 并中止后续启动链路。
- **[FIX] 修复**：在"进程已存在"分支增加最多 **3 次 × 15s** 的重试循环：
  - 每次 `_test_connection()` 失败后等待 15 秒再重试
  - 3 次重试全部失败时，不直接放弃，而是调用 `_login_miniQMT()`（完整 GUI 登录流程，含清场旧进程）
- **[影响]**：冷启动后的假阴性容错窗口从 0s → 45s，覆盖 WOL 唤醒后 QMT 网关初始化全链路
- **验证**：`py_compile start_miniQMT.py` → **Exit 0**

## 📅 2026-04-16 — ETF_OU_Grid Executor v4：接入真实下单（全量重写）

### 变更文件：`etf_ou_grid_executor.py`

- **[BUG] 根因**：v3 所有下单路径均为 `# TODO: 接 xttrader.order_stock`，从未真正发单。账本虽写入但无实盘持仓，遥测显示 `BUY_FIRST` 但 QMT 物理查仓为空。另外 `while True` 被注释掉，executor 一次性跑完即退出，无法盘中持续守护。
- **[ARCH] v4 完整重写，全面合规四大铁律**：

| 铁律 | v3 | v4 |
|---|---|---|
| 铁律一：禁止乐观更新 | ❌ 直接写账本 | ✅ 发单注册 `_pending`，回调 `on_stock_trade` 后才写账 |
| 铁律二：盲人摸象隔离 | ✅ 只读自身账本 | ✅ 保持，不查 `query_stock_positions` |
| 铁律三：对称清仓 | ❌ 无实盘量，TODO | ✅ 从账本 `bought_qty` 精确读取卖出量 |
| 铁律四：并发锁 | ❌ 无锁 | ✅ `_pending_lock` + `_pos_lock` 双锁保护 |

- **[FEAT] 新增组件**：
  - `GridCallback.on_stock_trade` — Fill-Based 买入后写账本，卖出后从账本移除
  - `GridCallback.on_order_error` — 废单后从 pending 移除，账本不动
  - `_sweep_stale_pending()` — 每轮巡检超时委托，物理补账
  - `_place_order()` — 统一下单入口，strategyName=`ETF_OU_Grid`，remark=`Buy`/`Sell`/`Sell_TimeStop`/`Sell_Shallow`/`Sell_Deep`
  - `_calc_buy_qty()` — 按资金和价格计算买入手数（百股取整）
  - 买入价格：`min(ask3, ask1 × 1.005)` — ask3 扫单，上浮保护

- **[FIX] subscribe_position → subscribe**（xtquant 正确 API）
- **[OPS] 账本清零**：`etf_grid_positions.json` 中 v3 的虚假持仓记录（物理无仓）已手工清零
- **[OPS] 持续守护**：`while True + time.sleep(5)` 开启，交易窗口 09:30~14:57
- **验证**：`py_compile` → **Exit 0**；QMT 网关连接成功，5s 轮询已激活

## 📅 2026-04-16 — momentum_master + momentum_vector_executor 正式接入 AutoPilot

### 变更文件：`autopilot_master.py`

- **[FEAT] 新增两个路径常量**：
  - `MOMENTUM_MASTER = _DIR / "momentum_master.py"` — 截面动量司令部（盘后选股）
  - `MOMENTUM_EXECUTOR = _DIR / "momentum_vector_executor.py"` — 动量向量执行器（盘中守护）

- **[FEAT] 新增三个调度节点**（时间流状态机 `task_loop`）：

| 时间节点 | Key | 模式 | 职责 |
|---|---|---|---|
| **09:20** | `momentum_t1_auction` | 阻塞（`run_blocking_with_args --t1-only`） | 执行昨日 T+1 集合竞价挂单信号 |
| **09:30** | `momentum_executor` | 看门狗守护（`run_daemon watchdog=True`） | 拉起盘中 VWAP 入场 + 移动止盈 1min 轮询 |
| **15:30** | 串入 `task_eod` Step 4 | 阻塞（`run_blocking`） | 日线同步 + 宇宙精选完成后运行动量选股，输出明日 `momentum_slots.json` |

- **[FEAT] 新增 `task_momentum_t1_auction()` 函数**：
  - 09:20 阻塞执行 `momentum_vector_executor.py --t1-only`
  - 只执行 T+1 待发信号，不启动主循环，完成即退出
  - 无信号时正常退出（exit code 0），不触发告警

- **[ARCH] `task_eod()` 升级为四步串行管道**：
  ```
  Step 1: qmt_daily_sync          (日线落盘)
  Step 2: refine_core_universe    (ETF宇宙精选)
  Step 3: qmt_1m_downloader       (1m分钟线)
  Step 4: momentum_master         (截面动量选股 → momentum_slots.json)  ← 新增
  ```
  - 数据链依赖顺序：Step4 必须在 Step1+Step2 完成后才能拿到最新日线和宇宙，`run_blocking` 串行保证

### 变更文件：`momentum_vector_executor.py`

- **[FEAT] 新增 `--t1-only` CLI 参数**（`argparse`）：
  - 触发路径：`autopilot 09:20 → run_blocking_with_args(MOMENTUM_EXECUTOR, ["--t1-only"])`
  - 执行逻辑：建立 QMT 连接 → 调用 `execute_t1_pending_signals()` → 等 3 秒缓冲 → 断连退出
  - 无信号时：打印 `ℹ️ 无待执行的 T+1 信号，正常退出`，`sys.exit(0)`（autopilot `run_blocking_with_args` 接受 rc=0/1 均为成功）
  - 双重保护：脚本内 `execute_t1_pending_signals` 本身有 `execute_date <= today` 日期门卫，防止误触发

- **[PATTERN] T+1 双重执行保障（防单点故障）**：
  - 保障层 1（09:20）：`autopilot --t1-only` 阻塞挂单，是主要执行路径
  - 保障层 2（09:25）：主循环启动后 `T1_AUCTION_HHMM` 检测，双保险

- **验证**：`py_compile autopilot_master.py` → **Exit 0**；`py_compile momentum_vector_executor.py` → **Exit 0**

### 补充变更：autopilot_master.py 盘前动量选股居安节点

- **[FEAT] 新增 _is_momentum_slots_stale() 函数**：读取 momentum_slots.json mtime，过期/不存在则返回True
- **[FEAT] 新增 09:00 盘前居安调度节点（key=momentum_preopen）**：slots 过期时触发补跑，否则跳过
- **[ARCH] 数据完整性双保险**：主路径（15:30 Step4）+ 兜底路径（09:00 盘前居安）
- **验证**：py_compile autopilot_master.py → Exit 0

## [BUG] 2026-04-16 momentum_master.py — 移除 xtquant 依赖，改为直接读 Parquet

- **根因**：xtdata.get_market_data_ex() 是 C/S 架构，必须 miniQMT 进程在线，即使读本地缓存也需要 TCP 连接。盘后/盘前非交易时段 miniQMT 未启动时必然报错。
- **修复**：改为直接读 Z:/QuantpC_Workspace/Data/Market_Daily/{code}.parquet（qmt_daily_sync 每日落盘），ETF 名称从 oracle_v2_universe.json 读取（已有字段），完全移除 from xtquant 导入
- **新增函数**：_code_to_parquet(code)、_load_parquet_close(code)
- **效果**：无 miniQMT 下 3 秒完成 60 只 ETF 全量扫描，可在盘后/盘前任意时刻离线运行
- **验证**：py_compile → Exit 0；miniQMT 未启动状态下实测通过，TOP3 正确选出

## [OPS] 2026-04-16 autopilot_master.py — task_eod 触发时间 15:30→15:05

- A 股 15:00 收盘，QMT 数据服务器 1-3 分钟内完成今日日K落盘，原 15:30 缓冲过于保守
- EOD 管道（日线同步→宇宙精选→1m 下载→动量选股）可提早约 25 分钟完成
- watchdog WATCHDOG_STOP_HHMM=1530 保持不变（全局安全边界，executor 自身在 15:00 已内部 exit）
- 验证：py_compile → Exit 0

## [BUG] 2026-04-16 momentum_vector_executor.py — 铁律一违反：乐观删账本改为物理封条

- **根因**：_execute_sell() 在发出卖单后立即 holdings.pop(code) 属乐观更新，违反铁律一。若成交回调丢失，账本已删但实盘仍有持仓，产生幽灵仓位。
- **修复（用户指定方案）**：
  1. 新增 _pending_sells: set 全局内存封条池
  2. _execute_sell：发单成功 → _pending_sells.add(code) + 注册 PENDING（direction=SELL），禁止 holdings.pop
  3. on_stock_trade：direction==SELL 且全量成交 → _pending_sells.discard + holdings.pop（唯一合法撤账本路径）
  4. on_order_error：卖单被拒 → _pending_sells.discard 移除封条，下轮允许重试
  5. monitor_and_exit：触发退出前先检查 code in _pending_sells，是则跳过（防重复发单）
- **函数签名**：_execute_sell 增加 info: dict = None 参数（供 PENDING 注册读取 trade_rule）
- **验证**：py_compile → Exit 0

## 📅 2026-04-19 — Hawkes V3 Titan 重构（全模块架构升级）

### 变更文件：`hawkes_executor.py`（V2 → V3 Titan 整体重构）

**[ARCH] 三模块分层重构，彻底白盒化每一步状态流转**

#### 模块一：全局状态机 & 绝对防御装甲（三道物理内存锁）

| 物理内存锁 | 类型 | 驱动点 | 锁名/作用 |
|---|---|---|---|
| `_pending_locks` | `set` | 发单即封印，Fill回调解除 | 锁A（在途防御）|
| `_last_fire_time` | `dict[str,float]` | 开火后更新，tick线程单写 | 锁B（60s冷却）|
| `_current_exposure` | `dict[str,float]` | Fill-Based精确驱动 | 锁C（2万敞口封顶）|

三锁在 `on_tick` 最顶层按 A→B→C 校验，任一未过立刻 return（物理致盲丢弃）。
锁C触发时主动调用 `_trigger_micro_exit()`（敞口强平）。

#### 模块二：三位一体开火引擎（The Trigger）

- 嗅探1：Hawkes λ ≥ 25.0（O(1) 指数衰减，无未来函数）
- 嗅探2：OBI > 0.8，5档市值加权（w=1/(i+1)），O(1)
- 嗅探3：`check_toxicity()` VPIN预留接口，当前True
- `FIRE_PAUSED=True` 干运行模式：三关全过但不实际下单

#### 模块三：极速退壳协议（Micro-Exit）

退出优先级（时间止损 > 止盈 > 止损 > 锁C敞口），统一入口 `_trigger_micro_exit()`：
- 防重入护盾：status!="holding" 静默return
- 封印顺序：① holdings→"exiting" ② _pending_locks.add ③ _execute_exit
- `_clear_holding_and_unlock()` 原子三步：清账本 + 敞口归零 + 解锁
- `entry_unix` 记录实际成交时间戳（on_order_trade时），45s从真实持仓开始算

**[ARCH] 完整状态生命周期闭环**
```
FIRE→_pending_locks.add → on_order_trade BUY→holdings["holding"]+敞口充值+锁解
→ _trigger_micro_exit → holdings["exiting"]+_pending_locks再封 → _execute_exit
→ on_order_trade SELL → _clear_holding_and_unlock（清账+归零+解锁）
```

**[PATTERN] 防御加特林三锁架构**（新设计模式，记录供后续策略复用）
- 锁A（在途锁）：解决「委托已发出但成交未回来」的幽灵窗口
- 锁B（冷却锁）：解决「同标的高频连发」
- 锁C（容量锁）：解决「仓位超限仍触发开火」
- 核心原则：纯内存数据结构替代API轮询，每道锁O(1)查找，职责单一

**[OPS] 四大铁律合规**
- ✓ 铁律一：Fill-Based，on_order_trade 后才写账本
- ✓ 铁律二：盲人摸象，只读 hawkes_holdings.json
- ✓ 铁律三：物理清仓，query_stock_positions→can_use_volume→全额卖
- ✓ 铁律四：_pending_locks_mu/_hawk_pending_mu/_exposure_mu/_holdings_mu 四锁全保护

**验证**：`py_compile hawkes_executor.py` → **Exit 0**（2026-04-19 13:59 落盘确认）

---

### 变更文件：`hawkes_executor.py`（Paper Trading 沙盘升级）

**[FEAT] FIRE_PAUSED=True 时启用真实沙盘模拟（替代此前的静默日志干运行）**

| 新增组件 | 说明 |
|---|---|
| `_paper_holdings` | 内存沙盘持仓字典，`_paper_holdings_mu` 保护 |
| `_PAPER_LEDGER_FILE` | `logs/YYYYMMDD_hawkes_paper_ledger.csv`，utf-8-sig，Excel 直接打开 |
| `_simulate_paper_entry()` | 模拟建仓，以 ask1 快照价入账，写入 `_paper_holdings` |
| `_paper_exit_monitor()` | 后台守护线程（仅 FIRE_PAUSED 时启动），1s 扫描，TP/SL/时间止损触发模拟退出 |
| `_write_paper_ledger()` | 每笔平仓后写 CSV 明细（含毛盈亏、双边手续费估算、净盈亏） |

**Paper Ledger CSV 字段**：`trade_id / code / entry_time / entry_price / qty / capital / lam / obi / exit_time / exit_price / hold_sec / exit_reason / gross_pnl / est_commission / net_pnl`

**手续费估算**：双边万1（ETF 免印花税），`est_commission = (entry_val + exit_val) × 0.0001`

**[ARCH] 沙盘与实盘隔离原则**
- 沙盘冷却器复用实盘 `_last_fire_time`（保证信号密度与实盘完全一致，不虚增信号）
- `_paper_holdings` 完全独立于 `hawkes_holdings.json`，不干扰实盘账本任何路径
- `_paper_exit_monitor` 只调用 `xtdata.get_full_tick()`（只读行情），不碰 `xt_trader`

**验证**：`py_compile hawkes_executor.py` → **Exit 0**（2026-04-19 14:09 落盘确认）

---

## 📅 2026-04-19 — macro_rotation_executor.py V3 Titan 重建 + 炸弹修复

### 变更文件：`macro_rotation_executor.py`（0字节重建）

**[BUG] 根因：文件于 2026-04-15 20:22 写入中断，导致 0 字节空文件**
- `.pyc` 缓存同步为空，无法从 pyc 恢复历史代码
- autopilot_master.py 每周五 14:42 仍调用此文件，执行空脚本无任何动作

**[FEAT] 用户重建 V3 Titan 四大护盾架构**
1. 原子级 JSON I/O（tmp → os.replace）
2. 物理查仓（query_stock_positions → can_use_volume 验证）
3. Oracle 鲜度校验（Timestamp 今日校验，过期数据直接防御）
4. 严格同步换仓（先卖→原子写槽位→再买）

**[BUG] Agent 修复 4 个确定性运行时炸弹**

| # | 位置 | 原始问题 | 修复 |
|---|---|---|---|
| ① | L189 | `xtconstant.StockAccount` 不存在 → AttributeError | 改为 `from xtquant.xttype import StockAccount` |
| ② | L28 | `ORACLE_FILE = "oracle_telemetry_macro.csv"` CWD 漂移，文件实际在 `.state/` | 改为 `os.path.join(STATE_DIR, "oracle_telemetry_macro.csv")` |
| ③ | L138 | `sell_price = lastPrice * 0.9` 远低于跌停板，QMT 以「超出涨跌停」拒单 | 改为 `bid1 - 0.002`（Taker价），对齐 macro_risk_monitor._sell_slot |
| ④ | L190 | `trader.subscribe(acc)` 未检查返回值，订阅失败静默继续 | 加 `if ... != 0: return` |

**[FEAT] Agent 补充 2 项设计增强**
- 时间守卫：入口强校验「周五 14:40~14:58」双重防误触发
- `updated_at` 时间戳：最终落盘写入，供 macro_risk_monitor 识别状态时效

**[ARCH] 双文件对齐确认**
- `ORACLE_FILE` 路径对齐 macro_risk_monitor 的 `TELEMETRY_CSV = _STATE_DIR / "oracle_telemetry_macro.csv"`
- 卖价逻辑（bid1-0.002）完全对齐 macro_risk_monitor._sell_slot
- `updated_at` 字段对齐 macro_risk_monitor._load_slots 的 `slots.get("updated_at", "N/A")`

**验证**：`py_compile macro_rotation_executor.py` → **Exit 0**（2026-04-19 14:54 落盘确认）

---

### 变更文件：`macro_rotation_executor.py`（接入动态决策引擎）

**[FEAT] 新增 Market Regime 自适应双权重打分引擎**

核心函数 `calculate_dynamic_targets(df_oracle, df_momentum, benchmark_price, benchmark_ma20)`:

| Regime | 判定条件 | W_ORA | W_MOM | Risk_Multiplier | 单槽资金 |
|---|---|---|---|---|---|
| UPTREND | 510300 现价 > MA20 | 0.3 | 0.7 | 1.0 | 50,000元 |
| DOWNTREND | 510300 现价 ≤ MA20 | 0.8 | 0.2 | 0.4 | 20,000元 |

数据流程：Inner Join(Oracle∩Momentum) → Z-Score无量纲化 → Composite_Score=Odds_Z×W_ORA+Mom_Z×W_MOM → Top2排名

**[FEAT] 新增三个辅助函数**
- `_load_oracle_df()`: 读取 `.state/oracle_telemetry_macro.csv`，每标的取今日最新一行，含鲜度校验
- `_build_momentum_df(codes)`: 拉取22根日线本地缓存，计算 Mom_20D=(P_last-P_-20)/P_-20，O(N)无未来函数
- `_get_benchmark_stats()`: 510300.SH 静态底座MA20 + 动态缝合 lastPrice，返回(现价, MA20)

**[ARCH] execute_rotation() 接入动态引擎（替换静态 get_oracle_targets）**
- 3步流水线：load_oracle_df → build_momentum_df → get_benchmark_stats → calculate_dynamic_targets
- sync_buy 签名升级：新增 `alloc=FUNDS_PER_SLOT` 参数，接受 Regime 动态分配额度
- alloc_A/alloc_B 字段落盘到 macro_slots.json，供 macro_risk_monitor 守卫读取

**[FIX] sync_buy 买价逻辑对齐 sync_sell**
- 原 lastPrice*1.02 → ask1+0.002（Taker价），与 sync_sell bid1-0.002 完全对称

**验证**：`py_compile macro_rotation_executor.py` → **Exit 0**（2026-04-19 15:11 落盘确认）

---

### 变更文件：`macro_rotation_executor.py`（Oracle直连 + N8N推送补全）

**[BUG] 根因：周五进攻日 Oracle 数据永远过期**
- `macro_risk_monitor.py (Sentinel)` 在 `weekday >= 5`（周五/周末）时自动退出，不调 Oracle
- 周一至周四 Sentinel 写入 CSV，但周五 14:42 执行器读到的最新时间戳是「周四 14:00」
- 鲜度校验：`today_str not in latest_ts` → True → 返回 `[DEFENSE_ETF, DEFENSE_ETF]`
- **结论：周五永不进攻，策略完全失效**

**[FEAT] _load_oracle_df 升级为三层防御架构**

| 层级 | 路径 | 触发条件 |
|---|---|---|
| 层一（主路） | 直接 POST ORACLE_URL，组装静态底座+动态缝合 payload | 正常情况 |
| 层二（备用） | 读 `.state/oracle_telemetry_macro.csv` | Oracle 宕机/超时 |
| 层三（兴山） | 返回空 DataFrame → calculate_dynamic_targets 全线防御 | CSV 也过期 |

**[FEAT] 补全 N8N_WEBHOOK + _send_webhook()**
- `import requests / from dotenv import load_dotenv` 补全
- `ORACLE_URL = os.getenv("ORACLE_URL", "http://10.10.8.20:8000/predict_batch")` + `N8N_WEBHOOK`
- `_send_webhook(title, message)` 对齐 macro_risk_monitor._send_n8n 实现
- execute_rotation() 结尾推送轮动汇总（Regime/槽位/资金/时间戳）

**[FIX] QMT_PATH 双反斜杠修复**
- raw string 中 `r"C:\\..."` 错误 → 还原为 `r"C:\..."` 正确写法

**验证**：`py_compile macro_rotation_executor.py` → **Exit 0**（2026-04-19 15:22 落盘确认）

---

### 变更文件：`macro_rotation_executor.py`（接口对齐 macro_risk_monitor）

**[BUG] 三个接口错位（交叉阅读两文件后发现）**

| # | 字段 | executor 写入值 | monitor 读取期望 | 后果 |
|---|---|---|---|---|
| ① | macro_slots.json 槽位键 | `slot_A / slot_B`（大写） | `slot_a / slot_b`（L322-323 小写） | monitor 永远读到 SAFE_ASSET 默认值，熔断裁决完全失效 |
| ② | macro_slots.json 资金键 | `alloc_A / alloc_B` | `slot_a_capital / slot_b_capital`（L424 动态格式化） | 熔断后买入国债资金量=50000硬编码，Regime决策的动态仓位完全失效 |
| ③ | CSV 列结构 | 8列（无`名称`） | 9列（含`名称`）| 两方追加写同一文件列数不同，read_csv全部错列 |

**[FIX] 三项修复**
- `_load_slots()` 默认值改为 `{"slot_a": None, "slot_b": None}`
- `execute_rotation()` 全部槽位读写改为小写键名
- `slot_a_capital / slot_b_capital` 对齐 monitor `f"{slot_name.lower()}_capital"` 格式
- CSV writerow 补充 `名称` 第三列（并调用 xtdata.get_instrument_detail 获取 ETF 中文名）

**验证**：`py_compile macro_rotation_executor.py` → **Exit 0**（2026-04-19 15:31 落盘确认）

**[ARCH] macro_slots.json 最终标准格式（executor写 / monitor读 共同约定）**
```json
{
  "slot_a": "518880.SH",
  "slot_b": "510300.SH",
  "slot_a_capital": 50000.0,
  "slot_b_capital": 50000.0,
  "updated_at": "2025-04-25 14:43:17"
}
```

---

## 📅 2026-04-19

### 变更文件：`tools/fetch_etf_universe.py`（新建）

**[FEAT] 全市场 ETF 代码采集工具（独立函数库）**

**设计目标**：可被任意脚本 import 调用，代码格式与历史数据（parquet/xtdata）完全对齐。

**核心函数：**

| 函数 | 用途 |
|---|---|
| `get_all_etf_codes()` | 获取全市场 ETF 代码列表（List[str]）|
| `get_etf_info_df()` | 获取带元数据的完整 DataFrame（含名称/交易制度/parquet路径）|
| `save_etf_universe_json()` | 落盘 JSON 到 `.state/etf_full_universe.json` |
| `code_to_parquet_path(code)` | 代码 → parquet 路径（格式对齐 qmt_daily_sync / momentum_master）|
| `classify_trade_rule(code)` | 推断 ETF 交易制度 T+0 / T+1 |
| `check_qmt_alive()` | 轻量探针检测 miniQMT 是否在线 |

**三层防御（与 refine_core_universe.py 对齐）：**
- 层一：`get_stock_list_in_sector("场内基金")` 等板块接口（覆盖最全，含 52x 科创系列）
- 层二：SH/SZ 全量拉取 + 前缀白名单过滤兼容旧版 miniQMT
- 层三：终止并抛出 RuntimeError + 建议操作说明

**代码格式规范（与项目历史数据对齐）：**
- QMT 标准格式：`XXXXXX.SH` / `XXXXXX.SZ`
- parquet 路径：`{6位代码}_{SH|SZ}.parquet` → `510300_SH.parquet`

**验证**：`py_compile tools/fetch_etf_universe.py` → **Exit 0**（2026-04-19 16:45 确认）

---

### 变更文件：`tools/fetch_etf_universe.py`（T+0 对位 CSV 铁律）

**[BUG] T+0 判定依赖硬编码前缀规则，与项目权威白名单脱钩**
- 原实现：`_T0_SH_PREFIXES` + `_T0_SZ_WHITELIST` 静态集合 → 手动维护，容易遗漏新上市 QDII
- 项目铁律（quant-v4-patterns §12.5）：永远通过读取 `t0_absolute_pool.csv`，永不用代码前缀推导

**[FIX] 删除硬编码规则，改为对位 `t0_absolute_pool.csv`**
- 删除 `_T0_SH_PREFIXES` / `_T0_SZ_WHITELIST` 两个静态常量
- 新增 `T0_POOL_CSV` 路径常量（对齐 momentum_master / t0_multigrid_executor 同一文件）
- 新增 `load_t0_pool(csv_path=None) -> frozenset`
  - 读取逻辑与 `momentum_master._load_t0_set()` 完全一致（`len(code)==9 and "." in code`）
  - 文件不存在 / 解析失败 → 返回空 frozenset → 下游全按 T+1 保守处理
- `classify_trade_rule(code, t0_set=None)` 改为接受预加载集合
  - `t0_set=None` 时惰性加载（单只代码查询场景）
  - `get_etf_info_df()` 内一次性 `load_t0_pool()` 后批量传入，避免 N 次重复 IO

**验证**：`py_compile tools/fetch_etf_universe.py` → **Exit 0**（2026-04-19 17:24 确认）

---

## 📅 2026-04-21 — etf_ou_grid_executor.py 双重物理护盾上线

### 变更文件：`etf_ou_grid_executor.py`

**[FEAT] 护盾一：`is_fatal_downtrend(code)` 无差别趋势护盾**

- **根因**：原网格在标的单边暴跌时仍会按步长无脑接飞刀，深水区缺乏主动拦截。
- **修复**：新增函数取近 22 根日线，计算 MA20 及斜率。条件 `当前价 < MA20 且 Slope < 0` → True。缝合于 BUY_NEXT 分支，命中写 DOWNTREND_REJECT 遥测+红色日志。数据异常保守放行。

**[FEAT] 护盾二：`get_atr_14(code)` + 断头台（ATR 动态止损 + 时间斩仓）**

- **根因**：原时间止损为静态 halflife×2 天，无法适配不同品种真实波幅。
- **修复**：ATR-14 = 近 14 日 TR 均值；动态止损价 = 均价 - 1.5×ATR。条件A（空间底线）：现价 < 止损价；条件B（时间底线）：持仓≥2天且浮亏。命中任一 → 市价全量清仓（Sell_Guillotine）+ N8N 推送 💥 + 关入 48h 小黑屋。

**[FEAT] 小黑屋（Blacklist）**：模块级 `_blacklist: dict` + `_BLACKLIST_LOCK`（铁律四），断头台后 48h 拦截该标的所有买入。

**[PATTERN] 网格买入四级拦截链**：熔断 → 小黑屋 → 趋势护盾 → 正常买入。卖出不受护盾影响。

**四律合规**：铁律一(只在Fill回调写账)✅ 铁律二(只读自身账本)✅ 铁律三(卖出量精确读bought_qty)✅ 铁律四(_BLACKLIST_LOCK新增，原双锁不变)✅

**验证**：`py_compile etf_ou_grid_executor.py` → **Exit 0**（2026-04-21 20:09 确认）

---

### 变更文件：`etf_ou_grid_executor.py` + `autopilot_master.py`（编码崩溃修复）

**[BUG] 根因：Windows GBK 控制台编码导致进程启动即崩，日志无内容**

**故障现象**：`20260421_etf_ou_grid_executor.log` 只有 AutoPilot banner，无任何执行器内容。进程在09:30:12启动，1秒内已退出（exitcode=0），看门狗停止复活，全天无网格监控。

**根因链**：
1. `autopilot_master._spawn()` 用 `Popen(stdout=fh)` 把子进程 stdout 接管为原始 OS fd
2. 子进程 Python 向该 fd 写时，用操作系统默认编码（GBK/CP936），而非 utf-8
3. 第一行 `print("🛸 极速网格...")` 含 emoji `\\U0001f6f8`，GBK 无法编码 → `UnicodeEncodeError`
4. 进程立刻以 exitcode=0 退出（Python 自身正常退出，异常在主线程传播完毕）
5. watchdog 判断 `rc==0` → "正常退出，停止复活" → 永久放弃，全天睡觉

**[FIX 1] `etf_ou_grid_executor.py` 模块顶部强制 UTF-8 输出流**：
`python
import sys, io
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
if hasattr(sys.stderr, 'buffer'):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)
`
在所有其他 import 之前执行，errors='replace' 保证即使有无法编码字符也不崩溃。

**[FIX 2] `autopilot_master._watchdog_thread()` 快速退出检测**：
- 新增 `_start_ts` 记录进程启动时间戳
- `is_stable_exit = (rc == 0 and _alive_sec >= 60)`
- 只有"稳定运行 ≥ 60秒后正常退出"才认为是合法退出；< 60秒即使 rc=0 也触发复活协议
- 兜底防御：任何启动崩溃（UnicodeError、ImportError等）均能被自动复活

**[PATTERN] Windows 子进程编码兜底铁律**：
> 所有通过 `Popen(stdout=pipe/file)` 拉起的 Python 子进程，必须在模块最顶部重定向 sys.stdout/stderr 为 UTF-8，否则 Windows 中文系统环境下首行含 emoji 的 print 必崩。

**验证**：`py_compile etf_ou_grid_executor.py` → Exit 0；`py_compile autopilot_master.py` → Exit 0

## 📅 2026-04-22 — Macro V4 战略重铸：Oracle 彻底废弃 + 夏普动量引擎升级

### 变更文件：`macro_rotation_executor.py`

**[ARCH] Oracle/Odds_Ratio/Z-Score 全线物理切除，升级为纯风险调整后动量打分**

| 删除内容 | 说明 |
|---|---|
| `ORACLE_URL` / `ORACLE_FILE` 常量 | 预言机 API 地址与遥测文件引用 |
| `MIN_ODDS` / `MIN_YIELD` 常量 | 旧版 Odds_Ratio 进攻门槛 |
| `get_oracle_targets()` (135行) | Oracle CSV 解析与鲜度校验函数 |
| `_load_oracle_df()` (116行) | 三层防御 Oracle 获取管道（含 requests 调用） |
| `_get_benchmark_stats()` | Regime MA20 判定函数（无需 Regime 权重） |
| `csv` import | 遥测落盘依赖（已删除） |

**[FEAT] _build_momentum_df 重构：Score = Return_20D / Ann_Vol（夏普式截面动量）**

`python
# 新公式（无未来函数，纯读本地 O(N) 计算）
Return_20D = (closes[-1] - closes[-21]) / closes[-21]          # 20日收益率
Ann_Vol    = std(daily_rets[-20:]) * sqrt(252)                  # 20日年化波动率
Score      = Return_20D / Ann_Vol                              # 截面夏普近似
`

- count=25（原来 count=22），确保 21 根动量数据 + 20 根波动率数据均充足
- 防御：Ann_Vol < 1e-8 → 跳过（防零除）；数据不足 21 根 → 自动剔除

**[FEAT] calculate_dynamic_targets 重构：纯 Score 降序，负动量强制切防守**

- 废弃：df_oracle 参数、Market Regime / W_ORA / W_MOM 双权重、Z-Score 步骤、risk_multiplier
- 函数签名简化：calculate_dynamic_targets(df_momentum) → 单参数
- 若 Return_20D < 0（负动量），该槽位强制切为 DEFENSE_ETF (511260.SH)
- 资金：双槽固定 FUNDS_PER_SLOT = 50,000，取消 DOWNTREND 缩仓逻辑

**[FEAT] MACRO_POOL 扩容至 10 只**：新增 588000.SH（科创50）、563300.SH（港股科技）

**[FEAT] 账本增强：sync_buy 返回成交参考价，写入 slot_{a/b}_hwm（移动止盈初始锚点）**

`json
{"slot_a": "513500.SH", "slot_a_capital": 50000, "slot_a_hwm": 1.2134, ...}
`

**[ARCH] 主流程简化：旧三步流水线 → 新两步**
`
旧：_load_oracle_df → _build_momentum_df → _get_benchmark_stats → calculate_dynamic_targets(4参数)
新：_build_momentum_df → calculate_dynamic_targets(1参数)
`

**验证**：`py_compile macro_rotation_executor.py` → **Exit 0**

---

### 变更文件：`macro_risk_monitor.py`

**[ARCH] Oracle 全线物理切除，守卫回归纯物理事实判定**

| 删除内容 | 说明 |
|---|---|
| `ORACLE_URL` 常量 | 预言机 API 地址 |
| `SENTINEL_ODDS_MIN / SENTINEL_YIELD_MIN` | 旧版 Odds/Yield 熔断阈值 |
| `csv` import | 遥测落盘依赖 |
| `_fetch_sentinel_ammo()` | payload 组装 + 静态底座函数 |
| `_write_telemetry()` | Oracle 格式遥测 CSV 写入函数 |
| `_is_sentinel_triggered()` | 基于 Odds_Ratio/Q50_Yield 的熔断判定函数 |
| main 函数内 requests.post Oracle 整块 | 预言机调用、response 解析、records 构建 |

**[FEAT] 新增双防线熔断判定（The Sentinel V2）**

防线1 — 移动止盈 (Trailing Stop)：
- 现价 > HWM → 追踪更新 HWM（落盘到 macro_slots.json）
- 现价 < HWM × (1 - 0.08) → 回撤 ≥ 8%，立即熔断

防线2 — 均线防守 (Trend Stop)：
- 现价 < MA20 → 记录首次跌破时间戳（slot_{a/b}_ma20_break_at）
- 连续跌破 ≥ 2 小时未修复 → 立即熔断
- 现价修复 → 自动清除计时（slot_ma20_break_at = null）

**[FEAT] 新增辅助函数**
- `_get_current_price(code)` → 读 get_full_tick，回退 lastClose，安全防零
- `_get_ma20(code)` → 读本地 get_market_data_ex(count=22)，取最后20根均值，0 网络请求
- `_save_slots(slots)` → 新增原子写入辅助函数（tmp → replace）

**[FEAT] MACRO_POOL 同步扩容至 10 只**（与执行器保持严格对齐）

**[FEAT] HWM 老账本兼容**：若 slot_hwm 字段不存在（旧账本）则以当前价初始化，不崩溃

**验证**：`py_compile macro_risk_monitor.py` → **Exit 0**

---

### 🔷 新设计模式（PATTERN）：移动止盈 × MA20 双防线熔断哨位

`
[防线1 移动止盈]
  持仓期间追踪最高水位（HWM），现价从峰值回撤 N% → 立即止盈出场。
  优势：无需预判顶部，自动锁定大部分利润。
  持久化：HWM 写入 JSON 账本（防进程重启失忆）。

[防线2 MA20 均线防守]
  跌破均线本身非熔断条件；需持续 T 小时未修复才触发。
  防止均线日内短暂假破（惯性穿越后快速收回）误伤。
  计时器有效期：进程重启后从 JSON 恢复，不归零。
`

适用场景：任何持仓周期为数天~数周的 ETF 轮动/动量策略，需要比固定止损更灵活的防守机制时。

### 变更文件：`macro_rotation_executor.py`（Regime 资金缩放补丁）

**[FEAT] 恢复沪深300 MA20 Regime 判定，实现动态资金缩放**

- 保留 V4 夏普动量打分（Oracle 仍废弃），仅在资金层叠加 Regime 控制
- 新增 `_get_benchmark_stats()`（与 V3 等价实现，纯读本地 QMT 缓存，0 网络请求）
- 新增常量 `FUNDS_UPTREND=50000` / `FUNDS_DOWNTREND=20000`

| Regime | 判定条件 | 单槽资金 |
|---|---|---|
| UPTREND | `现价 > MA20（沪深300）` | **5万元**（牛市满配进攻）|
| DOWNTREND | `现价 ≤ MA20` | **2万元**（熊市强行缩仓）|
| UNKNOWN | 基准数据异常 | **2万元**（保守降档）|

- 架构决策：`calculate_dynamic_targets(df_momentum)` 签名不变（单参数），
  资金覆盖在 `execute_rotation()` 中完成（Regime → alloc_per_slot → 覆盖 alloc_a/b）
- N8N 推送消息增加 `Regime` 和基准价格字段

**验证**：`py_compile macro_rotation_executor.py` → **Exit 0**

---

## 📅 2026-04-23 — underdog_executor.py 新建（落水狗左侧抄底）

### 变更文件：`underdog_executor.py`（[NEW]）

**[FEAT] 新建独立执行器：落水狗左侧抄底策略**

- 资金：FUNDS_LIMIT = 50,000（固定，禁止动态缩放）
- 账本：`.state/underdog_slots.json`（独立隔离，铁律二合规）
- ETF池：`from tools.fetch_etf_universe import get_all_etf_codes`（L48，真实工具）
- 扫描器 `scan_underdogs`：三维共振（跌幅极值10%分位 + 放量>3%量比>2x + 结构突破10日高）
- 巡逻器 `patrol_positions`：四道防线（A断头台/B时间腐烂/C均值回归减仓/D动能衰竭移动止盈）
- Fill-Based记账（铁律一）、盲人摸象隔离（铁律二）、对称清仓（铁律三）、并发锁（铁律四）全部合规
- PENDING_BUY占位模式：发单前预写占位，失败pop清除，Fill回调覆盖FULL

**验证**：`py_compile underdog_executor.py` → **Exit 0**

---

## 📅 2026-04-23 — underdog_executor.py 分裂作息重构

### 变更文件：`underdog_executor.py`（作息架构升级）

**[ARCH] 分裂作息：Scanner 锁定尾盘 / Patrol 盘中高频**

- `LOOP_INTERVAL_SEC / MARKET_OPEN / MARKET_CLOSE` 废弃，改为专属时间窗口常量
- `PATROL_OPEN=09:30 / PATROL_CLOSE=14:55 / PATROL_INTERVAL=30s` — 四维退出矩阵盘中持续轮询
- `SCANNER_OPEN=14:45 / SCANNER_CLOSE=14:55` — 三维共振扫描仅在尾盘触发，`_scan_done_date` 防止同日重复
- `_is_trading_time()` 拆分为 `_is_patrol_time()` + `_is_scanner_window()`，语义更精准
- `main()` 重写为三段式：Patrol 区 / Scanner 区 / 静默等待区，主循环以 `PATROL_INTERVAL=30s` 为基础心跳
- Scanner 异常时不标记 done，允许窗口内重试；成功后推 N8N 告知结果

**验证**：`py_compile underdog_executor.py` → **Exit 0**
