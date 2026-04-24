import yaml
import os
import sys
import json
import time
import requests
import math
from datetime import datetime
from dotenv import load_dotenv
from xtquant import xtdata, xtconstant
from xtquant.xttrader import XtQuantTrader
from quant_logger import record_action

# =============================================================================
# 1. 策略配置
# =============================================================================
load_dotenv()
ACCOUNT_ID = os.getenv("ACCOUNT_ID")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")
QMT_PATH = os.getenv("QMT_PATH")

# 物理忽略代码 (T0 网格底仓)
T0_BLACKLIST = ['513310.SH', '159509.SZ', '513500.SH'] 
# 【资金隔离墙】绝对物理隔离上限，严禁读取账户全部可用资金，保护 T0 和 Sniper 现金流
ROTATION_TOTAL_CAPITAL = 100000  

_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(_DIR, ".state")
TARGET_FILE = os.path.join(STATE_DIR, "rotation_targets.yaml")
HOLDINGS_FILE = os.path.join(STATE_DIR, "rotation_holdings.json")
T0_GRID_FILE  = os.path.join(STATE_DIR, "grid_targets.yaml")
SNIPER_FILE   = os.path.join(STATE_DIR, "sniper_holdings.json")

# =============================================================================
# 2. 工具函数
# =============================================================================
def send_n8n_alert(title, message):
    if not N8N_WEBHOOK_URL: return
    try:
        payload = {"title": title, "message": message, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
        # 采用 POST 提交 JSON
        requests.post(N8N_WEBHOOK_URL, json=payload, timeout=10)
    except Exception:
        pass

def acquire_lock_with_ttl(lock_path, max_age_seconds=600):
    """带 TTL 的自愈型进程锁。默认 600 秒 (10 分钟) 超时。"""
    if os.path.exists(lock_path):
        file_age = time.time() - os.path.getmtime(lock_path)
        if file_age > max_age_seconds:
            print(f"⚠️ [系统自愈] 发现残留孤儿锁 ({file_age:.1f}秒前)。强行粉碎并接管控制权...")
            try:
                os.remove(lock_path)
            except OSError: pass
        else:
            print(f"🚫 [并发拦截] 进程锁生效中 (存活 {file_age:.1f}秒)，本次调度自动静默退让。")
            return False
    try:
        with open(lock_path, 'w') as f: f.write(str(os.getpid()))
        return True
    except: return False

def release_lock(lock_path):
    if os.path.exists(lock_path):
        try: os.remove(lock_path)
        except OSError: pass

def safe_execute_and_lock(xt_trader, acc, code, order_type, qty, price, strategy_name, order_remark, save_func):
    """原子化下单与落盘封装"""
    try:
        res = xt_trader.order_stock(acc, code, order_type, qty, xtconstant.FIX_PRICE, price, strategy_name, order_remark)
        if res > 0:
            save_func()
        return res
    except Exception as e:
        print(f"❌ Execution Critical Error: {e}")
        return -1

def _load_holdings():
    if os.path.exists(HOLDINGS_FILE):
        try:
            with open(HOLDINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except: return {}
    return {}

def _save_holdings(holdings):
    with open(HOLDINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(holdings, f, ensure_ascii=False, indent=4)

def _load_excluded_codes() -> set:
    """加载 T0 网格和 Sniper 管辖的标的码，物理扫描时需排除它们。"""
    excluded = set(T0_BLACKLIST)
    # T0 网格目标
    if os.path.exists(T0_GRID_FILE):
        try:
            with open(T0_GRID_FILE, 'r', encoding='utf-8') as f:
                t0 = yaml.safe_load(f) or {}
                excluded.update(t0.keys())
        except: pass
    # Sniper 持仓
    if os.path.exists(SNIPER_FILE):
        try:
            with open(SNIPER_FILE, 'r', encoding='utf-8') as f:
                excluded.update((json.load(f) or {}).keys())
        except: pass
    # T1 网格持仓 🛡️ 防止 Rotation 物理扫描时误收 T1 底仓并卖出
    t1_ledger = os.path.join(STATE_DIR, "t1_grid_ledger.yaml")
    if os.path.exists(t1_ledger):
        try:
            with open(t1_ledger, 'r', encoding='utf-8') as f:
                t1 = yaml.safe_load(f) or {}
                excluded.update(t1.keys())
            print(f"🛡️ [Firewall/T1] 已排除 T1 网格标的: {sorted(t1.keys())}")
        except: pass
    return excluded

def _sync_physical_holdings(xt_trader, acc, holdings: dict) -> dict:
    """物理持仓扫描补录：
    扫描券商真实持仓，将不在 rotation_holdings.json 中但非 T0/Sniper 管辖的标的
    自动补录进来，防止因上次执行失败导致的'漏记持仓'被忽略或错误被 T0 认作孤儿。

    规则：
      - 排除 T0 网格、Sniper、T0_BLACKLIST 管辖的标的
      - 对于剩余有持仓的标的，若不在 holdings 中 → 补录（用均价作为买入价记录）
      - 不删除 holdings 中实盘已无仓的记录（由后续卖出逻辑处理）
    返回更新后的 holdings（已落盘）。
    """
    excluded = _load_excluded_codes()
    try:
        real_positions = xt_trader.query_stock_positions(acc) or []
    except Exception as e:
        print(f"⚠️ [物理扫描] 查询持仓失败: {e}，跳过补录")
        return holdings

    added = []
    for pos in real_positions:
        code = pos.stock_code
        if pos.volume <= 0:
            continue
        if code in excluded:
            continue
        if code in holdings:
            continue  # 已记录，无需补录
        # 补录：用均价作为参考买入价
        avg_cost = float(pos.open_price) if pos.open_price else 0.0
        holdings[code] = {
            "qty":       int(pos.volume),
            "buy_price": avg_cost,
            "date":      datetime.now().strftime("%Y-%m-%d"),
            "_synced":   True  # 标记为物理扫描补录，非执行器亲手买入
        }
        added.append(f"{code}({int(pos.volume)}股@{avg_cost:.3f})")

    if added:
        _save_holdings(holdings)
        print(f"🔄 [物理持仓补录] 发现 {len(added)} 个未记录的轮动持仓，已同步进账本:")
        for item in added:
            print(f"   ✅ {item}")
    else:
        print("✅ [物理持仓扫描] 账本与实盘一致，无需补录。")

    return holdings


def _get_firewall_locked_shares(code: str) -> int:
    """读取其他策略（T1 Grid / Sniper）账本中该标的被锁定的股数。

    T0 Grid 标的与 Rotation 通过排除名单隔离，不会共享标的，
    其 grid_targets.yaml 按"格子方向"记账无直接股数，故 T0 不参与减法。

    Returns:
        locked_shares (int): 其他策略合计锁定的股数，0 表示无锁定。
    """
    locked = 0

    # ── T1 网格：available_shares 代表 T1 底仓实际锁定股数 ──
    t1_ledger_path = os.path.join(STATE_DIR, "t1_grid_ledger.yaml")
    if os.path.exists(t1_ledger_path):
        try:
            with open(t1_ledger_path, 'r', encoding='utf-8') as f:
                t1 = yaml.safe_load(f) or {}
            if code in t1:
                t1_shares = int(t1[code].get('available_shares', 0))
                locked += t1_shares
                if t1_shares > 0:
                    print(f"🛡️ [防火墙/T1] {code} T1 账本锁定 {t1_shares} 股")
        except Exception as e:
            print(f"⚠️ [防火墙/T1] 读取 T1 账本失败: {e}")

    # ── Sniper：qty 代表 Sniper 持有的物理股数 ──
    if os.path.exists(SNIPER_FILE):
        try:
            with open(SNIPER_FILE, 'r', encoding='utf-8') as f:
                sniper = json.load(f) or {}
            if code in sniper:
                sniper_shares = int(sniper[code].get('qty', 0))
                locked += sniper_shares
                if sniper_shares > 0:
                    print(f"🛡️ [防火墙/Sniper] {code} Sniper 账本锁定 {sniper_shares} 股")
        except Exception as e:
            print(f"⚠️ [防火墙/Sniper] 读取 Sniper 账本失败: {e}")

    return locked


def _reconcile_rotation_vol(
    xt_trader,
    acc,
    code: str,
    holdings: dict
) -> int:
    """动态子账户对账探针：精准确定 Rotation 真实可处置物理股数。

    算法：
        total_qmt_vol    = QMT 实盘该标的 can_use_volume（物理可用股数）
        locked_by_others = T1 + Sniper 账本锁定股数
        true_rotation_vol = total_qmt_vol - locked_by_others

    若与账本不一致，强制以物理为准覆写 holdings 内存及 YAML 落盘。

    Returns:
        true_rotation_vol (int): 修正后 Rotation 可处置的股数。
                                  0 表示无可用仓位，调用方应跳过卖出。
    """
    # ── Step 1：物理探针 ──
    total_qmt_vol = 0
    try:
        pos_list = xt_trader.query_stock_positions(acc) or []
        for pos in pos_list:
            if pos.stock_code == code:
                total_qmt_vol = int(pos.can_use_volume)
                break
        print(f"🔍 [动态确权] {code} QMT 实盘可用量: {total_qmt_vol} 股")
    except Exception as e:
        print(f"⚠️ [动态确权] {code} 查询 QMT 持仓异常: {e}，以账本值为准")
        return int(holdings.get(code, {}).get('qty', 0))

    # ── Step 2：防火墙减法 ──
    locked_by_others = _get_firewall_locked_shares(code)
    true_rotation_vol = total_qmt_vol - locked_by_others

    # ── Step 3：边界拦截 ──
    if true_rotation_vol < 0:
        print(
            f"🚨 [CRITICAL/确权异常] {code} 防火墙锁定量({locked_by_others}) "
            f"超过 QMT 物理可用量({total_qmt_vol})！"
            f"真实可处置股数异常为负，已强制归零，禁止卖出。"
        )
        return 0

    # ── Step 4：账本强制覆写（消灭懒惰）──
    ledger_qty = int(holdings.get(code, {}).get('qty', 0))
    if true_rotation_vol != ledger_qty:
        print(
            f"⚠️ [Rotation确权] 发现账本偏差 {code}: "
            f"账本={ledger_qty} 股, 物理可处置={true_rotation_vol} 股 "
            f"(QMT={total_qmt_vol} - 防火墙={locked_by_others})，"
            f"已强制修正为: {true_rotation_vol}"
        )
        if code in holdings:
            holdings[code]['qty'] = true_rotation_vol
            _save_holdings(holdings)  # 落盘持久化
    else:
        print(f"✅ [动态确权] {code} 账本与实盘一致 ({true_rotation_vol} 股)，无需修正")

    return true_rotation_vol


def get_session():
    if not QMT_PATH or not ACCOUNT_ID:
        print("❌ 环境变量缺失 QMT_PATH 或 ACCOUNT_ID")
        return None, None
        
    session_id = int(time.time())
    xt_trader = XtQuantTrader(QMT_PATH, session_id)
    xt_trader.start()
    connect_result = xt_trader.connect()
    if connect_result != 0:
        print("❌ 无法连接 QMT 终端，请检查 QMT 是否已启动")
        return None, None
    from xtquant.xttype import StockAccount
    acc = StockAccount(ACCOUNT_ID)
    return xt_trader, acc

# =============================================================================
# 3. 执行逻辑
# =============================================================================
def run_rotation_executor():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚙️ ETF 执行器：开始无情调仓...")
    
    # A. 读取目标
    if not os.path.exists(TARGET_FILE):
        print(f"❌ 找不到目标文件: {TARGET_FILE}")
        return
        
    with open(TARGET_FILE, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
        targets = config.get('targets', [])
    
    if not targets:
        print("📭 目标池为空，停止执行")
        return

    print(f"🎯 本轮轮动目标: {targets}")

    # B. 连接 QMT
    xt_trader, acc = get_session()
    if not xt_trader: return
    
    # C. 物理持仓扫描补录 ─────────────────────────────────────────────────
    # 先读账本，再做物理扫描，将漏记的轮动持仓补录进来。
    # 这样即使上次执行失败，实际持有的标的也能被正确识别并卖出/保留。
    holdings = _load_holdings()
    holdings = _sync_physical_holdings(xt_trader, acc, holdings)

    print(f"📋 补录后账本: {list(holdings.keys())}")

    # D. 卖出逻辑：不在最新目标名单中的持仓全部清仓 ─────────────────────
    codes_to_sell = [c for c in list(holdings.keys()) if c not in targets]
    codes_to_keep = [c for c in list(holdings.keys()) if c in targets]

    print(f"📤 需要卖出: {codes_to_sell}")
    print(f"📦 保留不动: {codes_to_keep}")

    for code in codes_to_sell:
        # ══════════════════════════════════════════════════════════════════
        # 【动态子账户对账】极限清剿前的精准确权
        # 严禁信任旧账本——必须通过物理探针 + 防火墙减法确定真实可处置量
        # ══════════════════════════════════════════════════════════════════
        true_rotation_vol = _reconcile_rotation_vol(xt_trader, acc, code, holdings)

        if true_rotation_vol <= 0:
            ledger_qty = holdings.get(code, {}).get('qty', 0)
            if ledger_qty <= 0:
                print(f"⚠️ {code} 账本数量为 0，跳过卖出")
            else:
                print(
                    f"⚠️ {code} 经物理确权后可处置量为 0（账本={ledger_qty}），"
                    f"疑似 Ghost 标的或全被防火墙锁定，跳过卖出"
                )
            del holdings[code]
            _save_holdings(holdings)
            continue

        tick_data = xtdata.get_full_tick([code])
        tick = tick_data.get(code, {})
        bid1 = tick.get('bidPrice', [0])[0]
        sell_price = round(bid1 - 0.002, 3) if bid1 > 0 else 0

        if sell_price > 0:
            _code = code  # 闭包捕获（防晚绑定）
            def sell_save(_c=_code):
                del holdings[_c]
                _save_holdings(holdings)

            seq = safe_execute_and_lock(
                xt_trader, acc, code, xtconstant.STOCK_SELL,
                true_rotation_vol, sell_price, "Rotation", "Exit", sell_save
            )
            print(
                f"📉 [轮动调仓·极限清剿] 卖出 {code} | "
                f"价格: {sell_price} | 物理确权数量: {true_rotation_vol} | 单号: {seq}"
            )
            record_action(
                strategy="Rotation", action="平仓", target=code,
                price=sell_price, reason="调仓卖出",
                extra={"qty": true_rotation_vol, "seq": seq, "physical_sweep": True}
            )
        else:
            print(f"⚠️ {code} 盘口异常，跳过卖出")

    # ─── 等待撮合结算 ────────────────────────────────────────────────────
    print("⏳ [物理阻断] 等待 3 秒，让交易所完成撮合与资金结算...")
    time.sleep(3)

    # E. 买入逻辑 ─────────────────────────────────────────────────────────
    # 已持有的目标（codes_to_keep）原封不动，只对新目标下单。
    targets_to_buy = [t for t in targets if t not in codes_to_keep]

    if not targets_to_buy:
        print("✅ 所有目标均已持有（保留），本轮无需新建仓位。")
        return

    # ── 资金分配：全部可用轮动资金 ÷ 需要买入的新标的数量 ────────────────
    # 例：A+B → B+C，卖A freed 50k，B保留 50k，新买C分到全部freed资金=50k
    # 例：A+B → C，卖A+B freed 100k，C分到全部100k
    # 不把资金分给已保留的标的（它们的仓位不变）
    buy_count = len(targets_to_buy)
    each_fund = ROTATION_TOTAL_CAPITAL / len(targets)  # 每个目标槽位的标准配额

    # 如果保留了部分旧仓，把「被保留的配额」转移给新买标的
    kept_count   = len(codes_to_keep)
    freed_budget = ROTATION_TOTAL_CAPITAL - (kept_count * each_fund)  # 卖出释放的资金额度
    per_new_fund = freed_budget / buy_count if buy_count > 0 else 0

    buy_dict = {code: per_new_fund for code in targets_to_buy}

    # 获取可用现金 (仅用于辅助决策)
    asset = xt_trader.query_stock_asset(acc)
    real_cash = asset.cash if asset else 0

    print(f"💰 账户实时可用现金: {real_cash:.2f} | 需买入名单: {targets_to_buy}")
    for code, allocated_cash in buy_dict.items():
        print(f"   {code}  配额: ¥{allocated_cash:,.0f}")

    for code, allocated_cash in buy_dict.items():

        # 资金防火墙：如果可用现金不足本标的分配额，跳过保护其他策略
        if real_cash < allocated_cash:
            reason = f"买入 {code} 需要 ¥{allocated_cash:,.0f}，账户仅剩 ¥{real_cash:,.0f}"
            print(f"❌ [资金熔断] {reason}，跳过。")
            record_action(
                strategy="ETF_Rotation",
                action="熔断",
                target=code,
                reason=reason,
                extra={"allocated": allocated_cash, "real_cash": real_cash}
            )
            continue

        tick = xtdata.get_full_tick([code])
        if code in tick:
            ask1 = tick[code].get('askPrice', [0])[0]
            if ask1 > 0:
                buy_qty = math.floor((allocated_cash / ask1) / 100) * 100
                if buy_qty > 0:
                    buy_price = round(ask1 + 0.002, 3)
                    # 🛡️ Pattern 2 & V6: 原子化
                    def buy_save():
                        holdings[code] = {
                            "qty": buy_qty,
                            "buy_price": buy_price,
                            "date": datetime.now().strftime("%Y-%m-%d")
                        }
                        _save_holdings(holdings)

                    seq = safe_execute_and_lock(
                        xt_trader, acc, code, xtconstant.STOCK_BUY,
                        buy_qty, buy_price, "Rotation", "Entry", buy_save
                    )
                    print(f"🎯 [轮动调仓] 买入 {code} | 价格: {buy_price} | 数量: {buy_qty} | 单号: {seq}")
                    record_action(strategy="Rotation", action="买入", target=code, price=buy_price, reason="调仓买入", extra={"qty": buy_qty, "seq": seq})
                    real_cash -= (buy_qty * ask1)
                else:
                    print(f"⚠️ {code} 配额 ¥{allocated_cash:,.0f} 不足以购买一手，无法建仓")
            else:
                print(f"⚠️ {code} 盘口异常，跳开买入")
        else:
            print(f"⚠️ {code} 无法获取实时行情，跳过")


    # E. 最终汇报
    msg = f"🏁 [ETF 轮动调仓完成]\n更新时间: {config.get('update_time')}\n本周持仓: {targets}\n保留底仓: {T0_BLACKLIST}"
    send_n8n_alert("📊 ETF 轮动汇报", msg)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🏁 调仓流程结束")

def run_rotation():
    LOCK_FILE = os.path.join(STATE_DIR, "rotation.lock")
    if not acquire_lock_with_ttl(LOCK_FILE): return

    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 Rotation 引擎启动...")
        run_rotation_executor()
    finally:
        release_lock(LOCK_FILE)

if __name__ == "__main__":
    run_rotation()
