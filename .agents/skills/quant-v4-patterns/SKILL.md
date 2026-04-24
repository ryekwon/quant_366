---
name: quant-v4-patterns
description: 量化交易 V4.0 核心架构模式 - 包含防火墙隔离、物理清仓保障、订单标识规范及成本价动态校验，以及四大铁律。§14 Fill-Based 原子记账铁律（Pending注册表/IO锁/无情清场/加权均价/预写回写分离/精准卖出/账本字段规范）。【写新代码前必须先读本文件，对照铁律检查】
---

# 量化交易 V4.0 核心架构模式

本 Skill 沉淀了在 QMT (迅投量化) 环境下构建稳健、不冲突、高精度交易系统的核心模式。

## 1. 领地隔离 (Strategy Firewall)
当账户运行多个策略（如 T0, Sniper, 轮动）时，必须防止不同策略对同一标的的误操作。

### 实现模式
- **排除名单**：策略加载时读取其他策略的配置文件（如 `.state/grid_targets.yaml`）。
- **实时拦截**：在选股或下单前执行 ` territoriale_check `。

```python
# 示例：轮动策略避开 T0 标的
def is_t0_occupied(code):
    with open(".state/grid_targets.yaml", 'r') as f:
        targets = yaml.safe_load(f).get('targets', [])
    return code in [t['code'] for t in targets]
```

## 2. 物理清仓保障 (Physical Sweep)
防止因内部计数（lots）与实盘碎股（如分红、手动交易）不一致导致的“僵尸持仓”（如 100 股残余）。

### 核心规则
- **绝对退出**：在止盈 (Absolute TP) 或 止损 (Abyss SL) 逻辑中，**严禁**使用内部计算的数量。
- **物理真相**：必须先执行 `query_stock_positions` 获取 `volume`，然后进行全额清仓。

```python
# 物理歼灭协议
target_pos = next((p for p in pos_list if p.stock_code == code), None)
if target_pos:
    total_qty = int(target_pos.volume) # 物理真实数量
    xt_trader.order_stock(acc, code, xtconstant.STOCK_SELL, total_qty, ...)
```

## 3. 订单标识规范 (Order Tagging)
为了在 QMT 终端清晰区分不同策略的成交单和持仓，必须统一 `strategyName` 和 `orderRemark`。

### 推荐标准
| 策略类型 | strategyName | orderRemark |
| :--- | :--- | :--- |
| **Grid T0** | `V4.0` | `Buy` / `Sell` |
| **T0 TP** | `V4_TP` | `Absolute` |
| **T0 SL** | `V4_SL` | `Abyss` |
| **Sniper** | `Sniper` | `Exit` |
| **Rotation**| `ETF_Rota` | `Buy` / `Sell` |

## 4. 成本价防漂移 (Cost Basis Protection)
券商提供的 `open_price` 可能因分红、派息或历史持仓产生漂移，导致止盈止损线失效。

### 解决方案
- **内部基准**：在 `runtime_state` 中持久化记录 `base_price`（入场价）。
- **动态校准**：收容孤儿资产时，以当前 `tick` 市价作为基准，而非券商成本价。

## 5. 状态全自动同步 (State Sync & Adoption)
系统启动或巡逻时，应自动对比“实盘持仓”与“策略账本”。
- **Orphan (孤儿)**：实盘有，账本无 -> 进入 `sell_only` 模式，由网格高抛清退。
- **Ghost (幽灵)**：账本有，实盘无 -> 立即归零账本计数或删除对应记录。
- **校准基准**：采用实时 Tick `lastPrice` 作为初始成本（Base Price），而非券商历史成本。

## 6. 进程守护与唯一性 (Process Singleton & Watchdog)
为了防止多个执行器实例竞争 API 或文件锁，必须强制执行单例运行。

### 实现模式
- **文件锁 (File Lock)**：启动时尝试获得文件独占访问，防止多开。
- **Watchdog (看门狗)**：在 `autopilot_master.py` 中记录 PID 并由主进程监听子线程，崩溃后自动重启，带有最大尝试限次和超时保护。
- **定时清理**：收盘后（如 16:30）由主任务执行物理清理，彻底结束所有 Python 和 PowerShell 子程序进程，释放端口及 API 资源。

## 7. 行为观察探针 (Action Probes)
为了实现实时的全局动作同步及长期的交易复盘/机器学习分析，所有关键决策必须记录。

### 探针标准
所有执行器在决策后果必须调用 `quant_logger.record_action` 原子探针，包含：
- `strategy`: 策略标识。
- `action`: 买/卖/止盈/止损/拦截/熔断。
- `target`: 标的代码。
- `reason`: 可理解的业务原因（如“触发绝对止盈”）。
- `extra`: 包含具体交易数据（如 `qty`, `is_full_sweep` 等元数据）。

```python
record_action(
    strategy="T0_Grid", 
    action="止盈", 
    target=code, 
    reason="触发绝对止盈线",
    extra={"qty": total_qty, "is_full_sweep": True}
)
```

## 8. 日常数据前置与清洗 (Data Prep & Sync)
量化系统稳健运行的前提是本地 OHLC 指标的准确。
- **白名单自动化**：每日同步 A 股/基金全量清单，根据成交量、停牌、退市及自定义过滤规则更新。
- **数据回充**：在 `autopilot_master` 开盘任务中执行 `qmt_daily_sync`，预下载所有相关历史数据，确保因子计算不由于网络回弹断流。

---

## 9. Fill-Based 独立记账系统（多引擎仓位物理隔离）

> **应用场景**：T0、T1、FatFish 共用同一账户，必须防止跨引擎仓位踩踏。

### 9.1 设计原则（三条物理定律）

| 定律 | 规则 | 反例 |
|---|---|---|
| **物理致盲** | 每个引擎只读自己的账本，禁止在主循环读 `query_stock_positions` | T0 读到 T1 的 3000 股，以为是自己的 |
| **零库存基准** | T0（日内策略）每日启动时强制 `current_lots=0` | 启动时继承昨日遗留格数，当天继续加格超限 |
| **Fill-Based 记账** | 发单后注册 pending，仅在 `on_order_trade` 回调确认成交后更新账本 | 发单后乐观 `current_lots ±1`，委托撤单后账本与实盘永久错位 |

### 9.2 实现模板

```python
# ── 模块级全局（多线程共享）──
import threading
runtime_state: dict = {}           # 提升为模块级供回调访问
_t0_pending_lock = threading.Lock()
_t0_pending: dict[int, dict] = {} # {seq: {code, direction, qty, sent_at}}
PENDING_TIMEOUT_SEC = 60
PENDING_SWEEP_SEC   = 30

# ── 启动：零库存基准（reconcile-v2）──
def reconcile_positions_with_real(yaml_targets: dict) -> dict:
    state = {}
    for code, cfg in yaml_targets.items():
        state[code] = {
            "current_lots": 0,   # ← 强制零，不查 QMT 总持仓
            "volume":       0,
            "base_price":   0.0,
            ...
        }
    return state

# ── 下单：注册 pending（不乐观更新）──
seq = xt_trader.order_stock(acc, code, xtconstant.STOCK_BUY, qty, ...)
if seq > 0:
    with _t0_pending_lock:
        _t0_pending[seq] = {"code": code, "direction": "buy", "qty": qty, "sent_at": time.time()}
    rs['base_price'] = current_price   # 只更新中枢，不碰 current_lots

# ── 回调：成交后精确入账 ──
class GridTraderCallback(XtQuantTraderCallback):
    def on_order_trade(self, trade):
        global runtime_state
        with _t0_pending_lock:
            meta = _t0_pending.pop(trade.order_id, None)
        if meta is None:
            return  # 不是 T0 发的单，忽略（其他引擎成交）
        rs = runtime_state.get(meta["code"])
        if rs is None:
            return
        filled = trade.traded_volume
        lots   = filled // 100
        if meta["direction"] == "buy":
            rs["current_lots"] += lots
            rs["volume"]       += filled
        else:
            rs["current_lots"] = max(0, rs["current_lots"] - lots)
            rs["volume"]       = max(0, rs["volume"] - filled)
        _save_runtime_state(runtime_state, STATE_FILE)
```

### 9.3 Pending 超时巡检（补丁 C）

每 30 秒在主循环中调用，防止网络闪断导致成交回报丢失：

```python
def _t0_sweep_stale_pending(xt_trader, acc):
    now = time.time()
    stale = {seq: m for seq, m in _t0_pending.items() if now - m['sent_at'] > PENDING_TIMEOUT_SEC}
    for seq, meta in stale.items():
        trades = xt_trader.query_stock_trades(acc) or []
        filled = sum(t.traded_volume for t in trades if t.order_id == seq)
        if filled > 0:
            rs = runtime_state.get(meta['code'])
            if rs:
                lots = filled // 100
                if meta['direction'] == 'buy':
                    rs['current_lots'] += lots; rs['volume'] += filled
                else:
                    rs['current_lots'] = max(0, rs['current_lots'] - lots)
                    rs['volume'] = max(0, rs['volume'] - filled)
                _save_runtime_state(runtime_state, STATE_FILE)
        with _t0_pending_lock:
            _t0_pending.pop(seq, None)
```

### 9.4 监控日志标志

- `[T0成交·Fill]` — 回调成功更新账本
- `[T0·Sweep]` — 超时巡检补记
- `[Reconcile-v2]` — 零库存初始化完成

### 9.5 多引擎防火墙配置矩阵

每新增一个策略，**所有其他策略**的防火墙必须同步加入新策略的账本文件：

| 账本文件 | 保护的策略领土 |
|---|---|
| `rotation_targets.yaml` + `rotation_holdings.json` | ETF 轮动（双源） |
| `sniper_holdings.json` | Sniper |
| `t1_grid_ledger.yaml` | T1 Grid |
| `grid_targets.yaml` | T0 Grid |

---

## 3. 订单标识规范（更新）

| 策略类型 | strategyName | orderRemark |
|---|---|---|
| T0 买入 | `T0_Grid` | `T0_Buy` |
| T0 常规卖 | `T0_Grid` | `T0_Sell` |
| T0 止盈 | `T0_Grid` | `T0_TP` |
| T0 止损 | `T0_Grid` | `T0_SL` |
| T0 收盘清仓 | `T0_Grid` | `T0_EOD` |
| T0 孤儿清退 | `T0_Grid` | `T0_Orphan` |
| T1 Grid | `T1_Grid` | `T1_Buy` / `T1_Sell` |
| Sniper | `Sniper` | `Entry` / `Exit` |
| ETF 轮动 | `ETF_Rota` | `Buy` / `Sell` |

---

## 10. T1 账本一致性铁律 (Ledger Consistency Law)

> 状态机 `current_grid`（逻辑指针）与 `grid_inventory`（物理抽屉）必须保持 100% 强同构。
> **任何三态映射的破坏，一律定性为致命 Bug。**

### 10.1 三态定义

| 状态 | 条件 | 物理要求 |
|---|---|---|
| **空仓基准** | `current_grid == 0` | `available_shares == 0`；`grid_inventory` 有没有无所谓 |
| **防守部署** | `current_grid == 1` | 必须且仅有 `grid_inventory: {'1': {...}}` 一个节点 |
| **纵深防御** | `current_grid == N` | 必须有连续完整的 `'1'` ~ `'N'` 共 N 个节点，缺一 KeyError 程序暴毙 |

### 10.2 一致性校验脚本（盘前必跑）

```python
import yaml

def check_ledger_consistency(ledger_path):
    with open(ledger_path, encoding='utf-8') as f:
        d = yaml.safe_load(f)
    errors = []
    for code, rec in d.items():
        grid  = int(rec.get('current_grid', 0))
        avail = int(rec.get('available_shares', 0))
        inv   = rec.get('grid_inventory') or {}

        # 铁律 1：grid==0 时 available_shares 必须为 0
        if grid == 0 and avail != 0:
            errors.append(f'[{code}] ❌ grid=0 但 available_shares={avail}')

        # 铁律 2：grid==N 时必须存在连续的 1..N 槽
        if grid > 0:
            for i in range(1, grid + 1):
                if str(i) not in inv:
                    errors.append(f'[{code}] ❌ grid={grid} 但 slot {i} 缺失')

            # 铁律 3：inventory 总股数必须等于 available_shares
            total = sum(v.get('filled_qty', 0) for v in inv.values()
                        if v.get('status') == 'holding')
            if total != avail:
                errors.append(f'[{code}] ⚠️ inv_total={total} ≠ available_shares={avail}')

    if errors:
        for e in errors:
            print(e)
        raise SystemExit('❌ 账本一致性校验失败，禁止启动 T1 执行器！')
    print('✅ 账本一致性校验通过')
```

### 10.3 常见违规案例

| 违规 | 现象 | 修复 |
|---|---|---|
| `current_grid=1` 但从未成交 | `available_shares=0` 且无 `grid_inventory` | 改为 `current_grid=0` |
| 手动卖出后未清 inventory | `grid_inventory` 有残余 slot | 对应 slot 的 `status` 改为 `sold`，或删除该 slot |
| 均分非 100 整数倍 | `filled_qty=2325` | 用实际成交单量重算，确保每格均为 100 的倍数 |

---

## 11. 量化网格执行四大铁律

> 以下四条是经实盘踩坑提炼的不可妥协的架构物理定律。**写任何新策略前必须对照此清单检查，违反即为 Bug。**

### 铁律一：绝对禁止乐观更新 (No Optimistic Updates)

发单后**绝对禁止**立刻修改可用股数或格数指针。  
**唯一合法的写账路径**是通过 `on_stock_trade` 异步回调，按 QMT 实际推送的 `traded_volume` 写入账本。

```python
# ❌ 致命反例
seq = xt_trader.order_stock(...)
rs["current_lots"] += 1        # ← 委托可能废单，账本永久多记
rs["available_shares"] -= qty  # ← 撤单后永不恢复

# ✅ 正确：发单只注册 pending，不动账本
_pending[seq] = {"code": code, "grid": level, "direction": "buy", ...}
# → on_stock_trade 回调触发后，再按 traded_volume 写账
```

> **故障案例**：T0 乐观 `±1` 导致多个标的格数虚高，全天买入被 `max_lots` 限制拦截，资金空置。

---

### 铁律二：盲人摸象隔离原则 (Blind-Isolation)

多引擎共存时（T0 / T1 / FatFish 同账户），任何执行器**绝对禁止**调用 `query_stock_positions()` 查询全局物理总持仓来做买卖决策。  
**只能**读取自身独立账本（`grid_inventory` / `runtime_state`）。

```python
# ❌ 致命反例
pos_list = xt_trader.query_stock_positions(acc)   # T0 读到 T1 的 3000 股！
rs["current_lots"] = pos.volume // 100            # 把 T1 持仓当 T0 自己的

# ✅ 正确：启动时 reconcile 强制零库存，盘中只靠回调入账
for code in yaml_targets:
    state[code] = {"current_lots": 0, ...}        # 零库存基准，不查 QMT
```

> **故障案例**：T0 启动读到 T1 的 3000 股底仓，`current_lots >= max_lots`，全天买入被封锁。

---

### 铁律三：网格买卖绝对对称 (Symmetrical Liquidation)

卖出退格时，**绝对禁止**用现价重新计算卖出量。  
**必须**从 `grid_inventory` 精确读取该格的 `filled_qty`，原数归还，消灭 100 股碎股残余。

```python
# ❌ 致命反例（动态计算卖出量）
sell_qty = int(per_grid_capital / last_price / 100) * 100  # 价格变动 → 卖出量≠买入量 → 100股残余

# ✅ 正确（从 inventory 读实际买入量）
slot     = inventory.get(str(current_grid), {})
sell_qty = slot.get("filled_qty", 0)              # 买了多少还多少，严格对称
if sell_qty <= 0:
    continue  # inventory 无记录则不卖
```

> **故障案例**：513920 以 3100 股买入，卖出时按 `5000/1.74` 重算得 2800 股，产生 300 股残余遗留账户。

---

### 铁律四：并发 IO 与内存锁 (Concurrency Locks)

QMT 回调线程与主循环线程**严格异步并发**。凡涉及以下操作，**必须用 `threading.Lock()` 包裹，零例外**：

| 被保护对象 | 锁变量 |
|---|---|
| `.state` 账本文件的读写（`load_ledger` / `save_ledger`） | `_ledger_lock` |
| `_pending` 订单注册字典的读写 | `_pending_orders_lock` / `_t0_pending_lock` |
| `runtime_state` 字典的内存修改 + 落盘 | `_runtime_io_lock` |

```python
# ✅ 标准模板
_ledger_lock = threading.Lock()

def load_ledger() -> dict:
    with _ledger_lock:
        ...

def save_ledger(ledger: dict):
    with _ledger_lock:
        ...

# 回调线程写账
def on_stock_trade(self, trade):
    with _pending_lock:
        meta = _pending.pop(trade.order_id, None)
    with _runtime_io_lock:          # 字典修改也加锁
        rs["current_lots"] += ...
    _save_runtime_state(...)        # save 内部自带锁
```

> **故障案例**：5 只 ETF 同时成交触发 5 个并发回调，无锁写 YAML → 文件截断清空 → 次日启动账本全部归零。

---

## 12. 截面动量司令部标准模式（Cross-Sectional Momentum Pipeline）

> **首次实盘落地**：`momentum_master.py`（2026-04-16）  
> **触发时机**：每日盘后（15:30+）或盘前（09:00前）一次性运行  
> **输出**：`.state/momentum_slots.json` → 供下游 Executor 消费

### 12.1 算法核心（M-Score = 截面夏普比率近似）

```python
from scipy.stats import linregress

def calculate_momentum_score(prices: np.ndarray) -> dict:
    n = len(prices)
    if n < WINDOW or np.any(np.isnan(prices)):
        return {"m_score": -999.0, "slope": 0.0, "volatility": 0.0, "rsi": 0.0}

    # 1. 归一化（起点→0，跨品种公平比较）
    normalized = (prices - prices[0]) / prices[0]

    # 2. 线性回归斜率（趋势强度）
    x = np.arange(n, dtype=float)
    slope, *_ = linregress(x, normalized)

    # 3. 年化波动率（风险惩罚项）
    daily_ret = np.diff(prices) / prices[:-1]
    vol = float(np.std(daily_ret) * np.sqrt(252))
    if vol < 1e-8 or np.isnan(vol):
        vol = 1e-8  # 防零除

    # 4. M-Score：斜率 / 波动率（夏普比率的截面近似）
    m_score = float(slope) / vol

    # 5. RSI(14) — 鱼尾过滤辅助指标
    gains  = np.where(daily_ret > 0, daily_ret, 0.0)
    losses = np.where(daily_ret < 0, -daily_ret, 0.0)
    g14    = gains[-14:] if len(gains) >= 14 else gains
    l14    = losses[-14:] if len(losses) >= 14 else losses
    avg_g  = float(np.mean(g14)) if len(g14) > 0 else 0.0
    avg_l  = float(np.mean(l14)) if len(l14) > 0 else 0.0
    rsi    = 100.0 if avg_l < 1e-12 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)

    return {"m_score": round(m_score, 4), "slope": round(float(slope), 6),
            "volatility": round(vol, 4), "rsi": round(rsi, 2)}
```

### 12.2 三道防线（顺序不可颠倒）

| 防线 | 判据 | 推荐参数 | 说明 |
|---|---|---|---|
| **第一道** | 流动性底线 | `avg_amount_5d ≥ 5亿` | 利用 universe 已有字段，零额外请求 |
| **第二道** | 正斜率过滤 | `slope > 0` | 仅允许上涨趋势标的进入赛圈 |
| **第三道** | RSI 鱼尾过滤 | `RSI < 75` | 拒绝极度超买标的，防高位接盘 |
| **最终产出** | M-Score 降序 TOP_N | `TOP_N = 3` | 与下游 Executor 席位数对齐 |

### 12.3 数据管道规范

```python
# 参数常量（集中在模块头部，禁止散落代码中）
LOOKBACK_DAYS = 250   # 约1年，与 qmt_daily_sync 保持对齐
WINDOW        = 20    # 动量计算窗口（约1个月）
TOP_N         = 3     # 只取最强前 3 名

# 本地 QMT 数据读取（不 download，直接读缓存）
market_data = xtdata.get_market_data_ex(
    field_list=["close", "volume", "amount"],
    stock_list=candidates,
    period="1d",
    start_time=start_date,
    end_time=end_date,
)

# 安全访问（quant-safe-patterns §2.1）
for code in candidates:
    if code not in market_data or market_data[code] is None or market_data[code].empty:
        skip_no_data += 1
        continue

# 原子落盘（铁律四：防并发写损坏）
tmp_path = OUTPUT_JSON + ".tmp"
with open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)
os.replace(tmp_path, OUTPUT_JSON)
```

### 12.4 与其他策略的分工边界

| 角色 | 脚本 | 职责 |
|---|---|---|
| 宇宙定义者 | `tools/refine_core_universe.py` | 从全量 ETF 筛选精英候选池（N=60） |
| 动量司令部 | `momentum_master.py` | 从候选池中选出最强 TOP_N，输出 `momentum_slots.json` |
| 执行器 | `momentum_vector_executor.py` ✅ | 读 `momentum_slots.json`，VWAP确认入场，移动止盈退出 |

### 12.5 T+0 白名单查表铁律（T+0/T+1 分类物理真相）

> **铁律**：判断标的是否 T+0 可交易，**永远通过读 `t0_absolute_pool.csv` 白名单**，  
> **永不使用代码前缀推导**（如 `513xxx → T+0`），**永不猜测**。

**根因**：代码前缀规则经常有例外——同一前缀下可同时存在 T+0 与 T+1 标的（如 `513xxx` 中有 QDII ETF 也有 A 股主题 ETF），用前缀推导必然误判。只有交易所官方认证、人工维护的 `t0_absolute_pool.csv` 才是物理真相。

```python
_T0_POOL: set = set()
_T0_POOL_LOADED = False

def _load_t0_pool():
    """从 .state/t0_absolute_pool.csv 加载 T+0 可交易标的集合（模块级缓存）。"""
    global _T0_POOL, _T0_POOL_LOADED
    if _T0_POOL_LOADED:
        return
    try:
        with open(T0_POOL_CSV, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            for row in reader:
                if row and row[0].strip():
                    code = row[0].strip()
                    if len(code) == 9 and "." in code:   # 跳过表头等非代码行
                        _T0_POOL.add(code)
        _T0_POOL_LOADED = True
    except Exception as e:
        pass   # 失败则所有标的按 T+1 处理（保守原则）

def is_t0_eligible(code: str) -> bool:
    """查询 T+0 白名单。永不猜测，永不前缀推导。"""
    _load_t0_pool()
    return code in _T0_POOL
```

**适用场景**（所有需要区分 T+0/T+1 的策略均必须使用此函数）：
- ETF 轮动：保留仓当日是否可换出
- Momentum Executor：退出信号是否能当日执行
- Sniper：买入当日是否可抢时平仓

**文件格式**：`.state/t0_absolute_pool.csv`，每行 `代码,名称`（UTF-8 with BOM），由人工维护，定期由 `tools/etf_name_fetcher.py` 辅助更新

---


## 13. N8N Webhook 推送标准规范（策略三件套之一）

> **地位**：与遥测 CSV、日志文件并列为「策略三件套」，**任何新策略必须同时实现这三件套**。
> **原则**：推有意义的事件，静默无变化扫描（防刷屏，防告警疲劳）。

### 13.1 标准接入模板（每个策略模块复制此块）

```python
import os
from dotenv import load_dotenv
try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

load_dotenv()
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")

def send_webhook(title: str, message: str) -> None:
    """N8N 推送，失败静默（不阻断主逻辑）。timeout=5sec。"""
    if not _HAS_REQUESTS or not N8N_WEBHOOK_URL:
        return
    try:
        requests.post(N8N_WEBHOOK_URL, json={"title": title, "message": message}, timeout=5)
    except Exception:
        pass  # 推送失败不影响交易逻辑
```

### 13.2 推送节点设计原则

| 原则 | 说明 |
|---|---|
| **推有意义的事件** | 开仓/平仓/换仓/熔断/告警 → 必须推 |
| **静默无变化扫描** | `HOLD` / 无行动 / 空仓巡逻 → 不推 |
| **熔断大推送代替子推送** | 已有系统级熔断推送时，子节点的`MELTDOWN_REJECT`不推（避免重复） |
| **告警类必须推** | 委托超时/撤单/账本异常/席位解算失败 → 必须推（这是最后兜底） |

### 13.3 各策略标准推送节点清单

| 策略模块 | 推送场景 | 标题参考 |
|---|---|---|
| 任意 Master 解算 | 席位解算成功/失败 | `🛸 XXX 席位已解算` / `🚨 XXX 席位解算失败` |
| 任意 Executor 开仓 | 首格/首仓买入 | `🌊 首网开仓` |
| 任意 Executor 加仓 | 第N格买入 | `⚓ 深潜加仓` |
| 任意 Executor 止盈 | 各类止盈触发 | `💰 止盈触发` |
| 任意 Executor 熔断 | 系统总仓位/风控触发 | `🚨 系统级熔断` |
| 任意 Executor 时间止损 | 超期强平 | `💀 时间止损触发` |
| Sniper 超时撤单 | 委托30秒未成交 | `⚠️ Sniper 委托超时撤单` |
| 轮动换仓 | 真实买卖动作产生 | `🔄 轮动换仓执行` |

### 13.4 配置依赖

- 环境变量：`.env` 文件中 `N8N_WEBHOOK_URL=https://...`
- 需安装：`pip install python-dotenv requests`
- 失败不崩：`send_webhook` 内部 try/except 全包，超时5秒，不阻断交易逻辑

### 13.5 当前各模块接入状态

| 模块 | N8N接入 | 备注 |
|---|---|---|
| `etf_ou_grid_master.py` | ✅ 已接入（2节点） | 席位解算结果 |
| `etf_ou_grid_executor.py` | ✅ 已接入（6节点） | 开/加/止盈/熔断/时间止损 |
| `macro_rotation_executor.py` | ✅ 已接入 | 换仓/止损时推送 |
| `sniper_entry_executor.py` | ✅ 已接入 | 超时撤单告警 |
| `momentum_master.py` | ❌ 待接入 | TO-DO：扫描完毕推TOP3结果 |
| `hawkes_executor.py` | ❌ 待接入 | TO-DO：开火/止盈/止损推送 |

---

## 14. Fill-Based 原子记账铁律（§14）

> **适用范围**：所有持有槽位账本（JSON/YAML）的执行引擎，包括 `macro_rotation_executor`、`fat_fish_executor`、`underdog_executor`、`sniper_entry_executor` 等。

**核心思想**：下单只是"意图"，**成交才是事实**。账本必须由成交事实驱动，绝不由下单推断。

---

### 铁律 14.1 — Pending 注册表（禁止 time.sleep 推断成交）

❌ **禁止写法**：
```python
seq = trader.order_stock(...)
time.sleep(2)          # ← 假设2秒内必成交
slots["shares"] = qty  # ← 下单≠成交
_save_slots(slots)
```

✅ **合规模板**：
```python
# 1. 下单，拿 seq_id
seq = trader.order_stock(acc, code, ..., qty, ...)

# 2. 注册 pending（不落账本）
with _pending_lock:
    _pending[seq] = {"slot": "slot_a", "direction": "buy", "target_qty": qty, "filled_so_far": 0}

# 3. 账本由 on_order_trade 回调原子写，主流程只等待
time.sleep(8)
_cancel_pending_orders(trader, acc)  # 清场兜底
```

---

### 铁律 14.2 — IO 锁（禁止并发回调裸写账本）

❌ **禁止写法**：
```python
def on_order_trade(self, trade):
    slots = _load_slots()   # ← 无锁，5笔碎单并发=竞态
    slots["shares"] += trade.traded_volume
    _save_slots(slots)      # ← 后写覆盖前写，股数归零
```

✅ **合规模板**：
```python
_slots_lock = threading.Lock()   # 模块级，只定义一次

def on_order_trade(self, trade):
    with _slots_lock:            # ← 持锁期间独占
        slots = _load_slots()
        slots["shares"] += trade.traded_volume
        _save_slots(slots)       # ← 原子完成
```

---

### 铁律 14.3 — 无情清场（禁止留幽灵挂单过夜）

❌ **危险场景**：
```
14:42 下单买 159915，159915 在跌停板
time.sleep(8) 结束，订单仍未成交
主程序退出 → 14:55 突然成交 → 账本无记录 → 幽灵持仓
```

✅ **强制执行**：每次 `time.sleep(N)` 后，**必须**跟一次：
```python
time.sleep(8)
_cancel_pending_orders(trader, acc)
# 实现：query_stock_orders → 状态50/51/52 → cancel_order_stock → N8N告警
```

**QMT 委托状态码**：`50=未报` / `51=待成交` / `52=部分成交` → 均应撤单

---

### 铁律 14.4 — 加权均价（禁止覆盖式成本记录）

❌ **禁止写法（Topup 时直接覆盖均价）**：
```python
slots["cost"] = new_price   # ← 丢失历史成本信息
```

✅ **合规模板（加权重算）**：
```python
old_shares = slots.get("slot_a_shares", 0)
old_cost   = slots.get("slot_a_cost",   filled_px)
new_shares = old_shares + filled_qty

if new_shares > 0 and old_shares > 0:
    slots["slot_a_cost"] = round(
        (old_cost * old_shares + filled_px * filled_qty) / new_shares, 4
    )
else:
    slots["slot_a_cost"] = round(float(filled_px), 4)
slots["slot_a_shares"] = new_shares
```

---

### 铁律 14.5 — 预写与回写分离（禁止回调与主流程双写同一字段）

**职责分工**：

| 字段 | 由谁写 | 时机 |
|------|--------|------|
| `slot_x`（标的代码） | 主流程 | 下单前预写（占位） |
| `slot_x_capital` | 主流程 | 下单前预写（资金意图） |
| `slot_x_shares` | `on_order_trade` | 成交后原子写 |
| `slot_x_cost` | `on_order_trade` | 成交后加权重算 |
| `slot_x_hwm` | `on_order_trade` | 首次成交时初始化（只升不降） |
| `updated_at` | 主流程 | 最终 `_load_slots()` 重读后补写 |

> 主流程不得写 shares/cost/hwm；回调不得覆盖 slot/capital。

---

### 铁律 14.6 — 精准卖出（禁止全仓物理清盘误杀手动仓）

❌ **危险写法**：
```python
# 卖出时物理查仓全量卖出
qty = int(target_pos.can_use_volume)  # 包含手动加仓的股数
```

✅ **合规模板**：
```python
def sync_sell(trader, acc, code, slot, known_qty=0):
    if known_qty > 0:
        qty = int((known_qty // 100) * 100)   # ← 精准账本数量
    else:
        # 账本无记录时兜底物理查仓（旧仓位迁移期容错）
        pos = next((p for p in trader.query_stock_positions(acc) if p.stock_code == code), None)
        qty = int(pos.can_use_volume) if pos else 0
```

> `known_qty` 来自账本 `slot_x_shares`，由 Fill-Based 回调写入，保证等于本策略实际买入量。

---

### 铁律 14.7 — 账本字段命名规范（多槽位策略标准）

所有多槽位策略（Rotation / FatFish / Underdog）账本字段统一按以下规范命名：

```json
{
  "slot_a":          "511260.SH",   // 标的代码（None = 空仓）
  "slot_a_capital":  75000.0,       // 资金配额（意图值，由主流程写）
  "slot_a_shares":   552,           // 实际持仓股数（由回调写）
  "slot_a_cost":     135.8123,      // 加权均价（由回调写）
  "slot_a_hwm":      136.215,       // 最高水位线，止盈锚点（只升不降）
  "updated_at":      "2026-04-24 15:07:00"
}
```

> **注意**：账本 JSON 禁止使用 `//` 注释，标准 JSON 不支持，会导致 `json.load()` 崩溃。
