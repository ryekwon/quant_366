"""
patch_telemetry_mfe.py
═══════════════════════════════════════════════════════════════
一次性补丁：回填 sniper_telemetry.csv 中 mfe_pct=0.0 AND mae_pct=0.0 的行。

使用方法：
    .venv\Scripts\python.exe tools\patch_telemetry_mfe.py

前置条件：
    - MiniQMT 客户端必须处于运行状态（xtdata 需要连接）
    - 脚本从 QMT 本地缓存拉取 1m/1d K 线，无需实时行情
"""

import csv
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# 🛡️ 解决 Windows 控制台打印 Emoji 导致的 UnicodeEncodeError
if sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


# ─── 路径配置 ─────────────────────────────────────────────────────────────────
_ROOT      = Path(__file__).resolve().parent.parent
TELEMETRY  = _ROOT / ".state" / "sniper_telemetry.csv"
BACKUP     = TELEMETRY.with_suffix(".csv.bak")

# ─── 字段定义（与 sniper_exit_guard.py 保持一致）───────────────────────────────
FIELDS = [
    "exit_ts", "code", "name", "entry_date", "entry_price",
    "exit_price", "exit_reason", "pnl_pct",
    "intraday_high", "mfe_pct", "intraday_low", "mae_pct",
]


def _parse_qmt_index_naive(idx, period: str):
    """
    QMT index 格式因 period 而异，统一输出 tz-naive DatetimeIndex：
      1m  → YYYYMMDDHHMMSS 整数 → pd.to_datetime(..., format="%Y%m%d%H%M%S")
      1d  → epoch-ms 整数       → to_datetime(unit="ms") + tz剥离
    永远不做 tz_localize / tz_convert，保持朴素时间戳，切片两端对齐。
    """
    import pandas as pd
    _sample = int(idx[0])
    if 20_000_101_000000 <= _sample <= 21_000_101_000000:
        # 1m: YYYYMMDDHHMMSS → naive datetime（不加时区）
        return pd.to_datetime(idx.astype(str), format="%Y%m%d%H%M%S")
    else:
        # 1d: epoch-ms → UTC → Asia/Shanghai → 剥离时区 → naive
        return (
            pd.to_datetime(idx, unit="ms")
            .tz_localize("UTC")
            .tz_convert("Asia/Shanghai")
            .tz_localize(None)          # 剥离时区，保持本地朴素时间
        )


def _recalc_mfe_mae(code: str, entry_date: str, entry_price: float,
                    entry_ts: str = "") -> tuple[float, float, float, float]:
    """
    重算 MFE / MAE。
    优先使用 1m 分钟线（subscribe → get_market_data_ex），
    空时降级 1d 日线。
    返回 (intraday_high, mfe_pct, intraday_low, mae_pct)
    """
    import pandas as pd
    from xtquant import xtdata

    # 切割基准：全程 tz-naive
    cutoff_dt = None
    try:
        if entry_ts:
            cutoff_dt = pd.to_datetime(entry_ts)
        else:
            cutoff_dt = pd.to_datetime(f"{entry_date} 09:25:00")
    except Exception as _e:
        print(f"  ⚠️  {code} cutoff 解析失败: {_e}")

    # 主动订阅 1m（同 exit_guard 修复逻辑）
    try:
        xtdata.subscribe_quote(code, period="1m", count=-1)
    except Exception:
        pass

    # ── 尝试 1m ──────────────────────────────────────────────────────────────
    try:
        raw = xtdata.get_market_data_ex(
            field_list=["high", "low"], stock_list=[code],
            period="1m", count=1000, dividend_type="front",
        )
        df = raw.get(code)
        if df is not None and not df.empty:
            df.index = _parse_qmt_index_naive(df.index, period="1m")
            df_cut = df[df.index > cutoff_dt] if cutoff_dt is not None else df
            if not df_cut.empty:
                hi = float(df_cut["high"].max())
                lo = float(df_cut["low"].min())
                mfe = round((hi / entry_price - 1) * 100, 4) if entry_price > 0 else 0.0
                mae = round((lo / entry_price - 1) * 100, 4) if entry_price > 0 else 0.0
                print(f"  ✅ [1m] {code} 高={hi:.3f} 低={lo:.3f} "
                      f"Bar数={len(df_cut)} 基准={cutoff_dt}")
                return hi, mfe, lo, mae
            else:
                print(f"  ⚠️  {code} 1m 切割后无 Bar（基准={cutoff_dt}），降级日线")
        else:
            print(f"  ⚠️  {code} 1m 返回空，降级日线")
    except Exception as _e:
        import traceback
        print(f"  ⚠️  {code} 1m 拉取异常，降级日线:")
        print(traceback.format_exc())

    # ── 降级 1d ──────────────────────────────────────────────────────────────
    try:
        raw_1d = xtdata.get_market_data_ex(
            field_list=["high", "low"], stock_list=[code],
            period="1d", count=10, dividend_type="front",
        )
        df_1d = raw_1d.get(code)
        if df_1d is not None and not df_1d.empty:
            df_1d.index = _parse_qmt_index_naive(df_1d.index, period="1d")
            entry_day = pd.to_datetime(f"{entry_date} 00:00:00")
            df_1d_cut = df_1d[df_1d.index >= entry_day]
            if not df_1d_cut.empty:
                hi = float(df_1d_cut["high"].max())
                lo = float(df_1d_cut["low"].min())
                mfe = round((hi / entry_price - 1) * 100, 4) if entry_price > 0 else 0.0
                mae = round((lo / entry_price - 1) * 100, 4) if entry_price > 0 else 0.0
                print(f"  ✅ [1d] {code} 高={hi:.3f} 低={lo:.3f} Bar数={len(df_1d_cut)}")
                return hi, mfe, lo, mae
    except Exception as _e:
        print(f"  ⚠️  {code} 日线也失败: {_e}")

    print(f"  ❌ {code} 两级均失败，保留 0.0")
    return 0.0, 0.0, 0.0, 0.0


def main():
    if not TELEMETRY.exists():
        print(f"❌ 找不到 CSV 文件: {TELEMETRY}")
        sys.exit(1)

    # 备份原文件
    shutil.copy2(TELEMETRY, BACKUP)
    print(f"📦 已备份原文件 → {BACKUP.name}")

    # 读入全部记录
    with open(TELEMETRY, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    need_fix = [r for r in rows
                if float(r.get("mfe_pct", 1)) == 0.0
                and float(r.get("mae_pct", 1)) == 0.0]

    if not need_fix:
        print("✅ 没有需要修复的行，退出。")
        return

    print(f"\n🔍 发现 {len(need_fix)} 行 MFE/MAE=0.0，开始重算...\n")

    fixed = 0
    for row in rows:
        if float(row.get("mfe_pct", 1)) != 0.0 or float(row.get("mae_pct", 1)) != 0.0:
            continue  # 已有数据，跳过

        code        = row["code"]
        entry_date  = row["entry_date"]
        entry_price = float(row["entry_price"])
        # entry_ts 可能不存在于旧版 CSV，fallback 到空字符串
        entry_ts    = row.get("entry_ts", "")

        print(f"→ 重算 [{code}] {row.get('name','')} | 入场日={entry_date} | 入场价={entry_price}")
        hi, mfe, lo, mae = _recalc_mfe_mae(code, entry_date, entry_price, entry_ts)

        row["intraday_high"] = round(hi, 4)
        row["mfe_pct"]       = mfe
        row["intraday_low"]  = round(lo, 4)
        row["mae_pct"]       = mae
        fixed += 1

    # 回写 CSV
    with open(TELEMETRY, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n🎉 完成！共修复 {fixed} 行，已写回 {TELEMETRY.name}")
    print(f"   原始备份保留在 {BACKUP.name}")


if __name__ == "__main__":
    main()
