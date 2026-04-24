# -*- coding: utf-8 -*-
"""
fix_grid_state_noon.py — 午休持仓对齐工具（一次性使用）
============================================================
从 reconcile_report.json 读取已发现的偏差，
结合当前行情，将 grid_state.json 中的 volume/current_lots/base_price
更新为实盘真实状态，让下午 T0 引擎能从正确基点继续工作。
"""
import os, sys, json, math, time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
QMT_PATH   = os.getenv("QMT_PATH", "")
ACCOUNT_ID = os.getenv("ACCOUNT_ID", "")

_DIR          = Path(__file__).parent.parent
STATE_FILE    = _DIR / ".state" / "grid_state.json"
REPORT_FILE   = _DIR / ".state" / "reconcile_report.json"
TARGETS_FILE  = _DIR / ".state" / "grid_targets.yaml"

def main():
    # 1. 读取对账报告
    with open(REPORT_FILE, encoding='utf-8') as f:
        report = json.load(f)

    discrepancies = report.get("discrepancies", [])
    if not discrepancies:
        print("✅ 无偏差，无需修正。")
        return

    # 2. 读取 grid_state 和 YAML targets
    with open(STATE_FILE, encoding='utf-8') as f:
        grid_state = json.load(f)

    import yaml
    with open(TARGETS_FILE, encoding='utf-8') as f:
        targets = yaml.safe_load(f) or {}

    # 3. 连接 xtdata 获取实时价格
    print("⏳ 连接 xtdata 获取当前行情...")
    from xtquant import xtdata
    xtdata.connect()
    time.sleep(2)

    codes = [d['code'] for d in discrepancies]
    ticks = xtdata.get_full_tick(codes)
    print(f"✅ 行情已拉取: {list(ticks.keys())}")

    # 4. 逐项修正 grid_state
    print("\n─── 开始修正 grid_state.json ───")
    for d in discrepancies:
        code      = d['code']
        real_vol  = d['real_vol']
        old_vol   = d['tracked_vol']

        tick         = ticks.get(code, {})
        last_price   = tick.get('lastPrice', 0)
        bid1         = (tick.get('bidPrice') or [last_price])[0]
        current_price = bid1 if bid1 > 0 else last_price

        if current_price <= 0:
            print(f"  ⚠️ [{code}] 无法获取实时价格，跳过")
            continue

        # 从 YAML 取 trade_amount
        trade_amount = targets.get(code, {}).get('trade_amount', 10000)
        spread_pct   = targets.get(code, {}).get('spread_pct', 0.006)

        # 估算每格手数
        lot_qty = math.floor((trade_amount / current_price) / 100) * 100
        if lot_qty < 100:
            lot_qty = 100

        # current_lots = 实盘手数 // 每格手数（向下取整）
        current_lots = max(1, real_vol // lot_qty)

        # base_price 设为当前价（新的 T0 中枢）
        base_price = round(current_price, 3)

        # 写入 grid_state
        rs = grid_state.get(code, {})
        old_lots      = rs.get('current_lots', 0)
        old_base      = rs.get('base_price', 0)

        rs['volume']        = real_vol
        rs['current_lots']  = current_lots
        rs['base_price']    = base_price
        rs['last_buy_price']= base_price
        rs['lot_qty']       = lot_qty  # 保存每格手数，供卖出对称使用
        rs['status']        = 'active'
        grid_state[code] = rs

        print(f"  ✅ [{code}] {rs.get('name', '')} 修正完毕:")
        print(f"      volume      : {old_vol} → {real_vol}")
        print(f"      current_lots: {old_lots} → {current_lots}")
        print(f"      base_price  : {old_base:.4f} → {base_price:.4f}  (当前价)")
        print(f"      lot_qty     : {lot_qty} 股/格")
        print(f"      上轨        : {base_price + base_price * spread_pct:.4f}")
        print(f"      下轨        : {base_price - base_price * spread_pct:.4f}")

    # 5. 保存
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(grid_state, f, indent=2, ensure_ascii=False)

    print(f"\n✅ grid_state.json 已更新，可以重启 T0 引擎迎接下午行情！")
    print(f"   提示: 运行 'python t0_multigrid_executor.py' 或重启 autopilot_master")

if __name__ == '__main__':
    main()
