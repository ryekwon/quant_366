#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
t1_emergency_reset.py — T1 网格紧急外科手术脚本 (一次性)
==========================================================
背景：
  2026-03-19 早盘因 slot_capital 颗粒度 BUG，多只 ETF 跳空穿仓，
  单标的打满 4 格（~8W），导致整体超买约 15 万元。

本脚本执行三项外科任务：
  1. 物理斩仓  — QMT 卖出超额持仓，保留每只标的 1 格底仓（5000 元等值）
  2. 账本覆写  — 重新计算 MA20/ATR，将账本状态强制归一到 grid=1
  3. 日志归档  — 输出操作记录到 action_log 与本地日志文件

⚠️ 极度危险操作 —— 使用前必须确认：
  1. DRY_RUN = True  时只打印，不下单，不改账本（默认值）
  2. DRY_RUN = False 时实盘执行，不可回滚，请三思

执行方式（明早 09:30 后，QMT 已连接并处于交易时段）：
  # 先 dry-run 确认
  python t1_emergency_reset.py
  # 确认无误后改 DRY_RUN=False 或加 --live 参数实盘执行
  python t1_emergency_reset.py --live
"""

import os
import sys
import json
import time
import random
import argparse
import warnings
import traceback
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path

import yaml
import numpy as np

warnings.filterwarnings("ignore")

# ⚠️ quant-safe-patterns: xtconstant 必须与 xtdata 同行导入
from xtquant import xtdata, xtconstant
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
from dotenv import load_dotenv

# ======================================================
# 强制 UTF-8 输出
# ======================================================
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

# ======================================================
# ★ 手术参数 — 改动前请三思 ★
# ======================================================
DRY_RUN          = True          # True=只打印 False=实盘执行（命令行 --live 可覆盖）
PER_GRID_CAPITAL = 0          # 单格底仓资金（元）= 16W / 8只 / 4格
MAX_CONN_RETRY   = 5             # QMT 连接最大重试次数

# ======================================================
# 路径配置
# ======================================================
_DIR            = Path(__file__).parent.resolve()
WHITELIST_FILE  = Path(r"Z:\QuantpC_Workspace\Data\t1_grid_whitelist.yaml")
LEDGER_FILE     = _DIR / ".state" / "t1_grid_ledger.yaml"
ACTION_LOG_DIR  = _DIR / ".state" / "action_logs"
LOG_DIR         = _DIR / "logs"

QMT_PATH    = os.getenv("QMT_PATH", "")
ACCOUNT_ID  = os.getenv("ACCOUNT_ID", "")
STRATEGY_NAME = "T1_EmergencyReset"
ORDER_REMARK  = "T1_EmgReset_Sell"

LOG_DIR.mkdir(exist_ok=True)
_today_str_date = date.today().strftime("%Y-%m-%d")
_today_str_8    = date.today().strftime("%Y%m%d")
_log_path       = LOG_DIR / f"{_today_str_8}_t1_emergency_reset.log"

# ======================================================
# 日志
# ======================================================
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(_log_path, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

def log(msg: str):
    logging.info(msg)
    sys.stdout.flush()


# ======================================================
# Action Log
# ======================================================
def append_action_log(action: str, target: str, price: float, reason: str, extra: dict | None = None):
    try:
        ACTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = ACTION_LOG_DIR / f"action_{_today_str_8}.jsonl"
        record = {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "strategy":  STRATEGY_NAME,
            "action":    action,
            "target":    target,
            "price":     round(float(price), 4),
            "reason":    reason,
            "extra":     extra or {},
        }
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"⚠️ action_log 写入失败: {e}")


# ======================================================
# 白名单读取
# ======================================================
def _infer_suffix(code: str) -> str:
    return f"{code}.SH" if code[:1] in ("5", "6") else f"{code}.SZ"


def load_whitelist() -> list:
    """返回带完整后缀的白名单 code 列表"""
    if not WHITELIST_FILE.exists():
        log(f"❌ 白名单不存在: {WHITELIST_FILE}")
        return []
    with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    items = data.get("etf_list") or data.get("whitelist") or []
    result = []
    for item in items:
        code = str(item.get("code", "")).strip()
        if not code:
            continue
        if "." not in code:
            code = _infer_suffix(code)
        result.append(code)
    log(f"✅ 白名单加载: {result}")
    return result


# ======================================================
# 账本读写
# ======================================================
def load_ledger() -> dict:
    if not LEDGER_FILE.exists():
        return {}
    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log(f"❌ 账本读取失败: {e}")
        return {}


def save_ledger(ledger: dict):
    tmp = LEDGER_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(ledger, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        tmp.replace(LEDGER_FILE)
        log(f"💾 账本已覆写: {LEDGER_FILE}")
    except Exception as e:
        log(f"❌ 账本写入失败: {e}")
        if tmp.exists():
            tmp.unlink()


# ======================================================
# QMT 连接
# ======================================================
class _EmergencyCb(XtQuantTraderCallback):
    def on_disconnected(self):
        log("🚨 [Emergency] QMT 断开！")
    def on_stock_order(self, order):
        log(f"📝 [报单] {order.stock_code} | {order.order_remark} | 价={order.price:.4f} | 状态={order.order_status}")
    def on_stock_trade(self, trade):
        log(f"✅ [成交] {trade.stock_code} | 成交价={trade.traded_price:.4f} | 数量={trade.traded_volume}")
        append_action_log(
            action="紧急斩仓成交",
            target=trade.stock_code,
            price=trade.traded_price,
            reason="Emergency Reset 超额卖出",
            extra={"qty": trade.traded_volume, "remark": trade.order_remark},
        )
    def on_order_error(self, order_error):
        log(f"❌ [下单失败] {order_error.stock_code} | {order_error.error_msg}")


def init_qmt():
    """初始化 QMT，5 重重试，quant-safe-patterns: start() 后 sleep(5) 再 connect()"""
    if not QMT_PATH or not ACCOUNT_ID:
        log("❌ QMT_PATH 或 ACCOUNT_ID 未设置，请检查 .env")
        return None, None

    session_id = random.randint(100000, 999999)
    trader = XtQuantTrader(QMT_PATH, session_id)
    trader.register_callback(_EmergencyCb())
    trader.start()
    time.sleep(5)  # 必须等待，不可省略

    for attempt in range(1, MAX_CONN_RETRY + 1):
        rc = trader.connect()
        if rc == 0:
            break
        log(f"   ⏳ QMT 连接未就绪 (rc={rc})，10s 后重试 ({attempt}/{MAX_CONN_RETRY})...")
        time.sleep(10)
    else:
        log(f"❌ QMT 连接失败，已重试 {MAX_CONN_RETRY} 次")
        trader.stop()
        return None, None

    acc = StockAccount(ACCOUNT_ID)
    trader.subscribe(acc)
    log(f"✅ QMT 连接成功 | PID={os.getpid()} Session={session_id}")
    return trader, acc


# ======================================================
# 行情：MA20 / ATR20 / 当前价
# ======================================================
def calc_ma20_atr20(code: str) -> tuple[float, float, float]:
    """
    返回 (ma20, atr_abs, last_price)。失败返回 (0, 0, 0)。
    quant-safe-patterns: get_market_data_ex 访问前判断 key
    """
    try:
        xtdata.download_history_data(code, period="1d", incrementally=True)
        raw = xtdata.get_market_data_ex(
            field_list=["close", "high", "low"],
            stock_list=[code],
            period="1d",
            count=30,
        )
        if code not in raw or raw[code].empty:
            log(f"  [{code}] ⚠️ 无日线数据")
            return 0.0, 0.0, 0.0
        df = raw[code].copy()
        df.columns = [c.lower() for c in df.columns]
        df = df.dropna(subset=["close", "high", "low"])
        if len(df) < 20:
            log(f"  [{code}] ⚠️ 日线不足 20 根")
            return 0.0, 0.0, 0.0

        close = df["close"]
        high  = df["high"]
        low   = df["low"]
        ma20  = float(close.rolling(20).mean().iloc[-1])
        prev_c = close.shift(1)
        tr = np.maximum.reduce([
            high.values - low.values,
            np.abs(high.values - prev_c.values),
            np.abs(low.values - prev_c.values),
        ])
        atr = float(np.nanmean(tr[-20:]))
        last_price = float(close.iloc[-1])
        return ma20, atr, last_price
    except Exception as e:
        log(f"  [{code}] ❌ MA20/ATR 计算失败: {e}")
        return 0.0, 0.0, 0.0


def get_tick_price(code: str) -> float:
    """获取实时最新价（优先 tick，失败降级日线收盘价）"""
    try:
        tick = xtdata.get_full_tick([code]).get(code, {})
        lp   = tick.get("lastPrice", 0.0)
        if lp > 0:
            return float(lp)
    except Exception:
        pass
    # 降级：用日线收盘价
    _, _, last = calc_ma20_atr20(code)
    return last


# ======================================================
# 模块 1：物理斩仓
# ======================================================
def module1_surgical_sell(trader, acc, whitelist: list, dry_run: bool) -> dict:
    """
    获取真实持仓，计算超额股数并卖出。
    返回 {code: {"real": int, "target": int, "excess": int, "price": float}} 手术报告
    """
    log("\n" + "=" * 65)
    log("🔪 模块 1：物理斩仓 — 超额持仓外科切除")
    log("=" * 65)

    # 获取真实持仓
    try:
        positions_raw = trader.query_stock_positions(acc)
        # 转换为 {code: volume} 字典
        real_pos: dict[str, int] = {}
        for p in (positions_raw or []):
            # can_use_volume = 可用 (T+0 卖出)，volume = 总持仓
            real_pos[p.stock_code] = int(getattr(p, "volume", 0))
        log(f"📊 当前真实持仓: {real_pos}")
    except Exception as e:
        log(f"❌ 获取持仓失败: {e}")
        traceback.print_exc()
        return {}

    # 订阅行情
    for code in whitelist:
        xtdata.subscribe_quote(code, period="tick")
    time.sleep(2)

    surgery_report: dict = {}

    for code in whitelist:
        real_shares = real_pos.get(code, 0)
        current_price = get_tick_price(code)

        if current_price <= 0:
            log(f"  [{code}] ⚠️ 无法获取价格，跳过斩仓")
            continue

        target_shares = int(PER_GRID_CAPITAL / current_price / 100) * 100
        excess_shares = max(0, real_shares - target_shares)

        log(
            f"  [{code}] "
            f"真实={real_shares}股 | 目标底仓={target_shares}股 | "
            f"超额={excess_shares}股 | 现价={current_price:.4f}"
        )

        surgery_report[code] = {
            "real":    real_shares,
            "target":  target_shares,
            "excess":  excess_shares,
            "price":   current_price,
        }

        if excess_shares <= 0:
            log(f"  [{code}] ✅ 持仓正常，无需斩仓。")
            continue

        # ── 卖出超额股数 ─────────────────────────────────────────────
        log(
            f"\n  🚨 [紧急斩仓] {code} 真实持仓 {real_shares} 股，"
            f"保留底仓 {target_shares} 股，准备强平 {excess_shares} 股！"
        )

        if dry_run:
            log(f"  [DRY-RUN] 模拟 SELL {excess_shares}@对手价 (跳过真实下单)")
            append_action_log(
                action="[DRY-RUN] 紧急斩仓",
                target=code,
                price=current_price,
                reason="超额持仓 DRY-RUN 模拟",
                extra={"real": real_shares, "target": target_shares, "excess": excess_shares},
            )
            continue

        # 实盘：用对手价（盘口保护级别最优）卖出
        try:
            tick = xtdata.get_full_tick([code]).get(code, {}) or {}
            bid1 = (tick.get("bidPrice") or [0])[0]  # 买一价（taker 卖出挂单）
            sell_price = bid1 if bid1 > 0 else current_price

            seq = trader.order_stock(
                acc,
                code,
                xtconstant.STOCK_SELL,
                excess_shares,
                xtconstant.FIX_PRICE,
                sell_price,
                STRATEGY_NAME,
                ORDER_REMARK,
            )

            if seq and seq > 0:
                log(
                    f"  🚨 [紧急斩仓] {code} 真实持仓 {real_shares} 股，"
                    f"保留底仓 {target_shares} 股，已强平 {excess_shares} 股！"
                    f"(seq={seq} price={sell_price:.4f})"
                )
                append_action_log(
                    action="紧急斩仓",
                    target=code,
                    price=sell_price,
                    reason=f"超额 {excess_shares} 股 (real={real_shares} target={target_shares})",
                    extra={"seq": seq, "real": real_shares, "target": target_shares, "excess": excess_shares},
                )
            else:
                log(f"  ❌ [{code}] 斩仓下单失败 seq={seq}")
        except Exception as e:
            log(f"  ❌ [{code}] 斩仓异常: {e}")
            traceback.print_exc()

    return surgery_report


# ======================================================
# 模块 2：账本覆写
# ======================================================
def module2_ledger_reset(surgery_report: dict, whitelist: list, dry_run: bool):
    """
    对参与手术的标的，重置账本为 grid=1 底仓状态。
    并重新计算 MA20/ATR 写入 base_price 和 dynamic_step。
    """
    log("\n" + "=" * 65)
    log("🧠 模块 2：账本覆写 — 强制归一化为 grid=1 底仓")
    log("=" * 65)

    ledger = load_ledger()

    for code in whitelist:
        report = surgery_report.get(code)
        if not report:
            log(f"  [{code}] 无手术报告，跳过账本覆写。")
            continue

        current_price = report["price"]
        target_shares = report["target"]

        # 重新计算 MA20 / ATR
        ma20, atr_abs, _ = calc_ma20_atr20(code)

        if ma20 <= 0 or current_price <= 0:
            log(f"  [{code}] ⚠️ 价格或 MA20 异常，跳过账本重置")
            continue

        new_step = round(max(0.8 * (atr_abs / current_price), 0.008), 6)

        # 资金参数（与重构后的 executor 一致）
        symbol_max_limit = round(PER_GRID_CAPITAL * 4, 2)   # 4格合计 = 2W
        per_grid_capital_val = float(PER_GRID_CAPITAL)

        new_rec = {
            "base_price":        round(ma20, 6),
            "dynamic_step":      new_step,
            "atr_value":         round(atr_abs, 6),
            "current_grid":      1,            # 强制归一：保留 1 格底仓
            "available_shares":  target_shares, # 底仓可用（今日已成交，无 T+1 限制）
            "locked_shares":     0,
            "idle_days":         0,
            "cooldown_until":    "2000-01-01",
            "last_settle_date":  _today_str_date,
            "symbol_max_limit":  symbol_max_limit,
            "per_grid_capital":  per_grid_capital_val,
        }

        log(
            f"  [{code}] 账本覆写 → "
            f"base={new_rec['base_price']:.4f} step={new_step:.4f} "
            f"grid=1 avail={target_shares} locked=0"
        )
        append_action_log(
            action="账本覆写",
            target=code,
            price=current_price,
            reason="Emergency Reset — 归一化 grid=1",
            extra=new_rec,
        )

        if not dry_run:
            ledger[code] = new_rec
        else:
            log(f"  [DRY-RUN] 账本覆写模拟，不写入磁盘。")

    if not dry_run:
        save_ledger(ledger)
    else:
        log("\n[DRY-RUN] 账本覆写模拟完成，磁盘未改动。")


# ======================================================
# 主入口
# ======================================================
def main():
    global DRY_RUN

    parser = argparse.ArgumentParser(description="T1 紧急外科手术脚本")
    parser.add_argument(
        "--live",
        action="store_true",
        help="实盘模式（不加此参数默认 DRY-RUN，只打印不执行）",
    )
    args = parser.parse_args()

    if args.live:
        DRY_RUN = False

    log("=" * 65)
    log("🚨 T1 紧急外科手术脚本 启动")
    log(f"   模式: {'⚠️  实盘执行 LIVE' if not DRY_RUN else '🔍 DRY-RUN 仅模拟'}")
    log(f"   PER_GRID_CAPITAL = {PER_GRID_CAPITAL} 元")
    log(f"   目标：每只标的保留 {PER_GRID_CAPITAL} 元等值底仓（grid=1）")
    log("=" * 65)

    # 实盘模式二次确认
    if not DRY_RUN:
        log("\n⚠️  ⚠️  ⚠️  警告：即将执行实盘卖出！⚠️  ⚠️  ⚠️")
        log("请在 10 秒内按 Ctrl+C 取消，否则将自动继续...")
        for i in range(10, 0, -1):
            log(f"   倒计时: {i} 秒")
            time.sleep(1)
        log("♦ 确认执行，开始手术...")

    # 加载白名单
    whitelist = load_whitelist()
    if not whitelist:
        log("❌ 白名单为空，退出。")
        sys.exit(1)

    # 连接 QMT
    trader, acc = init_qmt()
    if trader is None and not DRY_RUN:
        log("❌ QMT 连接失败，实盘模式下无法继续。")
        sys.exit(1)

    if trader is None and DRY_RUN:
        log("⚠️ QMT 未连接，DRY-RUN 模式下用假持仓模拟（real_shares=0）")

    try:
        # ── 模块 1：斩仓 ─────────────────────────────────────────────
        if trader:
            surgery_report = module1_surgical_sell(trader, acc, whitelist, DRY_RUN)
        else:
            # DRY-RUN 无 QMT：模拟一个假的手术报告
            log("  [DRY-RUN / No QMT] 使用假持仓（real=0），仅测试账本覆写流程")
            surgery_report = {
                code: {"real": 0, "target": 0, "excess": 0, "price": 0.0}
                for code in whitelist
            }

        # ── 等待成交（实盘模式稍等让成交回报落盘）────────────────────
        if not DRY_RUN:
            log("\n⏳ 等待 15 秒让成交回报稳定...")
            time.sleep(15)

        # ── 模块 2：账本覆写 ─────────────────────────────────────────
        module2_ledger_reset(surgery_report, whitelist, DRY_RUN)

        log("\n" + "=" * 65)
        log("🏁 手术完成！")
        log(f"   日志已保存: {_log_path}")
        log("=" * 65)

    except KeyboardInterrupt:
        log("\n⏹ 手动中断，手术终止。")
    except Exception as e:
        log(f"\n🔥 手术脚本崩溃: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        if trader:
            try:
                trader.stop()
            except Exception:
                pass
        log("🔓 QMT 连接已断开，脚本退出。")


if __name__ == "__main__":
    main()
