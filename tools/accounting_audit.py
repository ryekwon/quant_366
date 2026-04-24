# -*- coding: utf-8 -*-
"""
accounting_audit.py — 全系统持仓穿透式审计 (Accounting Audit)
=========================================================
功能：
1. 物理真相拉取: 从 QMT 查询绝对真实的持仓。
2. 逻辑账本对比: 与 T0 (.state/grid_state.json) 和 Sniper (.state/sniper_holdings.json) 对齐。
3. 盘中归属校验: 扫描今日成交 (query_stock_trades)，根据 strategy_name 确定新增持仓归属。
4. 资金池核算: 生成审计报告，标记 "Unknown/Manual" 标的，防止干扰自动策略。
"""
import os
import json
import time
import yaml
import random
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from xtquant import xtdata, xttrader
from xtquant.xttype import StockAccount

# ─── 强制 UTF-8 ─────────────────────────────────────────────
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ─── 配置与路径 ──────────────────────────────────────────────
load_dotenv()
_DIR = Path(__file__).parent.parent.resolve()
STATE_DIR = _DIR / ".state"
LOG_DIR = _DIR / "logs"
SNIPER_FILE = STATE_DIR / "sniper_holdings.json"
T0_FILE = STATE_DIR / "grid_state.json"
ROTATION_FILE = STATE_DIR / "rotation_targets.yaml"  # 假设轮动配置在此
AUDIT_REPORT_DIR = STATE_DIR / "audit_reports"
AUDIT_REPORT_DIR.mkdir(exist_ok=True)

QMT_PATH = os.getenv("QMT_PATH")
ACC_ID = os.getenv("ACCOUNT_ID")

def load_json(path):
    if not path.exists(): return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except: return {}

def load_yaml(path):
    if not path.exists(): return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except: return {}

def run_audit():
    print(f"\n{'='*70}")
    print(f"📊 [Accounting Audit] 启动全系统持仓穿透式审计 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}\n")

    # 1. 连接 QMT
    session_id = random.randint(100000, 999999)
    xt_trader = xttrader.XtQuantTrader(QMT_PATH, session_id)
    xt_trader.start()
    if xt_trader.connect() != 0:
        print("❌ 无法连接 QMT，审计终止。")
        return

    acc = StockAccount(ACC_ID)
    time.sleep(2) # 等待就绪

    # 2. 拉取物理真相
    real_positions = xt_trader.query_stock_positions(acc)
    real_trades = xt_trader.query_stock_trades(acc) or []
    real_dict = {pos.stock_code: pos for pos in real_positions if pos.volume > 0}
    
    # 3. 加载逻辑账本
    sniper_holdings = load_json(SNIPER_FILE)
    t0_state = load_json(T0_FILE)
    rotation_targets = load_yaml(ROTATION_FILE) # 轮动通常也有个目标文件

    print(f"🔍 物理实盘持仓数: {len(real_dict)}")
    print(f"📖 Sniper 账本记录: {len(sniper_holdings)}")
    print(f"📖 T0 状态机记录: {len(t0_state)}")
    print("-" * 50)

    # 4. 执行穿透归属分析
    audit_results = {
        "Sniper": [],
        "T0": [],
        "Rotation": [],
        "Unknown_Manual": [],
        "Ghost_Logic": [] # 逻辑账本有，但实盘没有的（幽灵）
    }

    processed_codes = set()

    # -- 4.1. 实盘资产归口判定 --
    for code, pos in real_dict.items():
        attribution = "Unknown_Manual"
        reason = "No matching strategy records found"

        # A. 检查是否在 T0 状态机 (ETF 居多)
        if code in t0_state:
            attribution = "T0"
            reason = "Matches T0 Grid state"
        
        # B. 检查是否在 Sniper 账本
        elif code in sniper_holdings:
            attribution = "Sniper"
            reason = "Matches Sniper logic ledger"

        # C. 兜底校验：检查今日成交记录 (Lineage Proof)
        else:
            # 扫描今日该标的是否有策略下单记录
            for t in real_trades:
                if t.stock_code == code:
                    s_name = t.strategy_name.lower()
                    if "sniper" in s_name:
                        attribution = "Sniper"
                        reason = f"Identified by EOD trade log (Strategy: {t.strategy_name})"
                        # 反向补录进 Sniper 逻辑账本 (自愈)
                        if code not in sniper_holdings:
                            print(f"🩹 [Self-Healing] 发现 Sniper 遗漏资产 {code}，正在补录...")
                            detail = xtdata.get_instrument_detail(code) or {}
                            sniper_holdings[code] = {
                                "name": detail.get('InstrumentName', code),
                                "buy_price": float(pos.open_price),
                                "qty": int(pos.volume),
                                "date": datetime.now().strftime("%Y-%m-%d"),
                                "auto_recovered": True
                            }
                        break
                    elif "t0" in s_name or "grid" in s_name:
                        attribution = "T0"
                        reason = f"Identified by EOD trade log (Strategy: {t.strategy_name})"
                        break

        audit_results[attribution].append({
            "code": code,
            "qty": pos.volume,
            "mkt_val": round(pos.market_value, 2),
            "reason": reason
        })
        processed_codes.add(code)

    # -- 4.2. 逻辑幽灵清理 --
    # 如果 Sniper 账本里有，但实盘没了，说明是“虚假繁荣”，需要剔除
    ghosts = []
    final_sniper_holdings = {}
    for code, info in sniper_holdings.items():
        if code in processed_codes:
            final_sniper_holdings[code] = info
        else:
            ghosts.append(code)
            audit_results["Ghost_Logic"].append({"code": code, "strategy": "Sniper"})

    # 5. 物理引擎持久化 (会计核算结果)
    # A. 更新 Sniper 账本
    with open(SNIPER_FILE, 'w', encoding='utf-8') as f:
        json.dump(final_sniper_holdings, f, ensure_ascii=False, indent=4)
    
    # B. 生成审计报告
    report_name = f"audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path = AUDIT_REPORT_DIR / report_name
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(audit_results, f, ensure_ascii=False, indent=2)

    # 6. 控制台汇总报告
    print("\n✅ 审计核算汇总:")
    for group, items in audit_results.items():
        color = ""
        if group == "Unknown_Manual": color = "\033[93m" # Yellow
        elif group == "Ghost_Logic": color = "\033[91m" # Red
        else: color = "\033[92m" # Green
        
        print(f"{color}  [{group}]: {len(items)} 只标的\033[0m")
        for item in items:
            print(f"    - {item['code']}")

    if audit_results["Unknown_Manual"]:
        print(f"\n⚠️  [警告] 发现 {len(audit_results['Unknown_Manual'])} 只非策略持仓（可能是手动买入），已隔离，不会干扰自动平仓逻辑。")
    
    if ghosts:
        print(f"\n🧹 [自愈] 已清理 {len(ghosts)} 个逻辑幽灵持仓（实盘已无对应资产）。")

    print(f"\n📝 审计详细报告已存至: {report_path}")
    print(f"{'='*70}\n")
    xt_trader.stop()

if __name__ == "__main__":
    run_audit()
