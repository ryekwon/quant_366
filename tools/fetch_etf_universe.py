# -*- coding: utf-8 -*-
"""
tools/fetch_etf_universe.py
============================
职责：从 QMT 本地板块数据获取全市场 ETF 代码列表，
      输出格式与历史数据（parquet/xtdata）完全对齐。

代码格式规范（与整个项目历史数据一致）：
  沪市 ETF → XXXXXX.SH   e.g. 510300.SH  518880.SH
  深市 ETF → XXXXXX.SZ   e.g. 159915.SZ  159740.SZ

ETF 代码前缀规律（A 股监管规则）：
  沪市（.SH）：51x（普通 ETF）| 52x（科创板/QDII）| 58x（科创板宽基）
  深市（.SZ）：15x（创业板/跨境/债券 ETF）

调用示例（独立函数，随处复用）：
  from tools.fetch_etf_universe import get_all_etf_codes, get_etf_info_df

  # 仅拿代码列表
  codes = get_all_etf_codes()               # → ['510300.SH', '159915.SZ', ...]

  # 拿完整信息 DataFrame（含名称/交易制度/大市场/前缀类别）
  df = get_etf_info_df()                    # → pd.DataFrame

  # 转换为 parquet 文件路径（与 qmt_daily_sync / momentum_master 格式一致）
  from tools.fetch_etf_universe import code_to_parquet_path
  path = code_to_parquet_path('510300.SH')  # → .../Market_Daily/510300_SH.parquet

依赖：miniQMT 客户端必须在线（xtdata 板块接口需要 QMT 连接）
"""

import os
import sys
import json
import time
import datetime
import threading
from typing import List, Optional

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

# ── 路径常量（与项目其他脚本保持一致）──────────────────────────────────────────
_THIS_DIR    = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_THIS_DIR)        # 项目根目录（tools 的上一级）
# 日线 Parquet 数据目录（与 qmt_daily_sync / momentum_master 路径规则一致）
MARKET_DAILY_DIR = os.path.join(os.path.dirname(_PROJECT_DIR), "Data", "Market_Daily")

# ── ETF 代码前缀白名单（覆盖沪深全系列 ETF）───────────────────────────────────
# 沪市: 51x(普通ETF) | 52x(科创/QDII) | 58x(科创宽基)
# 深市: 15x(含创业板/跨境/债券ETF)
_ETF_SH_PREFIXES = ("51", "52", "58")
_ETF_SZ_PREFIXES = ("15",)
_ETF_ALL_PREFIXES = _ETF_SH_PREFIXES + _ETF_SZ_PREFIXES   # 统一多交所用

# ── 板块名候选（QMT 不同版本使用不同板块名，按优先级尝试）──────────────────────
_SECTOR_CANDIDATES = [
    "场内基金",   # 覆盖最全：含 ETF / LOF / 分级（再用前缀过滤）
    "ETF",
    "基金",
    "上海ETF",    # 部分版本
]

# ── T+0 白名单文件路径（quant-v4-patterns §12.5 铁律：永远通过读取 CSV，永不用代码前缀推导）──
# 与 momentum_master.py / t0_multigrid_executor.py 使用同一物理文件
T0_POOL_CSV = os.path.join(_PROJECT_DIR, ".state", "t0_absolute_pool.csv")


# ==============================================================================
# 📐 工具函数 1：代码格式转换（与历史数据格式完全一致）
# ==============================================================================

def code_to_parquet_path(code: str,
                          market_daily_dir: str = MARKET_DAILY_DIR) -> str:
    """
    将 QMT 标准代码（如 510300.SH）转换为本地 parquet 文件路径。

    文件命名规则（与 qmt_daily_sync.py / momentum_master.py 完全一致）：
      {6位代码}_{SH|SZ}.parquet
      e.g. 510300.SH → .../Market_Daily/510300_SH.parquet

    参数
    ----
    code              : QMT 格式代码，e.g. "510300.SH"
    market_daily_dir  : parquet 存储目录（默认对齐项目 Data/Market_Daily）

    返回
    ----
    str : 完整 parquet 文件路径
    """
    parts = code.split(".")
    if len(parts) == 2:
        fname = f"{parts[0]}_{parts[1]}.parquet"
    else:
        fname = f"{code}.parquet"   # fallback
    return os.path.join(market_daily_dir, fname)


import csv as _csv

def load_t0_pool(csv_path: Optional[str] = None) -> frozenset:
    """
    从 t0_absolute_pool.csv 加载 T+0 交易白名单。

    这是项目级判定 T+0 的唯一权威来源（quant-v4-patterns §12.5 铁律）：
      「永远通过读取 t0_absolute_pool.csv，永不用代码前缀推导」

    参数
    ----
    csv_path : CSV 路径（None = 使用项目默认路径 T0_POOL_CSV）

    返回
    ----
    frozenset : 已离线的 T+0 代码集合（如 {'510300.SH', '159915.SZ', ...}）
               如果 CSV 不存在或解析失败，返回空集 frozenset()
               → 下游 classify_trade_rule 将全部按 T+1 保守处理
    """
    path = csv_path or T0_POOL_CSV
    t0_set: set = set()
    if not os.path.exists(path):
        print(f"  ⚠️  T+0 白名单文件不存在: {path}")
        print(f"     所有标的按 T+1 保守处理")
        return frozenset()
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = _csv.reader(f)
            for row in reader:
                if not row:
                    continue
                code = row[0].strip()
                # 使用与 momentum_master._load_t0_set 相同的校验逻辑：
                # 长度=9（XXXXXX.SH）且包含点号 → 是有效代码
                if len(code) == 9 and "." in code:
                    t0_set.add(code)
        print(f"  ✅  T+0 白名单加载：{len(t0_set)} 只可 T+0 交易 | 来源: {os.path.basename(path)}")
    except Exception as e:
        print(f"  ⚠️  T+0 白名单读取失败: {e}，全按 T+1 保守处理")
    return frozenset(t0_set)


def classify_trade_rule(code: str, t0_set: Optional[frozenset] = None) -> str:
    """
    判定 ETF 交易制度：T+0 / T+1。

    【quant-v4-patterns §12.5 铁律】
      T+0 判定的唯一权威来源是 t0_absolute_pool.csv，
      永不使用代码前缀推导。

    参数
    ----
    code   : QMT 标准格式代码 e.g. "510300.SH"
    t0_set : 预加载的 frozenset（批量处理时传入避免重复 IO）
             为 None 时自动对 CSV 执行一次惰性加载

    返回
    ----
    str : "T+0" 或 "T+1"
    """
    if t0_set is None:
        t0_set = load_t0_pool()
    return "T+0" if code in t0_set else "T+1"


# ==============================================================================
# 📡 核心函数：全市场 ETF 代码获取
# ==============================================================================

def get_all_etf_codes(
    timeout_sec: float = 10.0,
    verbose: bool = True,
) -> List[str]:
    """
    获取 A 股全市场 ETF 代码列表（QMT 标准格式：XXXXXX.SH / XXXXXX.SZ）。

    策略（三层防御，与 refine_core_universe.py 对齐）：
      层一：尝试 get_stock_list_in_sector("场内基金") 等板块接口
           → 覆盖最全，含 52x 科创板系列
      层二：回退到 SH/SZ 全量拉取 + 前缀过滤
           → 兼容不支持中文板块名的 miniQMT 旧版本
      层三：抛出 RuntimeError（miniQMT 离线）

    参数
    ----
    timeout_sec : 单次板块查询超时（秒）
    verbose     : 是否打印调试信息

    返回
    ----
    List[str] : QMT 标准格式代码列表，已去重，按代码字符串排序
                e.g. ["159915.SZ", "510300.SH", "513500.SH", ...]

    异常
    ----
    ImportError  : 未安装 xtquant（miniQMT 未安装）
    RuntimeError : QMT 接口返回空数据（QMT 未启动或未登录）
    """
    from xtquant import xtdata

    def _log(msg):
        if verbose:
            print(msg)

    # ── 层一：板块接口（优先，覆盖最全）─────────────────────────────────────
    for sector_name in _SECTOR_CANDIDATES:
        try:
            result_box: List = []
            done = threading.Event()

            def _query(sn=sector_name, rb=result_box, ev=done):
                try:
                    codes = xtdata.get_stock_list_in_sector(sn) or []
                    rb.extend(codes)
                except Exception as e:
                    _log(f"  ⚠️  板块 [{sn}] 查询异常: {e}")
                finally:
                    ev.set()

            t = threading.Thread(target=_query, daemon=True)
            t.start()
            done.wait(timeout=timeout_sec)

            if not result_box:
                _log(f"  🔄  板块 [{sector_name}] 无数据，尝试下一候选...")
                continue

            # 用前缀白名单过滤出纯 ETF
            etf_list = [
                code for code in result_box
                if any(code.startswith(p) for p in _ETF_ALL_PREFIXES)
            ]
            if etf_list:
                etf_list_sorted = sorted(set(etf_list))
                _log(
                    f"  ✅  [{sector_name}] → 原始 {len(result_box)} 只"
                    f" → ETF 过滤后 {len(etf_list_sorted)} 只"
                )
                return etf_list_sorted

        except Exception as e:
            _log(f"  ⚠️  板块接口 [{sector_name}] 异常: {e}")

    # ── 层二：按交所全量拉取（兼容旧版 miniQMT）─────────────────────────────
    _log("  🔄  回退到 SH/SZ 全量拉取方案...")
    sh_list: List[str] = []
    sz_list: List[str] = []

    def _fetch_sh():
        try:
            sh_list.extend(xtdata.get_stock_list_in_sector("SH") or [])
        except Exception as e:
            _log(f"  ⚠️  SH 板块查询异常: {e}")

    def _fetch_sz():
        try:
            sz_list.extend(xtdata.get_stock_list_in_sector("SZ") or [])
        except Exception as e:
            _log(f"  ⚠️  SZ 板块查询异常: {e}")

    t1 = threading.Thread(target=_fetch_sh, daemon=True)
    t2 = threading.Thread(target=_fetch_sz, daemon=True)
    t1.start(); t2.start()
    t1.join(timeout=timeout_sec); t2.join(timeout=timeout_sec)

    sh_etfs = [c for c in sh_list if c.startswith(_ETF_SH_PREFIXES)]
    sz_etfs = [c for c in sz_list if c.startswith(_ETF_SZ_PREFIXES)]
    etf_list = sorted(set(sh_etfs + sz_etfs))

    if etf_list:
        _log(
            f"  ✅  SH/SZ 全量方案 → "
            f"沪市 {len(sh_etfs)} 只 + 深市 {len(sz_etfs)} 只 = {len(etf_list)} 只"
        )
        return etf_list

    # ── 层三：全部方案失败 ───────────────────────────────────────────────────
    raise RuntimeError(
        "无法获取 ETF 代码列表！\n"
        "  可能原因：1) miniQMT 未启动/未登录  "
        "2) xtdata 板块数据未下载（运行 xtdata.download_sector_data()）"
    )


# ==============================================================================
# 📊 扩展函数：获取带元数据的完整 DataFrame
# ==============================================================================

def get_etf_info_df(
    codes: Optional[List[str]] = None,
    fetch_names: bool = True,
    timeout_sec: float = 10.0,
    verbose: bool = True,
) -> "pd.DataFrame":
    """
    获取全市场 ETF 的完整元数据 DataFrame。

    参数
    ----
    codes       : 指定代码列表（None = 自动获取全市场）
    fetch_names : 是否同步获取 ETF 中文名称（需要 QMT 连接，稍慢）
    timeout_sec : get_all_etf_codes 超时（秒）
    verbose     : 是否打印进度

    返回
    ----
    pd.DataFrame，列：
      code         : QMT 标准代码 e.g. "510300.SH"
      exchange     : 交所 "SH" / "SZ"
      prefix       : 代码前3位 e.g. "510"
      trade_rule   : "T+0" / "T+1"（规则推断）
      name         : ETF 中文简称（fetch_names=True 时有值）
      parquet_path : 对应本地 parquet 文件路径（与历史数据格式一致）
    """
    if not _HAS_PANDAS:
        raise ImportError("get_etf_info_df 需要 pandas，请 pip install pandas")

    if codes is None:
        codes = get_all_etf_codes(timeout_sec=timeout_sec, verbose=verbose)

    # 一次性加载 T+0 白名单（批量处理避免每只重复 IO）
    t0_set = load_t0_pool()

    rows = []
    for code in codes:
        parts = code.split(".")
        num   = parts[0] if parts else code
        exch  = parts[1] if len(parts) > 1 else ""
        rows.append({
            "code":         code,
            "exchange":     exch,
            "prefix":       num[:3],
            "trade_rule":   classify_trade_rule(code, t0_set=t0_set),  # ✅ 对位 CSV
            "name":         "",
            "parquet_path": code_to_parquet_path(code),
        })

    df = pd.DataFrame(rows)

    # ── 可选：批量拉取 ETF 中文名称 ────────────────────────────────────────
    if fetch_names and not df.empty:
        try:
            from xtquant import xtdata
            if verbose:
                print(f"  📋  正在获取 {len(codes)} 只 ETF 中文名称...")

            name_map = {}
            for code in codes:
                try:
                    detail = xtdata.get_instrument_detail(code)
                    if detail and isinstance(detail, dict):
                        name_map[code] = detail.get("InstrumentName", "")
                    else:
                        name_map[code] = ""
                except Exception:
                    name_map[code] = ""

            df["name"] = df["code"].map(name_map).fillna("")
            if verbose:
                print(f"  ✅  名称获取完成，空名称：{(df['name'] == '').sum()} 只")

        except ImportError:
            if verbose:
                print("  ⚠️  xtquant 未安装，跳过名称获取")
        except Exception as e:
            if verbose:
                print(f"  ⚠️  名称获取异常: {e}")

    return df


# ==============================================================================
# 💾 工具函数：落盘为 JSON（供其他脚本读取）
# ==============================================================================

def save_etf_universe_json(
    output_path: Optional[str] = None,
    codes: Optional[List[str]] = None,
    fetch_names: bool = True,
    verbose: bool = True,
) -> str:
    """
    将全市场 ETF 代码列表落盘为 JSON 文件。

    输出格式（与 oracle_v2_universe.json 等系统文件兼容）：
    {
        "meta": {
            "generated_at": "2025-04-25 14:42:00",
            "total_etfs": 850
        },
        "universe": [
            {"code": "510300.SH", "exchange": "SH", "trade_rule": "T+1",
             "name": "沪深300ETF", "parquet_path": "..."},
            ...
        ]
    }

    参数
    ----
    output_path : 输出 JSON 路径（None = 项目根/.state/etf_full_universe.json）
    codes       : 指定代码列表（None = 自动获取全市场）
    fetch_names : 是否包含 ETF 名称
    verbose     : 是否打印进度

    返回
    ----
    str : 实际写入的文件路径
    """
    if output_path is None:
        output_path = os.path.join(_PROJECT_DIR, ".state", "etf_full_universe.json")

    df = get_etf_info_df(codes=codes, fetch_names=fetch_names, verbose=verbose)

    output = {
        "meta": {
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_etfs":   len(df),
            "source":       "xtdata.get_stock_list_in_sector",
        },
        "universe": df.to_dict(orient="records"),
    }

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    tmp_path = output_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, output_path)   # 原子替换，防写入中断损坏

    if verbose:
        print(f"\n✅ 全市场 ETF 宇宙已写入: {output_path}")
        print(f"   共 {len(df)} 只 ETF")
        t0_cnt = (df["trade_rule"] == "T+0").sum()
        t1_cnt = (df["trade_rule"] == "T+1").sum()
        print(f"   T+0: {t0_cnt} 只 | T+1: {t1_cnt} 只")

    return output_path


# ==============================================================================
# 🔍 工具函数：预检 QMT 是否在线（轻量探针）
# ==============================================================================

def check_qmt_alive(timeout_sec: float = 5.0) -> bool:
    """
    轻量探针：用极小查询检测 miniQMT 是否在线。
    返回 True = 在线可用 / False = 离线或超时。
    """
    result_box = [False]
    done = threading.Event()

    def _probe():
        try:
            from xtquant import xtdata
            xtdata.get_market_data_ex(
                field_list=["close"], stock_list=["510300.SH"],
                period="1d", count=1
            )
            result_box[0] = True
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=_probe, daemon=True).start()
    done.wait(timeout=timeout_sec)
    return result_box[0]


# ==============================================================================
# 🚀 独立运行入口（直接 python tools/fetch_etf_universe.py 运行）
# ==============================================================================

def main():
    """独立运行：获取全市场 ETF 并落盘 JSON + 打印统计。"""
    print("=" * 60)
    print("📡 全市场 ETF 代码采集器")
    print(f"   运行时间: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    # QMT 预检
    print("\n🔌 预检 miniQMT 连接状态...")
    if not check_qmt_alive(timeout_sec=5.0):
        print("🔥 miniQMT 离线或无响应（5s 超时）。")
        print("   请先启动迅投 miniQMT 客户端并登录账号。")
        return

    print("✅ miniQMT 在线，开始采集...\n")

    try:
        output_path = save_etf_universe_json(fetch_names=True, verbose=True)

        # 打印前 20 只示例
        if _HAS_PANDAS:
            df = get_etf_info_df(fetch_names=False, verbose=False)
            print(f"\n📋 代码样例（前 20 只）:")
            print(f"   {'代码':<14} {'交所':<6} {'前缀':<6} {'交易制度'}")
            print(f"   {'---':<14} {'--':<6} {'--':<6} {'----'}")
            for _, row in df.head(20).iterrows():
                print(
                    f"   {row['code']:<14} {row['exchange']:<6}"
                    f" {row['prefix']:<6} {row['trade_rule']}"
                )

        print(f"\n✅ 完成！结果已写入: {output_path}")

    except RuntimeError as e:
        print(f"\n🔥 错误: {e}")
    except Exception as e:
        print(f"\n🔥 未知错误: {e}")
        import traceback
        traceback.print_exc()

    print("=" * 60)


if __name__ == "__main__":
    main()
