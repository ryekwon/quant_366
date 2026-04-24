# ==========================================
# 部署: Quant-PC
# 命名: macro_risk_monitor.py  (代号: The Sentinel V2 / 物理熔断守卫)
# 职责: 周一至周四盘中巡逻——仅扫描当前持仓，触发阈值则强制切换至国债
#
# [2026-04-22] 重大重铸(V2)：废弃所有 Oracle 预言机依赖，回归物理事实防守。
#
# 守卫双防线（The Sentinel / 绝对机械化）：
#   ① 移动止盈 (Trailing Stop)：
#      现价 > HWM → 更新 HWM（追踪保护）
#      现价 < HWM * (1 - 0.08) → 回撤超过 8%，立即熔断
#   ② 均线防守 (Trend Stop)：
#      现价 < MA20 → 记录首次跌破时间
#      跌破后连续 2 小时未收回 → 立即熔断
#
# 熔断执行：立即清仓 → 切入 511260.SH（国债）→ 更新 JSON 账本 → N8N 推送
#
# 禁止进攻：周一至周四严禁买入任何非国债标的
#
# 调度（autopilot_master.py 注册）：
#   周一至周四 09:45 / 11:00 / 14:00 各触发一次
#   周五：运行 macro_rotation_executor.py（进攻窗口）
# ==========================================
import os
import sys
import json
import math
import time
import requests
import numpy as np
import threading
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from quant_logger import record_action

load_dotenv()

# 🛡️ 强制 UTF-8 输出，防止 Windows GBK 终端吞 Emoji
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── 路径常量 ──────────────────────────────────────────────────────────────────
_DIR             = Path(__file__).parent.resolve()
_STATE_DIR       = _DIR / ".state"
SLOTS_STATE_FILE = _STATE_DIR / "macro_slots.json"   # 执行器写入，守卫读取并更新

# ── 常量 ──────────────────────────────────────────────────────────────────────
ACCOUNT_ID  = os.getenv("ACCOUNT_ID")
QMT_PATH    = os.getenv("QMT_PATH")
N8N_WEBHOOK = os.getenv("N8N_WEBHOOK_URL")

SAFE_ASSET = '511260.SH'   # 国债防守锁

# 宏观资产全量池（与 macro_rotation_executor.py 保持严格同步，10只）
MACRO_POOL = [
    "510300.SH",   # 沪深300 ETF
    "513500.SH",   # 标普500 ETF
    "512890.SH",   # 红利防守 ETF
    "518880.SH",   # 黄金 ETF
    "511260.SH",   # 国债 ETF（SAFE_ASSET）
    "513100.SH",   # 纳斯达克科技 ETF
    "513050.SH",   # 中概互联 ETF
    "159915.SZ",   # 创业板 ETF
    "588000.SH",   # 科创50 ETF
    "563300.SH",   # 港股科技 ETF
]

# 熔断参数（物理事实判定，无预言机依赖）
TRAIL_STOP_PCT   = 0.08   # 移动止盈：从高水位回撤超过 8% 触发熔断
MA20_LOOKBACK    = 22     # MA20 数据取样根数（取 22 根，确保至少 20 根有效）
MA20_WINDOW      = 20     # MA20 窗口（取最近 20 根均值）
MA20_BREAK_HOURS = 2      # 跌破 MA20 后连续 2 小时未修复则触发熔断


# ── ETF 中文名称缓存 ──────────────────────────────────────────────────────────
_name_cache: dict = {}

def _get_name(code: str) -> str:
    """via get_instrument_detail，带本地内存缓存。"""
    if code in _name_cache:
        return _name_cache[code]
    try:
        from xtquant import xtdata as _xd
        detail = _xd.get_instrument_detail(code)
        name = (detail or {}).get('InstrumentName', '') if isinstance(detail, dict) else ''
    except Exception:
        name = ''
    _name_cache[code] = name
    return name


# ── 行情工具函数 ──────────────────────────────────────────────────────────────

def _get_current_price(code: str) -> float:
    """
    获取标的当前实时价格（get_full_tick lastPrice）。
    回退顺序：lastPrice → lastClose → 0.0
    """
    try:
        from xtquant import xtdata
        tick = xtdata.get_full_tick([code]).get(code, {})
        if not tick:
            return 0.0
        price = float(tick.get('lastPrice', 0.0) or 0.0)
        if price <= 0:
            price = float(tick.get('lastClose', 0.0) or 0.0)
        return price
    except Exception as e:
        print(f"  WARN _get_current_price({code}) 异常: {e}")
        return 0.0


def _get_ma20(code: str) -> float:
    """
    获取标的 20 日移动均线（纯读本地 QMT 缓存，0 网络请求）。
    count=22 保证至少 20 根有效 close 可用。
    失败返回 0.0。
    """
    try:
        from xtquant import xtdata
        raw = xtdata.get_market_data_ex(
            field_list=['close'],
            stock_list=[code],
            period='1d',
            count=MA20_LOOKBACK,
        )
        df = raw.get(code)
        if df is None or df.empty:
            return 0.0
        closes = df['close'].dropna().tolist()
        if len(closes) < MA20_WINDOW:
            print(f"  WARN {code} MA20 数据不足 {MA20_WINDOW} 根（实际={len(closes)}）")
            return 0.0
        ma20 = sum(closes[-MA20_WINDOW:]) / MA20_WINDOW
        return round(ma20, 6)
    except Exception as e:
        print(f"  WARN _get_ma20({code}) 异常: {e}")
        return 0.0


# ── QMT 会话 ───────────────────────────────────────────────────────────────────
def _get_qmt_session():
    if not ACCOUNT_ID or not QMT_PATH:
        print("ERROR env ACCOUNT_ID or QMT_PATH missing")
        return None, None
    from xtquant.xttrader import XtQuantTrader
    from xtquant.xttype import StockAccount
    xt = XtQuantTrader(QMT_PATH, int(time.time()))
    xt.start()
    time.sleep(1)
    if xt.connect() != 0:
        print("ERROR cannot connect QMT"); return None, None
    return xt, StockAccount(ACCOUNT_ID)


def _send_n8n(title: str, message: str):
    if not N8N_WEBHOOK:
        return
    try:
        requests.post(N8N_WEBHOOK, json={
            "title": title, "message": message,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }, timeout=10)
    except Exception:
        pass


def _sell_slot(xt_trader, acc, slot_name: str, code: str, capital: float):
    """市价全平仓指定标的，返回 seq"""
    from xtquant import xtdata, xtconstant
    try:
        pos_list = xt_trader.query_stock_positions(acc) or []
        real_qty = next((int(p.can_use_volume) for p in pos_list if p.stock_code == code), 0)
    except Exception as e:
        print(f"  WARN {slot_name} [{code}] 查询持仓失败: {e}")
        return -1
    if real_qty <= 0:
        print(f"  WARN {slot_name} [{code}] 可卖数量=0，跳过")
        return 0
    tick = xtdata.get_full_tick([code]).get(code, {})
    bid1 = tick.get("bidPrice", [0])[0] if tick else 0.0
    if bid1 <= 0:
        prev = tick.get("lastClose", tick.get("lastPrice", 0.0)) if tick else 0.0
        bid1 = prev if prev > 0 else 1.0
    sell_price = round(bid1 - 0.002, 3)
    seq = xt_trader.order_stock(
        acc, code, xtconstant.STOCK_SELL, real_qty,
        xtconstant.FIX_PRICE, sell_price, "MacroRotation", f"{slot_name}_Sentinel_Exit"
    )
    print(f"  ✂️ SELL {slot_name} [{code}] {real_qty}sh @ {sell_price} seq={seq}")
    record_action(
        strategy="MacroRotation", action="sentinel_exit",
        target=code, price=sell_price,
        reason="守卫熔断触发",
        extra={"slot": slot_name, "qty": real_qty, "seq": seq}
    )
    return seq


def _buy_safe_asset(xt_trader, acc, slot_name: str, capital: float):
    """将释放的资金切入国债 ETF"""
    from xtquant import xtdata, xtconstant
    tick  = xtdata.get_full_tick([SAFE_ASSET]).get(SAFE_ASSET, {})
    ask1  = tick.get("askPrice", [0])[0] if tick else 0.0
    if ask1 <= 0:
        ask1 = tick.get("lastPrice", 1.0) if tick else 1.0
    buy_qty   = math.floor(capital / ask1 / 100) * 100
    buy_price = round(ask1 + 0.002, 3)
    if buy_qty <= 0:
        print(f"  WARN {slot_name} 国债资金不足一手，跳过")
        return -1
    seq = xt_trader.order_stock(
        acc, SAFE_ASSET, xtconstant.STOCK_BUY, buy_qty,
        xtconstant.FIX_PRICE, buy_price, "MacroRotation", f"{slot_name}_Sentinel_Safe"
    )
    print(f"  ✅ BUY {slot_name} [{SAFE_ASSET}] {buy_qty}sh @ {buy_price} (safe) seq={seq}")
    record_action(
        strategy="MacroRotation", action="sentinel_buy_safe",
        target=SAFE_ASSET, price=buy_price,
        reason=f"{slot_name} 熔断后切入国债",
        extra={"slot": slot_name, "qty": buy_qty, "seq": seq}
    )
    return seq


# ── QMT 连接预检 ──────────────────────────────────────────────────────────────
def _check_qmt_alive(timeout_sec: float = 5.0) -> bool:
    result_box = [False]

    def _probe():
        try:
            from xtquant import xtdata
            xtdata.get_market_data_ex(
                field_list=['close'], stock_list=['510300.SH'],
                period='1d', count=1
            )
            result_box[0] = True
        except Exception:
            pass

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    return result_box[0]


# ── 当前持仓状态加载 / 保存 ────────────────────────────────────────────────────
def _load_slots() -> dict | None:
    """读取 .state/macro_slots.json（由执行器每周五落盘）。"""
    if not SLOTS_STATE_FILE.exists():
        print(f"⚠️  槽位状态文件不存在：{SLOTS_STATE_FILE}")
        print("   请先在周五运行 macro_rotation_executor.py 进行槽位分配。")
        return None
    try:
        return json.loads(SLOTS_STATE_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        print(f"🔥 槽位状态文件损坏：{e}")
        return None


def _save_slots(slots: dict):
    """原子写入 macro_slots.json（tmp → replace）。"""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _tmp = SLOTS_STATE_FILE.with_suffix(".tmp")
    _tmp.write_text(json.dumps(slots, ensure_ascii=False, indent=2), encoding="utf-8")
    _tmp.replace(SLOTS_STATE_FILE)
    print(f"  💾 macro_slots.json 已原子更新")


# ── 熔断判定：双防线 ───────────────────────────────────────────────────────────

def _check_trailing_stop(slot_name: str, code: str, current_price: float,
                          slots: dict) -> tuple[bool, str]:
    """
    移动止盈防线：
      - 若现价 > HWM → 更新 HWM（追踪保护）
      - 若现价 < HWM * (1 - TRAIL_STOP_PCT) → 触发熔断

    直接修改 slots 中的 hwm 字段（调用方负责落盘）。
    返回 (是否触发熔断, 原因字符串)。
    """
    hwm_key = f"{slot_name.lower()}_hwm"
    hwm     = slots.get(hwm_key)

    if hwm is None or hwm <= 0:
        # 执行器未写入 HWM（老账本兼容）：用当前价初始化
        print(f"  ⚠️  {slot_name} [{code}] HWM 未初始化，以现价 {current_price:.4f} 初始化")
        slots[hwm_key] = current_price
        return False, ""

    hwm = float(hwm)

    # 追踪更新 HWM
    if current_price > hwm:
        print(f"  📈 {slot_name} [{code}] 创新高 {current_price:.4f} > HWM {hwm:.4f}，更新 HWM")
        slots[hwm_key] = current_price
        hwm = current_price

    # 熔断判定
    stop_price = hwm * (1.0 - TRAIL_STOP_PCT)
    if current_price < stop_price:
        drawdown = (hwm - current_price) / hwm * 100
        reason = (
            f"移动止盈触发：现价 {current_price:.4f} < "
            f"HWM {hwm:.4f} × {1-TRAIL_STOP_PCT:.2f}={stop_price:.4f} "
            f"（回撤 {drawdown:.2f}%）"
        )
        return True, reason

    print(
        f"  ✅ {slot_name} [{code}] 移动止盈正常 | "
        f"现价={current_price:.4f}  HWM={hwm:.4f}  "
        f"止盈线={stop_price:.4f}  距离={((current_price-stop_price)/stop_price*100):+.2f}%"
    )
    return False, ""


def _check_ma20_stop(slot_name: str, code: str, current_price: float,
                     slots: dict, now: datetime) -> tuple[bool, str]:
    """
    均线防守防线：
      - 若现价 < MA20 → 记录或检查首次跌破时间
      - 若跌破后超过 MA20_BREAK_HOURS 小时未修复 → 触发熔断
      - 若现价 >= MA20 → 清除 break_at 记录

    直接修改 slots 中的 break_at 字段（调用方负责落盘）。
    返回 (是否触发熔断, 原因字符串)。
    """
    ma20 = _get_ma20(code)
    if ma20 <= 0:
        print(f"  ⚠️  {slot_name} [{code}] MA20 获取失败（数据不足），跳过均线防守")
        return False, ""

    break_key = f"{slot_name.lower()}_ma20_break_at"

    if current_price < ma20:
        gap_pct = (ma20 - current_price) / ma20 * 100

        # 记录首次跌破时间（首次才记，后续复用）
        if slots.get(break_key) is None:
            slots[break_key] = now.strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"  ⚠️  {slot_name} [{code}] 首次跌破 MA20 | "
                f"现价={current_price:.4f} MA20={ma20:.4f} 跌幅={gap_pct:.2f}% | "
                f"计时开始（{MA20_BREAK_HOURS}h 后熔断）"
            )
            return False, ""

        # 检查是否已连续跌破达 MA20_BREAK_HOURS
        break_at_str = slots[break_key]
        try:
            break_at = datetime.strptime(break_at_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            # 时间格式异常，重置计时
            slots[break_key] = now.strftime("%Y-%m-%d %H:%M:%S")
            return False, ""

        elapsed_hours = (now - break_at).total_seconds() / 3600

        if elapsed_hours >= MA20_BREAK_HOURS:
            reason = (
                f"均线防守触发：现价 {current_price:.4f} < MA20 {ma20:.4f} "
                f"（跌幅 {gap_pct:.2f}%），已连续跌破 {elapsed_hours:.1f} 小时 "
                f">= {MA20_BREAK_HOURS}h 阈值"
            )
            return True, reason
        else:
            remaining = MA20_BREAK_HOURS - elapsed_hours
            print(
                f"  ⏳ {slot_name} [{code}] 跌破 MA20 计时中 | "
                f"现价={current_price:.4f} MA20={ma20:.4f} | "
                f"已跌破 {elapsed_hours:.1f}h，还需 {remaining:.1f}h 熔断"
            )
            return False, ""
    else:
        # 价格已修复（站上 MA20）：清除计时
        if slots.get(break_key) is not None:
            print(
                f"  ✅ {slot_name} [{code}] 重回 MA20 以上，清除计时 | "
                f"现价={current_price:.4f}  MA20={ma20:.4f}"
            )
            slots[break_key] = None
        else:
            print(
                f"  ✅ {slot_name} [{code}] MA20 正常 | "
                f"现价={current_price:.4f}  MA20={ma20:.4f}  "
                f"距MA20 {((current_price-ma20)/ma20*100):+.2f}%"
            )
        return False, ""


# ── 主函数 ───────────────────────────────────────────────────────────────────
def run_sentinel():
    now          = datetime.now()
    ts           = now.strftime('%Y-%m-%d %H:%M')
    weekday      = now.isoweekday()   # 1=周一 ... 5=周五 ... 7=周日
    weekday_name = ['一', '二', '三', '四', '五', '六', '日'][weekday - 1]

    print(f"[{ts}] 🛡️  The Sentinel V2 启动 | 周{weekday_name} | 双防线物理巡逻模式")
    print(f"   防线1：移动止盈（回撤 >{TRAIL_STOP_PCT*100:.0f}% 熔断）")
    print(f"   防线2：MA20 均线防守（跌破 >{MA20_BREAK_HOURS}h 熔断）")

    # ── 物理验日：周五及周末跳过
    if weekday >= 5:
        print(f"   【守卫休息】周{weekday_name} 属于进攻窗口或休市，守卫不介入。")
        print(f"   若为周五，请运行 macro_rotation_executor.py 进行槽位分配。")
        return

    # ── 读取当前持仓槽位
    slots = _load_slots()
    if slots is None:
        return

    slot_a = slots.get('slot_a', SAFE_ASSET)
    slot_b = slots.get('slot_b', SAFE_ASSET)
    print(f"   📂 当前槽位 | Slot_A: [{slot_a}]  Slot_B: [{slot_b}]")
    print(f"   （槽位更新于 {slots.get('updated_at', 'N/A')}）")
    print(f"   HWM: slot_a={slots.get('slot_a_hwm', 'N/A')}  slot_b={slots.get('slot_b_hwm', 'N/A')}")

    hold_codes = list(dict.fromkeys([c for c in [slot_a, slot_b] if c]))

    # ── QMT 连接预检
    print("📡 预检 miniQMT 连接状态...")
    if not _check_qmt_alive(timeout_sec=5.0):
        print("🔥 物理熔断：miniQMT 离线，守卫无法巡逻！请检查 QMT 进程。")
        return
    print(f"✅ miniQMT 在线，对 {len(hold_codes)} 个持仓槽位执行双防线判定...")

    # ── 延迟建立 QMT 会话（仅在确实需要下单时才创建）
    xt_trader    = None
    acc          = None
    slots_updated = False

    # ── 对每个槽位执行双防线判定
    print(f"\n⚡ 物理熔断守卫判断（双防线）:")
    for slot_name, code in [('Slot_A', slot_a), ('Slot_B', slot_b)]:
        print(f"\n  --- {slot_name} [{code}] ---")

        # 国债本身免检
        if code == SAFE_ASSET:
            print(f"  ✅ {slot_name} [{code}] 本身为国债，免检跳过")
            continue

        # 获取当前实时价格
        current_price = _get_current_price(code)
        if current_price <= 0:
            print(f"  ⚠️  {slot_name} [{code}] 无法获取实时价格，跳过本次判定")
            continue
        print(f"  💹 {slot_name} [{code}] 现价={current_price:.4f}  ETF名称={_get_name(code)}")

        triggered = False
        reason    = ""

        # ─── 防线1：移动止盈 (Trailing Stop) ───
        print(f"  [防线1 移动止盈]")
        triggered, reason = _check_trailing_stop(slot_name, code, current_price, slots)

        # ─── 防线2：均线防守（仅防线1未触发时检查）───
        if not triggered:
            print(f"  [防线2 MA20 防守]")
            triggered, reason = _check_ma20_stop(slot_name, code, current_price, slots, now)

        # ─── 熔断执行 ───
        if triggered:
            print(f"\n  🚨 {slot_name} [{code}] 熔断触发！")
            print(f"     原因：{reason}")
            print(f"     动作：立即市价平仓 {code}，资金转入 {SAFE_ASSET}（国债）")
            print(f"     ⛔  严禁进攻：周{weekday_name}禁止买入任何非国债标的！")

            # 连接 QMT（懒加载）
            if xt_trader is None:
                xt_trader, acc = _get_qmt_session()
                if xt_trader is None:
                    print("  🔥 无法建立 QMT 会话，熔断平仓失败！")
                    _send_n8n(
                        f"🚨 Sentinel熔断 QMT失联",
                        f"{slot_name} [{code}] 需要平仓但 QMT 无法连接！\n原因: {reason}"
                    )
                    continue

            slot_capital = slots.get(f"{slot_name.lower()}_capital", 50000)

            # 卖出
            sell_seq = _sell_slot(xt_trader, acc, slot_name, code, slot_capital)
            if sell_seq is not None and sell_seq > 0:
                print("      ⏳ 等待2s 结算...")
                time.sleep(2)

                # 买入国债
                buy_seq = _buy_safe_asset(xt_trader, acc, slot_name, slot_capital)

                # 更新槽位状态：清除旧 HWM 和 MA20 计时
                slot_key_lower = slot_name.lower().replace('slot_', 'slot_')
                slots[f"{slot_name.lower()}"]            = SAFE_ASSET
                slots[f"{slot_name.lower()}_hwm"]        = None
                slots[f"{slot_name.lower()}_ma20_break_at"] = None
                slots_updated = True

                _send_n8n(
                    f"🚨 Sentinel 熔断已执行",
                    f"{slot_name} [{code}] 已平仓（sell_seq={sell_seq}），"
                    f"并切入国债 {SAFE_ASSET}（buy_seq={buy_seq}）\n"
                    f"熔断原因: {reason}"
                )
            else:
                print(f"  ⚠️  {slot_name} [{code}] 卖出委托未成功，账本暂不更新")
        else:
            # 无熔断：HWM 更新已在 _check_trailing_stop 中完成（slots 已修改）
            # MA20 break_at 更新已在 _check_ma20_stop 中完成（slots 已修改）
            slots_updated = True  # HWM/break_at 状态可能已变化，均需落盘

    # ── 最终落盘（含 HWM 追踪更新、MA20 计时更新、熔断状态）
    if slots_updated:
        slots["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        _save_slots(slots)

    # ── 释放 QMT 会话
    if xt_trader is not None:
        try:
            xt_trader.stop()
        except Exception:
            pass

    print(f"\n🛡️  守卫巡逻完毕 [{ts}]")


if __name__ == '__main__':
    run_sentinel()
