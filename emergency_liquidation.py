# -*- coding: utf-8 -*-
# ==============================================================================
# 🚨 emergency_liquidation.py — 紧急物理清仓脚本
# 用途：立即清空 ETF_OU_Grid (159518.SZ) 和 Hawkes (513300.SH) 所有持仓
# 操作：query_stock_positions 物理查仓 → 全量 FIX_PRICE bid1 市价卖出
# 铁律三：不信账本，查物理真相；卖出量=实际可用量
# ==============================================================================

import sys, io, os, time, json

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

from dotenv import load_dotenv
load_dotenv()

from xtquant import xtdata, xtconstant
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount

# ── 配置 ──────────────────────────────────────────────────────────────────────
QMT_PATH   = r"C:\国金证券QMT交易端\userdata_mini"
ACCOUNT_ID = os.getenv("ACCOUNT_ID", "8887070833")

_DIR = os.path.dirname(os.path.abspath(__file__))

# 需要强制清仓的标的
LIQUIDATE_TARGETS = [
    {"code": "159518.SZ", "name": "标普油气ETF嘉实", "strategy": "ETF_OU_Grid"},
    {"code": "513300.SH", "name": "纳斯达克ETF华夏",  "strategy": "Hawkes_V3"},
]

# 对应账本文件（清仓后同步清账）
ETF_GRID_POS_FILE   = os.path.join(_DIR, ".state", "etf_grid_positions.json")
HAWKES_HOLDINGS_FILE = os.path.join(_DIR, ".state", "hawkes_holdings.json")

_results = []   # 记录每笔操作结果


class LiqCallback(XtQuantTraderCallback):
    def on_stock_trade(self, trade):
        code   = trade.stock_code
        filled = int(trade.traded_volume)
        price  = float(trade.traded_price)
        print(f"  ✅ [成交确认] {code} 卖出 {filled}股 @ {price:.4f}"
              f" | 成交金额≈{filled*price:.0f}元")
        _results.append({"code": code, "filled": filled, "price": price})

    def on_order_error(self, e):
        print(f"  ❌ [委托错误] seq={e.order_id} err={e.error_id} msg={e.error_msg}")


def _clear_etf_grid_ledger(code: str):
    """清空 etf_grid_positions.json 中对应标的的所有格"""
    try:
        if not os.path.exists(ETF_GRID_POS_FILE):
            return
        with open(ETF_GRID_POS_FILE, 'r', encoding='utf-8-sig') as f:
            pos = json.load(f)
        if code in pos:
            pos[code] = []
            tmp = ETF_GRID_POS_FILE + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(pos, f, indent=4, ensure_ascii=False)
            os.replace(tmp, ETF_GRID_POS_FILE)
            print(f"  📒 [账本清零] etf_grid_positions.json → {code} 所有格已清空")
    except Exception as e:
        print(f"  ⚠️ [账本清零] ETF Grid 账本写入异常: {e}")


def _clear_hawkes_ledger(code: str):
    """清空 hawkes_holdings.json 中对应标的"""
    try:
        if not os.path.exists(HAWKES_HOLDINGS_FILE):
            return
        with open(HAWKES_HOLDINGS_FILE, 'r', encoding='utf-8') as f:
            h = json.load(f)
        if code in h:
            del h[code]
            tmp = HAWKES_HOLDINGS_FILE + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(h, f, indent=2, ensure_ascii=False)
            os.replace(tmp, HAWKES_HOLDINGS_FILE)
            print(f"  📒 [账本清零] hawkes_holdings.json → {code} 已删除")
    except Exception as e:
        print(f"  ⚠️ [账本清零] Hawkes 账本写入异常: {e}")


def run_emergency_liquidation():
    print("=" * 65)
    print("🚨 紧急清仓脚本启动")
    print(f"   目标标的: {[t['code'] for t in LIQUIDATE_TARGETS]}")
    print("=" * 65)

    # ── 步骤1：连接 QMT ────────────────────────────────────────────
    print("\n[1/4] 连接 QMT 交易网关...")
    trader = XtQuantTrader(QMT_PATH, int(time.time()))
    cb = LiqCallback()
    trader.register_callback(cb)
    trader.start()
    time.sleep(3)
    conn = trader.connect()
    if conn != 0:
        print(f"❌ QMT 连接失败 conn={conn}，退出")
        return
    print("✅ QMT 连接成功")

    acc = StockAccount(ACCOUNT_ID)
    if trader.subscribe(acc) != 0:
        print("❌ 账户订阅失败，退出")
        trader.stop()
        return
    print(f"✅ 账户订阅成功: {ACCOUNT_ID}")
    time.sleep(1)

    # ── 步骤2：物理查仓（铁律三）─────────────────────────────────
    print("\n[2/4] 物理查仓（query_stock_positions）...")
    try:
        pos_list = trader.query_stock_positions(acc) or []
    except Exception as e:
        print(f"❌ 查仓异常: {e}")
        trader.stop()
        return

    target_codes = {t["code"] for t in LIQUIDATE_TARGETS}
    holdings_found = {p.stock_code: p for p in pos_list if p.stock_code in target_codes}

    if not holdings_found:
        print("⚠️  物理查仓结果：目标标的均无可用仓位（已为零）")
    else:
        for code, p in holdings_found.items():
            print(f"   📊 {code} | 可用量={p.can_use_volume} | 持仓量={p.volume} | 成本={p.open_price:.4f}")

    # ── 步骤3：逐一发卖单 ─────────────────────────────────────────
    print("\n[3/4] 发起清仓委托...")
    sell_seqs = {}

    for target in LIQUIDATE_TARGETS:
        code  = target["code"]
        name  = target["name"]
        strat = target["strategy"]

        pos = holdings_found.get(code)
        if pos is None:
            print(f"  ⚪ [{code}] {name} 无可用仓位，跳过")
            # 账本也清零，防止僵尸记录
            if strat == "ETF_OU_Grid":
                _clear_etf_grid_ledger(code)
            elif strat == "Hawkes_V3":
                _clear_hawkes_ledger(code)
            continue

        sell_qty = int(pos.can_use_volume)
        if sell_qty <= 0:
            print(f"  ⚪ [{code}] {name} can_use_volume=0（T+1锁定或已清），跳过")
            continue

        # 获取 bid1（追买一价，确保成交）
        try:
            ticks    = xtdata.get_full_tick([code])
            tick     = ticks.get(code, {})
            bid_list = tick.get("bidPrice") or []
            bid1     = float(bid_list[0]) if bid_list and bid_list[0] > 0 else 0.0
            if bid1 <= 0:
                bid1 = float(tick.get("lastPrice", 0.0))
            if bid1 <= 0:
                print(f"  ❌ [{code}] 无法获取 bid1，人工介入！")
                continue
        except Exception as e:
            print(f"  ❌ [{code}] 获取行情异常: {e}")
            continue

        print(f"  🔪 [{code}] {name} | 卖出 {sell_qty}股 @ bid1={bid1:.4f}"
              f" | 预计金额≈{sell_qty*bid1:.0f}元")

        seq = trader.order_stock(
            acc, code,
            xtconstant.STOCK_SELL,
            sell_qty,
            xtconstant.FIX_PRICE,
            round(bid1, 3),
            "Emergency_Liq",    # strategyName
            "Force_Exit",       # orderRemark
        )

        if seq > 0:
            print(f"  ✅ [{code}] 委托成功 seq={seq}")
            sell_seqs[code] = seq
        else:
            print(f"  ❌ [{code}] 委托被拒 seq={seq}，请人工检查！")

    # ── 步骤4：等待成交回报 + 同步清账本 ─────────────────────────
    print("\n[4/4] 等待成交回报（最多 30 秒）...")
    for _ in range(30):
        time.sleep(1)
        filled_codes = {r["code"] for r in _results}
        if all(code in filled_codes for code in sell_seqs.keys()):
            break

    print("\n[账本清零] 同步更新本地账本...")
    for target in LIQUIDATE_TARGETS:
        code  = target["code"]
        strat = target["strategy"]
        if strat == "ETF_OU_Grid":
            _clear_etf_grid_ledger(code)
        elif strat == "Hawkes_V3":
            _clear_hawkes_ledger(code)

    print("\n" + "=" * 65)
    print("🏁 紧急清仓脚本执行完毕")
    print(f"   发出委托: {list(sell_seqs.keys())}")
    print(f"   已确认成交: {[r['code'] for r in _results]}")
    not_filled = set(sell_seqs.keys()) - {r["code"] for r in _results}
    if not_filled:
        print(f"   ⚠️  未收到成交回报（需人工复核）: {list(not_filled)}")
    else:
        print("   ✅ 所有委托均已收到成交回报")
    print("=" * 65)

    trader.stop()


if __name__ == "__main__":
    run_emergency_liquidation()
