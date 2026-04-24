---
name: quant-safe-patterns
description: 量化交易系统安全编码规范 - 基于实战踩坑总结的 xtquant/statsmodels/pandas 防御模式，用于写新代码时主动避免已知陷阱。【写新代码前必须先读本文件，对照清单检查】
---

# 量化交易系统安全编码规范 (Quant Safe Patterns)

> 本 Skill 收录了在 Z690 (Quant-PC) miniQMT 生产环境中**真实踩过的坑**，
> 每条规范都有对应的故障案例佐证。**写新代码时必须对照此清单检查。**

---

## 模块一：xtquant 正确导入规范

### ✅ 标准导入模板（必须完整，不可省略）

```python
# 行情模块
from xtquant import xtdata, xtconstant   # ← xtconstant 必须与 xtdata 同行导入！
# 交易模块
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
```

### ❌ 已知致命错误

```python
# 错误写法 —— xtconstant 未导入，下单时必然 NameError
from xtquant import xtdata  # 漏掉 xtconstant

# 然后下单时：
xt_trader.order_stock(acc, code, xtconstant.STOCK_BUY, ...)
# → NameError: name 'xtconstant' is not defined
```

> **故障案例**：`stat_arb_executor.py` 因此在所有下单路径上静默崩溃，
> `try/except` 吞掉 `NameError` 返回 `-1`，全天无交易记录且 log 无报错。
> **诊断方法**：无交易时先查 `.state/action_logs/` → 再查 `logs/` → 若均无条目 → 必为代码级静默崩溃。

---

## 模块二：xtdata 数据获取防御模式

### 2.1 `get_market_data_ex` 返回字典的安全访问

```python
data = xtdata.get_market_data_ex(['close'], codes, '1d')

# ❌ 错误：假设所有 code 都在字典中
for code in codes:
    if not data[code].empty:   # KeyError！若 code 无数据则不在 dict 中

# ✅ 正确：先判断 key 存在
for code in codes:
    if code in data and not data[code].empty:
        s = data[code]['close']
```

### 2.2 快速失败防护（管道早退）

```python
df_list = []
for code in codes:
    if code in data and not data[code].empty:
        df_list.append(data[code]['close'].rename(code))

if not df_list:
    raise RuntimeError("❌ 所有标的均无有效历史数据，请检查 QMT 连接与数据下载。")

df = pd.concat(df_list, axis=1)
```

### 2.3 `get_full_tick` 字段访问安全模式

```python
tick = xtdata.get_full_tick([code]).get(code, {})  # 不存在时给空 dict

# 防御性读取价格
price = tick.get('lastPrice', 0)
# 防御性读取昨收（字段名在不同版本 miniQMT 中可能不同）
pre_close = tick.get('lastClose', tick.get('preClose', tick.get('lastPrice', 0)))
```

---

## 模块三：OLS 回归安全模式（statsmodels）

### 3.1 `add_constant` + `params` 的陷阱

```python
import statsmodels.api as sm

# ❌ 错误：X 传入 Pandas Series 时，add_constant 返回 DataFrame 列名为 ["const", code_B]
# 当 index 不对齐时 params[code_B] 会 KeyError
X_with_const = sm.add_constant(X)          # X 是 Series
hedge_ratio = ols_result.params[code_B]    # ← 危险：按列名取值

# ✅ 正确：传入 .values（numpy），用位置索引取系数
X_with_const = sm.add_constant(X.values)   # numpy array，无列名污染
Y_arr = Y.values
result = sm.OLS(Y_arr, X_with_const).fit()
intercept  = result.params[0]  # 截距
hedge_ratio = result.params[1]  # β 系数（位置固定，不受列名影响）
```

### 3.2 Hedge Ratio 合理性检查

```python
if hedge_ratio <= 0:
    continue  # 对冲比率必须为正数，否则两标的方向相同，无套利意义
              # 合理范围参考：0.3 ~ 3.0
```

### 3.3 OU 过程半衰期计算防零溢出

```python
def calculate_halflife(spread):
    spread_lag = spread.shift(1).dropna()
    spread_diff = spread.diff().dropna()
    spread_lag = spread_lag.loc[spread_diff.index]

    result = sm.OLS(spread_diff.values, sm.add_constant(spread_lag.values)).fit()
    theta = result.params[1]

    # ① theta >= 0 → 序列发散或随机游走，无均值回归特性
    if theta >= 0:
        return np.inf
    # ② abs(theta) 极小 → 防止 -log(2)/~0 溢出产生超大 half_life 绕过过滤
    if abs(theta) < 1e-10:
        return np.inf

    return -np.log(2) / theta
```

---

## 模块四：Pandas 排序与数据类型陷阱

### 4.1 数值排序必须存浮点数，不能格式化为字符串

```python
# ❌ 错误：科学计数法字符串按字典序排，"1.0e-05" > "2.0e-04" 在字典序中为 False
results.append({'ADF_P_Value': f"{p_value:.8e}"})   # 字符串！
df.sort_values('ADF_P_Value')  # → 排序结果错误，最差标的排首位

# ✅ 正确：存浮点数（输出报告时再格式化）
results.append({'ADF_P_Value': p_value})             # 浮点数
df.sort_values('ADF_P_Value')  # → 数值升序，正确

# 需要展示时才格式化：
df['ADF_P_Value_str'] = df['ADF_P_Value'].map(lambda x: f"{x:.2e}")
```

> **故障案例**：`pair_researcher.py` 将 p_value 存为字符串，导致 `tradable_pairs_halflife.csv`
> 中排序颠倒，`stat_arb_executor` 每次都优先尝试协整关系最弱的配对。

---

## 模块五：xtquant 架构认知

### 5.1 C/S 架构 — 必须先启动 miniQMT 客户端

```
所有 xtdata/xttrader 调用
    → TCP → 127.0.0.1:58610 → miniQMT 进程 → 本地数据仓库
```

**不是**直接读文件，而是通过进程间通信。miniQMT 进程挂掉 = 所有调用失败。

### 5.2 离线能力边界

| 操作 | miniQMT在线 | 券商服务器在线 |
|---|---|---|
| `get_market_data_ex()` 读历史K线 | ✅ 需要 | ❌ 不需要（本地缓存） |
| `download_history_data2()` 增量补充 | ✅ 需要 | ✅ 需要 |
| `get_full_tick()` 实时价格 | ✅ 需要 | ✅ 需要 |
| `order_stock()` 下单 | ✅ 需要 | ✅ 需要 |

### 5.3 最佳调度实践

- **历史数据分析脚本**（如 `pair_researcher.py`）：收盘后（17:00）运行，无需实时行情
- **实时执行器**（如 `stat_arb_executor.py`）：交易时段运行，需完整连接

---

## 模块六：静默错误诊断 SOP

当程序运行但**无任何交易记录**时，按以下顺序排查：

```
Step 1: 检查 .state/action_logs/action_YYYYMMDD.jsonl
         → 有 StatArb/Sniper/T0_Grid 条目？
           - 是 → 进入策略逻辑层排查（阈值/风控）
           - 否 → Step 2

Step 2: 检查 logs/YYYYMMDD_xxx_executor.log
         → 看到"阶段2启动"但马上"引擎静默"？
           - 是 → 代码级静默崩溃：try/except 吞掉了异常
           - 否 → Step 3

Step 3: 在 safe_execute_and_lock 或等效 try/except 中临时加：
         except Exception as e:
             print(f"❌ [DEBUG] {type(e).__name__}: {e}")
             # → 具体报错类型会暴露根因
```

**已知静默崩溃模式**：
- `NameError: name 'xtconstant' is not defined` → 模块未导入
- `KeyError: 'code_B'` → OLS params 按列名取值失败
- `ZeroDivisionError` → theta 接近零时除法溢出

---

## 模块七：统计套利参数配置参考值

```python
# pair_researcher.py 规范配置
LOOKBACK_DAYS  = 500    # 约2年，过短噪音多，过长 Regime Shift 失真
MAX_P_VALUE    = 0.05   # ADF 95% 置信度
MIN_HALF_LIFE  = 5.0    # <5天往往是微观噪声非真正协整
MAX_HALF_LIFE  = 30.0   # >30天资金周转太慢
MIN_SAMPLES    = 250    # 配对共同历史数据最少250天（约1年）

# stat_arb_executor.py 规范配置
Z_SCORE_ENTRY  = 2.0    # 偏移2σ才开仓
Z_SCORE_EXIT   = 0.0    # 回归均值平仓
MA_FILTER      = 60     # 跌破60日均线的标的不入场

# 排序优先级（Ernest P. Chan 框架）
# ① ADF p值（显著性=可靠性） > ② 半衰期（资金效率） > ③ Hedge Ratio 合理性
```

---

## 模块八：QMT 持仓查询 T+1 结算延迟陷阱

### 8.1 买单提交后 `query_stock_positions` 短暂报 volume=0

```python
# ❌ 错误：买单提交后下一个 tick 查询持仓，volume 可能暂时为 0（T+1 结算延迟）
# 直接清零会"遗忘"刚买入的仓位，导致全天无法高抛
if target_pos is None or target_pos.volume == 0:
    rs['current_lots'] = 0   # ← 危险！买入后的瞬态 0 会触发此处
    continue

# ✅ 正确：先检查内部账本的 volume 字段，内部有记录则说明是 T+1 延迟误判
if target_pos is None or target_pos.volume == 0:
    internal_volume = rs.get('volume', 0)
    if internal_volume > 0:
        # QMT 报 0 但内部账本有仓 → T+1 延迟，跳过，不归零
        print(f"⚠️ [手动清仓保护] {code} QMT持仓暂报0，内部账本={internal_volume}，疑似结算延迟，跳过。")
        continue
    # 内部账本也是 0 → 真正的手动清仓
    rs['current_lots'] = 0
    continue
```

> **故障案例**：`520500.SH` 10:52 买入 5400 股后同一分钟触发误判，`current_lots` 归零，
> 全天无高抛逻辑执行，5400 股隔夜持有，次日需手动修复 `grid_state.json`。

---

## 模块九：Python 闭包晚绑定陷阱（for 循环中定义函数）

### 9.1 for 循环内的 `def` 共享最后一个循环变量

```python
# ❌ 错误：所有 sell_save 闭包共享同一个 `code` 变量（指向循环结束时的最后值）
for code in codes_to_sell:
    def sell_save():
        del holdings[code]   # ← code 是晚绑定，所有闭包都删最后一个 code
        _save_holdings(holdings)
    safe_execute_and_lock(..., sell_save)

# ✅ 正确：用默认参数提前绑定，每个闭包捕获自己的 code
for code in codes_to_sell:
    _code = code
    def sell_save(_c=_code):   # 默认参数在定义时求值，立即绑定
        del holdings[_c]
        _save_holdings(holdings)
    safe_execute_and_lock(..., sell_save)
```

> **故障案例**：`etf_rotation_executor.py` 多标的轮动卖出时，若 A、B 都要卖，
> 两个 `sell_save` 都执行 `del holdings[B]`，导致 A 的账本记录未被清除。

---

## 模块十：轮动策略虚拟账本漏记的防火墙穿透

### 10.1 `rotation_holdings.json` 只记录执行器亲手买入的标的

```python
# ❌ 错误陷阱：rotation_holdings.json 不是实盘真实持仓的镜像！
# 若上次执行失败，实际买入但未写入账本，后续执行会：
# ① 不卖出该标的（漏记）
# ② T0 的轮动防火墙（依赖 rotation_holdings.json）会认为该标的不存在→被识别为孤儿→早盘卖出

# ✅ 正确：每次执行前先"物理扫描补录"——从券商真实持仓反推账本
def _sync_physical_holdings(xt_trader, acc, holdings):
    excluded = _load_excluded_codes()   # T0 grid + Sniper 管辖的标的
    real_positions = xt_trader.query_stock_positions(acc) or []
    for pos in real_positions:
        code = pos.stock_code
        if pos.volume <= 0 or code in excluded or code in holdings:
            continue
        # 补录：用均价作为参考买入价，打上 _synced 标记
        holdings[code] = {
            "qty": int(pos.volume),
            "buy_price": float(pos.open_price),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "_synced": True
        }
    _save_holdings(holdings)
    return holdings
```

### 10.2 轮动资金分配：只对新建标的分配资金，不重复给保留标的

```python
# ❌ 错误：总资金 ÷ 全部目标数，再过滤已持有。保留仓的配额白白浪费。
# 例：A+B → B+C（保留B），C 只得到 50k，另 50k 闲置。
each_fund = TOTAL / len(targets)
buy_dict  = {c: each_fund for c in targets if c not in held}  # B的50k没用上

# ✅ 正确：freed_budget（卖出释放的资金）÷ 新买标的数量
each_fund    = TOTAL / len(targets)         # 每槽标准配额
kept_count   = len(codes_to_keep)
freed_budget = TOTAL - (kept_count * each_fund)  # 保留仓不释放资金
per_new_fund = freed_budget / len(targets_to_buy) if targets_to_buy else 0
buy_dict     = {c: per_new_fund for c in targets_to_buy}
# 例：A+B → B+C，freed=50k，C 得到完整 50k ✅
# 例：A+B → C，  freed=100k，C 得到全部 100k ✅
```

> **设计原则**：持仓保留（keep）= 仓位不变 + 配额转移给新标的。
> 绝不对保留标的重复买入，绝不因保留标的而浪费可用资金。

---

## 模块十一：XtQuant API 命名陷阱（query_account vs query_stock_asset）

### 11.1 查询账户资产

```python
# ❌ 错误：query_account 方法不存在，运行时 AttributeError（被 except 吞掉）
account_obj = xt_trader.query_account(acc)
real_cash = float(getattr(account_obj, 'cash', 0) or 0)
# → 潮汐资金池 try/except 捕获后打印 "查询账户失败"，然后用 YAML 原始配置，
#   但不报崩溃，程序继续运行，调试极难。

# ✅ 正确
asset = xt_trader.query_stock_asset(acc)
real_cash = float(asset.cash if asset else 0)
```

> **故障案例**：`t0_multigrid_executor.py` 潮汐资金池在 2026-03-23 全天 `[查询账户失败]`，
> T0 使用固定 YAML 配置而非动态杠杆缩放，资金利用率大幅低于设计值。

| 目的 | 正确方法 | 返回类型 |
|---|---|---|
| 查询可用现金 / 总资产 | `query_stock_asset(acc)` | `StockAsset` (.cash, .total_asset) |
| 查询持仓 | `query_stock_positions(acc)` | `list[StockPosition]` |
| 查询今日成交 | `query_stock_trades(acc)` | `list[StockTrade]` |
| 查询委托单 | `query_stock_orders(acc)` | `list[StockOrder]` |

---

## 模块十二：多引擎乐观记账反模式（T0/T1 共账踩踏）

### 12.1 乐观写账本（Optimistic Update）触发多引擎踩踏

```python
# ❌ 危险反模式：发单后立即乐观更新账本
seq = xt_trader.order_stock(acc, code, xtconstant.STOCK_BUY, buy_qty, ...)
rs['current_lots'] += 1    # ← 委托不一定成交！撤单或废单后账本永久多记 1
rs['volume']       += buy_qty

# ❌ 更危险：启动时用 query_stock_positions 初始化 T0 账本
pos_list = xt_trader.query_stock_positions(acc)  # 读到 T1 的 3000 股！
```

```python
# ✅ 正确：Fill-Based 延迟记账（见 quant-v4-patterns §9）
# 发单 → 注册 pending → 等 on_order_trade 回调 → 回调更新账本
seq = xt_trader.order_stock(...)
if seq > 0:
    with _t0_pending_lock:
        _t0_pending[seq] = {"code": code, "direction": "buy", "qty": buy_qty, "sent_at": time.time()}
    # current_lots 不动，等回调

def on_order_trade(self, trade):
    meta = _t0_pending.pop(trade.order_id, None)
    if meta:
        rs["current_lots"] += trade.traded_volume // 100   # 只加实际成交量
```

> **故障案例**：T0 启动时调用 `query_stock_positions` 误读 T1 的 3000 股底仓，
> 以为是自己持有 3 格，买入逻辑因 `current_lots >= max_lots` 被阻断，全天无法交易。

---

## 模块十三：T0 网格宽度（spread_pct）设计反模式

### 13.1 负期望底线陷阱（MIN_T0_SPREAD = 0.004）

```python
# ❌ 致命反模式：0.4% 是纯负期望
MIN_T0_SPREAD = 0.004  # 物理摩擦成本 ≈ 双边万一佣金 + 单边1-2 Tick ≈ 0.2%-0.3%
                        # 净盈利空间仅 0.1-0.2%，交易所规模化后必然亏损

# ✅ 正确：1.0% 是能击穿摩擦成本墙的真正底线
MIN_T0_SPREAD = 0.010  # 绝对底线：1.0%
MAX_T0_SPREAD = 0.030  # 适配港股/商品ETF极端高波动
_FALLBACK_SPREAD = 0.012  # ATR数据缺失时安全回退
```

> **故障案例**：`t0_master.py` 长期使用 `MIN=0.004 / MAX=0.015`，所有低波动 ETF
> spread 被钳制在 0.4%，每笔交易扣除手续费和滑点后几乎零盈利，实际以给券商打工告终。

### 13.2 ATR_pct 列缺失 —— 静默用错误值计算 spread

```python
# ❌ 危险：ATR_pct 列根本不在 df_res 中！fallback 到 Total_Return（回测总收益率）
atr_pct_val = float(row.get('ATR_pct', row.get('Total_Return', 0.02)))
# → Total_Return 是百分比形式的回测总收益，用它乘以 atr_multiplier 完全是随机值

# ✅ 正确：在 all_results 收集时显式加入 ATR_14_pct 字段
all_results.append({
    ...,
    "ATR_14_pct": _calc_atr14_pct(df),  # 从回测 DataFrame 现场计算，保证列存在
})

# 然后在钳制器中：
atr14_raw = float(row.get('ATR_14_pct', 0.0))
if atr14_raw <= 0 or atr14_raw > 0.15:
    spread_pct_raw = _FALLBACK_SPREAD    # 异常 → 安全回退
else:
    spread_pct_raw = atr14_raw * 0.3    # 日均振幅的 30% 作为基准切片
spread_pct_fin = max(MIN_T0_SPREAD, min(spread_pct_raw, MAX_T0_SPREAD))
```

> **故障案例**：`t0_master.py` L548 在 `df_res` 中找 `ATR_pct` 列，
> 该列从未被写入，`row.get('ATR_pct', row.get('Total_Return', 0.02))` 实际上
> 用 **回测总收益率** 当做 ATR 来计算 spread，完全是随机值。
> 修复：新增 `_calc_atr14_pct(df)` 辅助函数（容错，异常返回 0.0）。

### 13.3 动态公式参考（v2 钳制器，2026-03-26 上线）

| ATR_14_pct | raw_spread (×0.3) | final_spread |
|---|---|---|
| ≤1.5%（低波） | < 1.0% → 托底 | **1.0%** |
| 3.5%（典型A股ETF） | **1.05%** | 1.05% |
| 8.0%（港股ETF） | **2.4%** | 2.4% |
| >15% 或数据异常 | fallback | **1.2%** |

### 13.4 遗留死代码清理原则

- `get_safe_high_vol_etfs`（IOPV 溢价探针 + QDII 物理黑名单）已于 2026-03-26 删除
- **原则**：标的改为人工维护（`fixed_t0_target.yaml`）后，所有自动过滤函数即变为死代码，应立即物理删除，避免误用和维护负担
