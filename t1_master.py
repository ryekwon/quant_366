#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
t1_master.py — T+1 纯机械化网格交易【盘前调度与数学计算】
====================================================================
调度时间：每日 09:22 由 autopilot_master.py 阻塞启动，执行一次后退出。

职责一：T+1 跨日物理解冻
  - 读取 .state/t1_grid_ledger.yaml
  - 若 last_settle_date != today，将 locked_shares 累加到 available_shares，
    locked_shares 归零，并按情况递增 idle_days

职责二：资金参数写入（每日强制覆写）
  - TOTAL_CAPITAL / N 得出 symbol_max_limit（标的封顶）
  - symbol_max_limit / MAX_GRIDS_DOWN 得出 per_grid_capital（单次下单金额）
  - 两者均写入账本，executor 从账本直接读取，无需再看白名单

职责三：ATR 动态参数核算与状态机重置
  - 拉取近 30 根日线，计算 MA20 和 ATR(20)
  - 若标的处于空仓状态且满足踏空/首次建网条件，重置网格参数

⚠️ 重要安全规范：
  - 本模块只写账本（t1_grid_ledger.yaml），不下任何实盘单
  - TOTAL_CAPITAL 是唯一真相，绝不从 yaml 读取
  - 所有从账本读取的数据必须经过 .get() 防御，账本新增标的时字段可能缺失
  - 支持 --dry-run 参数，仅打印变化不写入账本
"""

import os
import sys
import json
import time
import argparse
import warnings
import traceback
from datetime import date, datetime
from copy import deepcopy
from pathlib import Path

import yaml
import numpy as np

warnings.filterwarnings("ignore")

try:
    from xtquant import xtdata, xtconstant   # xtconstant 必须同行导入（quant-safe-patterns）
    _HAS_XTDATA = True
except ImportError:
    _HAS_XTDATA = False
    print("⚠️ [t1_master] xtquant 未安装，ATR 计算将跳过（仅本地账本解冻有效）。")

# =====================================================
# ★ 资金顶层常量（唯一真相，不得从外部 yaml 读取）★
# =====================================================
TOTAL_CAPITAL  = 160000   # 总资金（元）
MAX_GRIDS_DOWN = 4        # 最大向下格数（须与 executor 保持一致）

# =====================================================
# 路径配置（从项目根目录相对引用，与其他策略保持一致）
# =====================================================
_DIR              = Path(__file__).parent.resolve()
WHITELIST_FILE    = Path(r"Z:\QuantpC_Workspace\Data\t1_grid_whitelist.yaml")
LEDGER_FILE       = _DIR / ".state" / "t1_grid_ledger.yaml"
STATUS_FILE       = _DIR / ".state" / "autopilot_status.json"
ACTION_LOG_DIR    = _DIR / ".state" / "action_logs"
LOG_DIR           = _DIR / "logs"

PROBE_NAME        = "t1_master"

# =====================================================
# 账本空白模板（新标的首次写入时的初始结构）
# =====================================================
_LEDGER_DEFAULTS = {
    "base_price":        0.0,
    "dynamic_step":      0.012,
    "atr_value":         0.0,
    "current_grid":      0,
    "available_shares":  0,
    "locked_shares":     0,
    "idle_days":         0,
    "cooldown_until":    "2000-01-01",
    "last_settle_date":  "2000-01-01",
    # 资金参数：由 master 每日强制覆写
    "symbol_max_limit":  round(TOTAL_CAPITAL / 8, 2),   # 占位默认，每日运行后校正
    "per_grid_capital":  round(TOTAL_CAPITAL / 8 / MAX_GRIDS_DOWN, 2),
}

# =====================================================
# 工具函数
# =====================================================

def _today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def _log(msg: str):
    """统一标准输出日志，前缀时间戳"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_ledger() -> dict:
    """读取账本，若文件不存在或空则返回空 dict"""
    if not LEDGER_FILE.exists():
        _log(f"⚠️ 账本未找到，新建空账本: {LEDGER_FILE}")
        return {}
    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        _log(f"❌ 账本读取失败: {e}")
        return {}


def save_ledger(ledger: dict, dry_run: bool = False):
    """原子写入账本（全量覆盖，防止撕裂）"""
    if dry_run:
        _log("🔍 [DRY-RUN] 账本不写入，仅展示变化：")
        print(yaml.dump(ledger, allow_unicode=True, default_flow_style=False, sort_keys=False))
        return
    LEDGER_FILE.parent.mkdir(exist_ok=True)
    tmp = LEDGER_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(ledger, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        tmp.replace(LEDGER_FILE)
        _log(f"💾 账本已更新: {LEDGER_FILE}")
    except Exception as e:
        _log(f"❌ 账本写入失败: {e}")
        if tmp.exists():
            tmp.unlink()


def load_whitelist() -> list:
    """
    读取白名单，仅返回标的列表（list of dict）。
    ⚠️ 不再从 yaml 读取 total_capital，资金由 TOTAL_CAPITAL 常量控制。

    格式兼容：
      - 顶级键 'etf_list' 或 'whitelist' 均可
      - code 支持带后缀（'512890.SH'）或纯数字（'517900'）自动补全后缀
    """
    if not WHITELIST_FILE.exists():
        _log(f"❌ 白名单未找到: {WHITELIST_FILE}")
        return []
    try:
        with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # 兼容 etf_list / whitelist 两种顶级键名
        items = data.get("etf_list") or data.get("whitelist") or []
        if not items:
            _log("⚠️ 白名单为空，无标的需要处理。")
            return []
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", "")).strip()
            if not code:
                continue
            # 自动补全交易所后缀
            if "." not in code:
                code = _infer_exchange_suffix(code)
            result.append({
                "code": code,
                "name": item.get("name", code),
            })
        _log(f"✅ 白名单加载完毕，共 {len(result)} 只标的")
        return result
    except Exception as e:
        _log(f"❌ 白名单读取失败: {e}")
        return []


def _infer_exchange_suffix(code: str) -> str:
    """
    根据代码前缀规则自动判断交易所后缀。
    沪市（SH）：5、6 开头
    深市（SZ）：0、1、2、3 开头
    """
    prefix = code[:1] if code else "0"
    if prefix in ("5", "6"):
        return f"{code}.SH"
    return f"{code}.SZ"


def write_probe(status: str, note: str = ""):
    """向 autopilot_status.json 写入探针（与其他策略格式对齐）"""
    try:
        all_status: dict = {}
        if STATUS_FILE.exists():
            try:
                with open(STATUS_FILE, "r", encoding="utf-8") as f:
                    all_status = json.load(f)
            except Exception:
                all_status = {}
        all_status[PROBE_NAME] = {
            "strategy_name": "T1网格参数核算",
            "script":        "t1_master.py",
            "pid":           os.getpid(),
            "status":        status,
            "fired_at":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "description":   note or f"状态: {status}",
        }
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(all_status, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 探针写入失败绝不中断主逻辑


def append_action_log(action: str, target: str, reason: str, extra: dict | None = None):
    """结构化交易动作日志，与 quant_logger.record_action 格式对齐"""
    try:
        ACTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = ACTION_LOG_DIR / f"action_{datetime.now().strftime('%Y%m%d')}.jsonl"
        record = {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "strategy":  "T1_Master",
            "action":    action,
            "target":    target,
            "price":     0.0,
            "reason":    reason,
            "extra":     extra or {},
        }
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# =====================================================
# 任务零：资金参数写入（每日强制覆写账本）
# =====================================================

def task_write_capital_params(
    ledger: dict,
    whitelist: list,
    symbol_max_limit: float,
    per_grid_capital: float,
) -> dict:
    """
    任务零：将当日计算的资金参数强制写入每只标的的账本条目。

    executor 直接从账本读取 symbol_max_limit 和 per_grid_capital，
    无需再触及白名单，确保盘前参数和盘中执行严格一致。
    """
    ledger = deepcopy(ledger)
    cnt = 0
    for item in whitelist:
        code = item.get("code", "")
        if not code:
            continue
        ledger = _ensure_entry(ledger, code)
        ledger[code]["symbol_max_limit"] = round(symbol_max_limit, 2)
        ledger[code]["per_grid_capital"] = round(per_grid_capital, 2)
        # 清除旧字段（兼容历史账本）
        ledger[code].pop("slot_capital", None)
        cnt += 1
    _log(
        f"💰 资金参数写入 {cnt} 只标的 | "
        f"symbol_max_limit={symbol_max_limit:.2f} | "
        f"per_grid_capital={per_grid_capital:.2f}"
    )
    return ledger


# =====================================================
# 任务一：T+1 跨日物理解冻
# =====================================================

def task_t1_unlock(ledger: dict) -> dict:
    """
    跨日物理解冻：
      若 last_settle_date != today，将 locked_shares 累加到 available_shares，
      locked_shares 归零，更新 last_settle_date。
      若解冻后 available_shares > 0，idle_days + 1。

    返回更新后的账本（副本，调用方决定是否写入）。
    """
    today = _today_str()
    ledger = deepcopy(ledger)
    total_unlocked = 0

    for code, rec in ledger.items():
        if not isinstance(rec, dict):
            continue

        settle_date    = rec.get("last_settle_date", "2000-01-01")
        locked         = int(rec.get("locked_shares", 0))
        available      = int(rec.get("available_shares", 0))

        if str(settle_date) == today:
            _log(f"  [{code}] 今日已结算，跳过解冻。")
            continue

        # 执行解冻
        if locked > 0:
            rec["available_shares"] = available + locked
            rec["locked_shares"]    = 0
            total_unlocked          += locked
            _log(f"  [{code}] ✅ T+1 解冻: +{locked}股 → available={rec['available_shares']}")
            append_action_log("解冻", code, f"T+1解冻 locked={locked}", {"locked_before": locked})
        else:
            _log(f"  [{code}] 无冻结持仓，跳过解冻。locked=0")

        # 更新结算日期
        rec["last_settle_date"] = today

        # 闲置天数递增（解冻后仍有持仓但无新交易）
        if rec.get("available_shares", 0) > 0:
            rec["idle_days"] = int(rec.get("idle_days", 0)) + 1
            _log(f"  [{code}] idle_days 累加 → {rec['idle_days']}")

        ledger[code] = rec

    _log(f"📦 解冻汇总：共解冻 {total_unlocked} 股")
    return ledger


# =====================================================
# 任务二：ATR 动态参数核算与状态机重置
# =====================================================

def _compute_ma20_atr20(code: str) -> tuple[float, float, float]:
    """
    拉取近 30 根日线，计算 MA20、ATR(20) 绝对值、当前最新价。
    返回 (ma20, atr_abs, current_price) 或 (0, 0, 0) 表示失败。

    ⚠️ quant-safe-patterns: get_market_data_ex 字典访问必须先判断 key 存在
    """
    if not _HAS_XTDATA:
        _log(f"  [{code}] xtdata 不可用，跳过 ATR 计算。")
        return 0.0, 0.0, 0.0

    try:
        # 先下载增量数据，确保本地缓存最新
        xtdata.download_history_data(code, period="1d", incrementally=True)

        raw = xtdata.get_market_data_ex(
            field_list=["close", "high", "low"],
            stock_list=[code],
            period="1d",
            count=30,
        )

        # 防御性访问（quant-safe-patterns §2.1）
        if code not in raw or raw[code].empty:
            _log(f"  [{code}] ⚠️ 无历史日线数据，跳过。")
            return 0.0, 0.0, 0.0

        df = raw[code].copy()
        # 字段归一化（不同版本 QMT 返回字段名可能不同）
        df.columns = [c.lower() for c in df.columns]

        if "close" not in df.columns or "high" not in df.columns or "low" not in df.columns:
            _log(f"  [{code}] ⚠️ 日线数据缺少 close/high/low 字段。")
            return 0.0, 0.0, 0.0

        df = df.dropna(subset=["close", "high", "low"])
        if len(df) < 20:
            _log(f"  [{code}] ⚠️ 有效日线 {len(df)} 根 < 20，跳过 ATR 计算。")
            return 0.0, 0.0, 0.0

        close  = df["close"]
        high   = df["high"]
        low    = df["low"]

        # MA20
        ma20 = float(close.rolling(20).mean().iloc[-1])

        # ATR(20) — True Range：max(H-L, |H-prev_C|, |L-prev_C|)
        prev_close = close.shift(1)
        tr = np.maximum.reduce([
            high.values - low.values,
            np.abs(high.values - prev_close.values),
            np.abs(low.values  - prev_close.values),
        ])
        # 取最后 20 根
        atr_abs       = float(np.nanmean(tr[-20:]))
        current_price = float(close.iloc[-1])

        return ma20, atr_abs, current_price

    except Exception as e:
        _log(f"  [{code}] ❌ ATR 计算异常: {e}")
        return 0.0, 0.0, 0.0


def _ensure_entry(ledger: dict, code: str) -> dict:
    """确保账本中有该标的的条目（首次出现时以默认值初始化）"""
    if code not in ledger or not isinstance(ledger.get(code), dict):
        ledger[code] = deepcopy(_LEDGER_DEFAULTS)
        ledger[code]["last_settle_date"] = _today_str()
        _log(f"  [{code}] 账本新增标的，初始化默认字段。")
    else:
        # 补齐新字段（兼容旧账本），资金字段会在 task_write_capital_params 中强制覆写
        for k, v in _LEDGER_DEFAULTS.items():
            ledger[code].setdefault(k, v)
    return ledger


def task_atr_reset(ledger: dict, whitelist: list) -> dict:
    """
    遍历白名单，核算 ATR 参数并按需重置状态机。

    状态机重置条件（所有条件均满足才重置）：
      1. 空仓：available_shares == 0 AND locked_shares == 0
      2. 踏空突破（current_price > base_price * (1 + dynamic_step)) 或 首次挂网（base_price == 0）

    重置动作：
      base_price     = MA20
      dynamic_step   = max(0.8 * ATR_pct, 0.008)   保底 0.8%
      atr_value      = ATR 绝对值（元）
      current_grid   = 0
      idle_days      = 0
    """
    ledger = deepcopy(ledger)

    for item in whitelist:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        if not code:
            continue

        _log(f"\n[{code}] 开始参数核算...")
        ledger = _ensure_entry(ledger, code)
        rec    = ledger[code]

        base_price  = float(rec.get("base_price", 0.0))
        dyn_step    = float(rec.get("dynamic_step", 0.012))
        available   = int(rec.get("available_shares", 0))
        locked      = int(rec.get("locked_shares", 0))

        # 判断是否空仓
        is_flat = (available == 0) and (locked == 0)

        # 拉取行情
        ma20, atr_abs, current_price = _compute_ma20_atr20(code)
        if current_price <= 0:
            _log(f"  [{code}] 行情数据不可用，跳过参数重置。")
            ledger[code] = rec
            continue

        _log(
            f"  [{code}] MA20={ma20:.4f}  ATR20={atr_abs:.4f}  "
            f"current={current_price:.4f}  base={base_price:.4f}  is_flat={is_flat}"
        )

        # 判断是否触发重置
        is_first_grid  = (base_price == 0.0)
        is_breakout    = (base_price > 0) and (current_price > base_price * (1.0 + dyn_step))

        if is_flat and (is_first_grid or is_breakout):
            reason = "首次建网" if is_first_grid else "踏空突破，上移网格"
            new_step = max(0.8 * (atr_abs / current_price), 0.008)
            new_step = round(new_step, 6)

            _log(
                f"  [{code}] 🔄 状态机重置 ({reason}): "
                f"base={ma20:.4f}  step={new_step:.4f}  atr={atr_abs:.4f}"
            )

            rec["base_price"]    = round(ma20, 6)
            rec["dynamic_step"]  = new_step
            rec["atr_value"]     = round(atr_abs, 6)
            rec["current_grid"]  = 0
            rec["idle_days"]     = 0

            append_action_log(
                "参数重置", code, reason,
                extra={
                    "new_base_price":   rec["base_price"],
                    "new_dynamic_step": new_step,
                    "new_atr_value":    atr_abs,
                    "current_price":    current_price,
                    "ma20":             ma20,
                }
            )
        else:
            skip_reason = []
            if not is_flat:
                skip_reason.append(f"持仓中(avail={available} locked={locked})")
            if not (is_first_grid or is_breakout):
                skip_reason.append("无踏空突破")
            _log(f"  [{code}] ⏭  跳过重置 — {' / '.join(skip_reason) or '无需重置'}")

            # 非首次挂网时也更新 atr_value，确保 hard_stop_price 始终基于最新 ATR
            if atr_abs > 0 and base_price > 0:
                rec["atr_value"] = round(atr_abs, 6)
                _log(f"  [{code}] 📈 更新 atr_value → {rec['atr_value']:.4f}（base_price 不变）")

        ledger[code] = rec

    return ledger


# =====================================================
# 主入口
# =====================================================

def main():
    parser = argparse.ArgumentParser(description="T1 Master — 盘前账本解冻与参数核算")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印账本变化，不写入磁盘（测试用）",
    )
    args = parser.parse_args()
    dry_run = args.dry_run

    _log("=" * 65)
    _log("🚀 T1 Master 启动 — T+1 网格盘前调度模块")
    if dry_run:
        _log("🔍 [DRY-RUN 模式] 账本不写入磁盘")
    _log("=" * 65)

    write_probe("running", "T+1 解冻与 ATR 参数核算中...")

    try:
        # ── 1. 加载账本和白名单 ──────────────────────────────────────
        ledger    = load_ledger()
        whitelist = load_whitelist()
        n         = len(whitelist)

        if n == 0:
            _log("❌ 白名单为空，中止运行。")
            write_probe("error", "白名单为空")
            sys.exit(1)

        # ── 2. 资金核算（顶层常量，不得从 yaml 读取）────────────────
        symbol_max_limit = round(TOTAL_CAPITAL / n, 2)
        per_grid_capital = round(symbol_max_limit / MAX_GRIDS_DOWN, 2)

        _log(
            f"\n💰 资金分配: TOTAL={TOTAL_CAPITAL:.0f}元 ÷ {n}只"
            f" = symbol_max={symbol_max_limit:.2f}元/只"
            f" ÷ {MAX_GRIDS_DOWN}格"
            f" = per_grid={per_grid_capital:.2f}元/格"
        )
        _log(f"📊 当前账本标的数: {len(ledger)}  |  白名单标的数: {n}")
        _log(f"📅 今日结算日: {_today_str()}")

        # ── 3. 任务零：资金参数强制写入账本 ─────────────────────────
        _log("\n" + "─" * 55)
        _log("💵 【任务零】资金参数强制写入账本")
        _log("─" * 55)
        ledger = task_write_capital_params(ledger, whitelist, symbol_max_limit, per_grid_capital)

        # ── 4. 任务一：T+1 跨日物理解冻 ──────────────────────────────
        _log("\n" + "─" * 55)
        _log("📦 【任务一】T+1 跨日物理解冻")
        _log("─" * 55)
        ledger = task_t1_unlock(ledger)

        # ── 5. 任务二：ATR 参数核算与状态机重置 ──────────────────────
        _log("\n" + "─" * 55)
        _log("📊 【任务二】ATR 动态参数核算与状态机重置")
        _log("─" * 55)
        ledger = task_atr_reset(ledger, whitelist)

        # ── 6. 保存账本 ──────────────────────────────────────────────
        _log("\n" + "─" * 55)
        save_ledger(ledger, dry_run=dry_run)

        # ── 7. 打印账本摘要 ──────────────────────────────────────────
        _log("\n📑 【账本最终状态】")
        for code, rec in ledger.items():
            if not isinstance(rec, dict):
                continue
            _log(
                f"  {code} | max={rec.get('symbol_max_limit', 0):.0f}元"
                f" per_grid={rec.get('per_grid_capital', 0):.0f}元"
                f" base={rec.get('base_price', 0):.4f}"
                f" step={rec.get('dynamic_step', 0):.4f}"
                f" grid={rec.get('current_grid', 0)}"
                f" avail={rec.get('available_shares', 0)}"
                f" locked={rec.get('locked_shares', 0)}"
                f" idle={rec.get('idle_days', 0)}"
                f" cool={rec.get('cooldown_until', 'N/A')}"
            )

        write_probe(
            "done",
            f"解冻与 ATR 核算完成 | max={symbol_max_limit:.0f}元 per_grid={per_grid_capital:.0f}元×{n}只"
        )
        _log("\n🏁 T1 Master 完成。")

    except Exception as e:
        _log(f"🔥 T1 Master 崩溃: {e}")
        traceback.print_exc()
        write_probe("error", f"崩溃: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
