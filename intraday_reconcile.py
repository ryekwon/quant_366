# -*- coding: utf-8 -*-
"""
intraday_reconcile.py — 盘中持仓实盘对账引擎
================================================
职责：
  - 对比 QMT 实盘持仓 vs grid_state.json 账本
  - 发现残差持仓（账本与实盘数量不符）时发出N8N告警
  - 14:55 触发时，对残差做保守清仓（防止 T0 持仓过夜）
  - 结果写入 .state/reconcile_report.json 供日终复盘使用

调用方式（由 autopilot_master 定时触发）:
  python intraday_reconcile.py --mode check       # 仅检查，不操作（11:28）
  python intraday_reconcile.py --mode eod_clear   # 检查+清仓残差（14:55）
"""

import os
import sys
import json
import time
import argparse
import datetime
from pathlib import Path
from dotenv import load_dotenv

# ─── 强制 UTF-8 输出 ─────────────────────────────────────────
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

load_dotenv()
QMT_PATH   = os.getenv("QMT_PATH", "")
ACCOUNT_ID = os.getenv("ACCOUNT_ID", "")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")

_DIR         = Path(__file__).parent.resolve()
STATE_FILE   = _DIR / ".state" / "grid_state.json"
REPORT_FILE  = _DIR / ".state" / "reconcile_report.json"
LOG_DIR      = _DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ─── 告警阈值 ─────────────────────────────────────────────────
MIN_DELTA_SHARES = 100  # 偏差 ≥ 100 股才告警（忽略零股噪音）

# ─── T0 网格标的标识（只对这些做清仓操作，手动仓位绝对不碰）
GRID_CODES_FILE = _DIR / ".state" / "grid_targets.yaml"


def _send_webhook(title: str, message: str):
    """发送N8N告警（失败静默）"""
    if not N8N_WEBHOOK_URL:
        return
    try:
        import requests
        requests.post(N8N_WEBHOOK_URL, json={
            "title": title, "message": message,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }, timeout=5)
    except Exception:
        pass


def _load_grid_state() -> dict:
    """读取 grid_state.json"""
    if not STATE_FILE.exists():
        print(f"⚠️ 找不到 {STATE_FILE}，跳过对账")
        return {}
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ 读取 grid_state 失败: {e}")
        return {}


def _load_grid_targets() -> set:
    """加载今日 T0 网格目标标的集合（只清仓这些标的的残差）"""
    targets = set()
    if not GRID_CODES_FILE.exists():
        return targets
    try:
        import yaml
        with open(GRID_CODES_FILE, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        targets = set(data.keys())
    except Exception as e:
        print(f"⚠️ 读取 grid_targets 失败: {e}")
    return targets


def _load_all_protected_codes() -> set:
    """
    🛡️ 多策略防火墙 — 读取所有非T0策略账本，构建保护标的集合。
    对账引擎遇到保护标的时：仅告警，禁止清仓。

    防火墙成员（按策略划分）：
      - ETF OU Grid : .state/etf_grid_positions.json   (key=code)
      - Momentum    : .state/momentum_holdings.json    (key=code)
      - Sniper      : .state/sniper_holdings.json      (key=code)
      - MacroRot    : .state/macro_slots.json          (slot_a / slot_b)
      - T1 Grid     : .state/t1_grid_ledger.yaml       (key=code)
      - Fat Fish    : .state/fat_fish_slots.yaml       (slots dict key=code)
      - Underdog    : .state/underdog_slots.json       (key=code)
    """
    protected: set = set()
    state_dir = _DIR / ".state"

    def _safe_json_keys(path) -> set:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                d = json.load(f)
            if isinstance(d, dict):
                return set(d.keys())
        except Exception:
            pass
        return set()

    # ── ETF OU Grid ──────────────────────────────────────────────
    protected |= _safe_json_keys(state_dir / "etf_grid_positions.json")

    # ── Momentum Vector ──────────────────────────────────────────
    protected |= _safe_json_keys(state_dir / "momentum_holdings.json")

    # ── Sniper ───────────────────────────────────────────────────
    protected |= _safe_json_keys(state_dir / "sniper_holdings.json")

    # ── MacroRotation（slot_a / slot_b 字段，不是 key）────────────
    try:
        macro_path = state_dir / "macro_slots.json"
        if macro_path.exists():
            with open(macro_path, 'r', encoding='utf-8') as f:
                ms = json.load(f)
            for fld in ("slot_a", "slot_b"):
                code = ms.get(fld)
                if code:
                    protected.add(code)
    except Exception:
        pass

    # ── T1 Grid ──────────────────────────────────────────────────
    try:
        import yaml as _yaml
        t1_path = state_dir / "t1_grid_ledger.yaml"
        if t1_path.exists():
            with open(t1_path, 'r', encoding='utf-8') as f:
                t1d = _yaml.safe_load(f) or {}
            protected |= set(t1d.keys())
    except Exception:
        pass

    # ── Fat Fish ─────────────────────────────────────────────────
    try:
        import yaml as _yaml
        ff_path = state_dir / "fat_fish_slots.yaml"
        if ff_path.exists():
            with open(ff_path, 'r', encoding='utf-8') as f:
                ffd = _yaml.safe_load(f) or {}
            protected |= set((ffd.get('slots') or {}).keys())
    except Exception:
        pass

    # ── Underdog ─────────────────────────────────────────────────
    protected |= _safe_json_keys(state_dir / "underdog_slots.json")

    if protected:
        print(f"🛡️ [多策略防火墙] 保护标的 ({len(protected)} 只): {sorted(protected)}")
    return protected


def _connect_trader():
    """建立 QMT 交易连接，返回 (trader, account) 或 (None, None)"""
    try:
        from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
        from xtquant.xttype import StockAccount
        from xtquant import xtdata

        xtdata.connect()

        session_id = int(time.time())
        trader = XtQuantTrader(QMT_PATH, session_id)
        trader.register_callback(XtQuantTraderCallback())
        trader.start()
        print("⏳ 等待交易网关初始化（5秒）...")
        time.sleep(5)

        res = trader.connect()
        if res != 0:
            print(f"❌ XtQuantTrader 连接失败 (错误码: {res})")
            return None, None

        acc = StockAccount(ACCOUNT_ID)
        sub = trader.subscribe(acc)
        if sub != 0:
            print(f"❌ 账户订阅失败 (错误码: {sub})")
            return None, None

        print(f"✅ QMT 连接成功，账户 {ACCOUNT_ID} 已就绪")
        time.sleep(1)
        return trader, acc
    except Exception as e:
        print(f"❌ QMT 连接异常: {e}")
        return None, None


def run_reconcile(mode: str = "check") -> dict:
    """
    执行持仓对账。
    mode: "check"     — 仅检查，只告警不操作
          "eod_clear" — 检查 + 自动清仓残差（仅T0标的）
    返回: reconcile 报告字典
    """
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*55}")
    print(f"🔍 盘中持仓对账引擎启动 [{mode.upper()}] — {now_str}")
    print(f"{'='*55}")

    grid_state       = _load_grid_state()
    grid_targets     = _load_grid_targets()
    protected_codes  = _load_all_protected_codes()   # 🛡️ 多策略防火墙

    if not QMT_PATH or not ACCOUNT_ID:
        print("❌ 缺少 QMT_PATH 或 ACCOUNT_ID，无法连接 QMT")
        return {"error": "missing_config"}

    trader, acc = _connect_trader()
    if not trader:
        return {"error": "qmt_connection_failed"}

    # ── 拉取实盘持仓 ─────────────────────────────────────────
    try:
        real_positions = trader.query_stock_positions(acc) or []
    except Exception as e:
        print(f"❌ 查询实盘持仓失败: {e}")
        trader.stop()
        return {"error": f"query_failed: {e}"}

    real_dict = {
        p.stock_code: {"volume": int(p.volume), "avg_cost": float(p.open_price)}
        for p in real_positions if p.volume > 0
    }

    # ── 对账逻辑 ─────────────────────────────────────────────
    report = {
        "timestamp":  now_str,
        "mode":       mode,
        "discrepancies": [],
        "grid_only":  [],   # 账本有但实盘没有（幽灵）
        "real_only":  [],   # 实盘有但账本没有
        "action_taken": []
    }

    print(f"\n📊 实盘持仓: {list(real_dict.keys())}")
    print(f"📋 网格账本: { {k: v.get('volume',0) for k,v in grid_state.items()} }")

    # 1. 检查网格账本中的每个标的
    for code, rs in grid_state.items():
        # 🛡️ 防火墙：其他策略持有的标的，只打印日志，禁止任何操作
        if code in protected_codes:
            real_vol_check = real_dict.get(code, {}).get('volume', 0)
            print(f"🛡️ [{code}] 受多策略防火墙保护（ETF-OU/Momentum/Sniper等），"
                  f"实盘 {real_vol_check} 股，T0账本跳过对账，不干预。")
            continue

        tracked_vol = rs.get('volume', 0)
        real_vol    = real_dict.get(code, {}).get('volume', 0)
        delta       = real_vol - tracked_vol

        if abs(delta) >= MIN_DELTA_SHARES:
            entry = {
                "code":        code,
                "name":        rs.get('name', code),
                "tracked_vol": tracked_vol,
                "real_vol":    real_vol,
                "delta":       delta,
                "in_yaml":     code in grid_targets
            }
            report["discrepancies"].append(entry)

            direction = "多" if delta > 0 else "少"
            msg = (f"⚠️ [{code} {rs.get('name','')}] "
                   f"账本 {tracked_vol} 股 | 实盘 {real_vol} 股 | "
                   f"偏差 {delta:+d} 股（实盘比账本{direction} {abs(delta)} 股）")
            print(msg)

            # 14:55 模式 + T0网格标的 + 实盘多于账本 → 清仓残差
            if mode == "eod_clear" and delta > 0 and code in grid_targets:
                try:
                    from xtquant import xtconstant
                    from xtquant import xtdata
                    tick = xtdata.get_full_tick([code]).get(code, {})
                    bid1 = tick.get('bidPrice', [0])[0]
                    sell_price = round(bid1 - 0.001, 3) if bid1 > 0 else round(
                        tick.get('lastPrice', 0) * 0.999, 3)

                    if sell_price <= 0:
                        print(f"   ⚠️ [{code}] 无法获取有效卖出价，跳过自动清仓")
                        continue

                    sell_qty = (abs(delta) // 100) * 100
                    if sell_qty < 100:
                        continue

                    seq = trader.order_stock(
                        acc, code, xtconstant.STOCK_SELL,
                        sell_qty, xtconstant.FIX_PRICE, sell_price,
                        'V4_Reconcile', 'EOD_Clear'
                    )
                    action = {
                        "code": code, "action": "EOD_Clear",
                        "qty": sell_qty, "price": sell_price, "seq": seq
                    }
                    report["action_taken"].append(action)
                    print(f"   ✅ [{code}] 残差清仓已提交: {sell_qty}股 @ {sell_price:.3f} | 单号:{seq}")
                    _send_webhook(
                        f"🧹 [{code}] 残差自动清仓",
                        f"账本 {tracked_vol} 股, 实盘 {real_vol} 股\n"
                        f"清仓 {sell_qty} 股 @ {sell_price:.3f} (收盘前主动清仓)"
                    )
                except Exception as e:
                    print(f"   ❌ [{code}] 自动清仓失败: {e}")

        elif tracked_vol > 0 and real_vol == 0:
            # 幽灵：账本有格数但实盘已清空
            report["grid_only"].append({"code": code, "tracked_vol": tracked_vol})
            print(f"👻 [{code}] 幽灵持仓：账本 {tracked_vol} 股，实盘 0（已自然平仓）")

    # ── EOD 二次扫描：T0 标的账实一致但仍有仓位 → 强制清仓 ──────────────
    # 解决场景：T0 策略本身未能在收盘前平仓，reconcile 第一遍因无偏差跳过，
    # 但实盘仍有持仓过夜。eod_clear 模式下对这类账实一致但 volume>0 的标的补刀。
    if mode == "eod_clear" and trader:
        for code, rs in grid_state.items():
            # 🛡️ EOD 二次强清同样受防火墙保护
            if code in protected_codes:
                continue

            real_vol     = real_dict.get(code, {}).get("volume", 0)
            tracked_vol  = rs.get("volume", 0)
            grid_vol     = rs.get("lot_qty", 0)  # lot_qty 是实际持仓量
            # 账实一致 + T0 标的有实仓 + 未在第一遍处理过（delta 为 0）
            eod_qty = (real_vol // 100) * 100
            if (real_vol > 0
                    and abs(real_vol - tracked_vol) < MIN_DELTA_SHARES  # 账实一致
                    and code in grid_targets                              # 确认是 T0 标的
                    and eod_qty >= 100
                    and not any(a["code"] == code for a in report["action_taken"])):  # 未双重处理
                print(f"🔔 [{code}] EOD 强清：账实一致但 T0 有持仓 {real_vol} 股，T0 未自平仓，强制清仓...")
                try:
                    from xtquant import xtconstant
                    from xtquant import xtdata
                    tick = xtdata.get_full_tick([code]).get(code, {})
                    bid1 = tick.get("bidPrice", [0])[0]
                    sell_price = round(bid1 - 0.001, 3) if bid1 > 0 else round(
                        tick.get("lastPrice", 0) * 0.999, 3)
                    if sell_price <= 0:
                        print(f"   ⚠️ [{code}] 无法获取有效卖出价，跳过强清")
                        continue
                    seq = trader.order_stock(
                        acc, code, xtconstant.STOCK_SELL,
                        eod_qty, xtconstant.FIX_PRICE, sell_price,
                        "V4_Reconcile", "EOD_ForceClose"
                    )
                    action = {"code": code, "action": "EOD_ForceClose",
                              "qty": eod_qty, "price": sell_price, "seq": seq}
                    report["action_taken"].append(action)
                    print(f"   ✅ [{code}] 强清已提交: {eod_qty}股 @ {sell_price:.3f} | 单号:{seq}")
                    _send_webhook(
                        f"🔔 [{code}] T0 未自平仓 → EOD 强清",
                        f"T0 账实一致但有 {real_vol} 股持仓过夜风险\n"
                        f"强清 {eod_qty} 股 @ {sell_price:.3f}"
                    )
                except Exception as e:
                    print(f"   ❌ [{code}] 强清失败: {e}")

    # 2. 实盘有但账本没有（手动买入/其他策略，不处理）
    for code, pos_info in real_dict.items():
        if code not in grid_state:
            report["real_only"].append({"code": code, "real_vol": pos_info["volume"]})
            print(f"🧱 [{code}] 实盘有 {pos_info['volume']} 股，不在网格账本中（手动/其他策略持仓，不干预）")

    # ── 汇总 ─────────────────────────────────────────────────
    n_discord = len(report["discrepancies"])
    n_ghost   = len(report["grid_only"])
    n_actions = len(report["action_taken"])

    print(f"\n{'─'*55}")
    print(f"✅ 对账完成 | 偏差: {n_discord} 项 | 幽灵: {n_ghost} 项 | 操作: {n_actions} 项")

    # ── 推送总结告警 ─────────────────────────────────────────
    if n_discord > 0 or n_ghost > 0:
        items = []
        for d in report["discrepancies"]:
            items.append(f"• [{d['code']}] 账本{d['tracked_vol']}股 vs 实盘{d['real_vol']}股 (Δ{d['delta']:+d})")
        for g in report["grid_only"]:
            items.append(f"• [{g['code']}] 幽灵持仓 {g['tracked_vol']} 股（实盘已归零）")
        body = "\n".join(items)
        if n_actions > 0:
            body += f"\n\n已自动清仓 {n_actions} 笔残差。"
        _send_webhook(
            f"🔍 盘中对账 [{mode.upper()}] — 发现 {n_discord+n_ghost} 项异常",
            body
        )
    else:
        print("🎉 账本与实盘完全一致，无异常！")

    # ── 落盘报告 ─────────────────────────────────────────────
    try:
        with open(REPORT_FILE, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"📄 对账报告已保存: {REPORT_FILE}")
    except Exception as e:
        print(f"⚠️ 报告保存失败: {e}")

    trader.stop()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="盘中持仓对账引擎")
    parser.add_argument("--mode", choices=["check", "eod_clear"], default="check",
                        help="check=仅检查(11:28), eod_clear=检查+清仓(14:55)")
    args = parser.parse_args()

    result = run_reconcile(args.mode)
    n_issues = len(result.get("discrepancies", [])) + len(result.get("grid_only", []))
    sys.exit(0 if n_issues == 0 else 1)
