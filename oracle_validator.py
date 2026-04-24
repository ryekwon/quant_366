#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
oracle_validator.py — 双预言机竞技场 (Oracle Arena)
========================================================
功能：对两个大模型预言机在 A 股宏观 ETF 上进行历史回测，横向评测预测准确率。

架构要点：
  - 严禁未来函数：每个时间节点 Date 只传 Date 之前的 K 线数据给 API
  - 时间漫游 (Time Travel)：按真实交易日顺序逐日推进
  - T+5 物理对账：用 Date 之后第 5 个交易日的收盘价计算真实涨跌幅
  - 多线程并发请求：ThreadPoolExecutor 防止单点 API 阻塞
  - 冷血进度日志：logging 全程记录，不吞任何异常

输出：oracle_arena_results.csv
字段：Date, Code, Model_A_Odds, Model_B_Odds, Actual_T5_Return
"""

import os
import sys
import csv
import json
import time
import logging
import threading
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from typing import Optional

import requests

# ═══════════════════════════════════════════════════════════════
# ① 日志配置（冷血模式：INFO + DEBUG 双通道）
# ═══════════════════════════════════════════════════════════════
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    handlers=[
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "logs",
                         f"oracle_validator_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
            encoding="utf-8"
        ),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("OracleArena")


# ═══════════════════════════════════════════════════════════════
# ② 核心配置区（集中管理，禁止散落代码中）
# ═══════════════════════════════════════════════════════════════
# 9 只宏观 ETF 标的池
MACRO_ETF_UNIVERSE = [
    "510300.SH",   # 沪深300 ETF（华泰柏瑞）
    "513500.SH",   # 标普500 ETF
    "513100.SH",   # 纳指100 ETF
    "159915.SZ",   # 创业板ETF（易方达）
    "512890.SH",   # 红利低波ETF
    "518880.SH",   # 黄金ETF（华安）
    #"511260.SH",   # 10年国债ETF
    "513050.SH",   # 中概互联ETF
    "513180.SH",   # 恒生科技ETF
]

# 回测时间范围
BACKTEST_START  = "20240101"
BACKTEST_END    = "20260420"

# 时间漫游参数
LOOKBACK_BARS   = 20    # 每次传给 API 的历史 K 线数量
T5_HORIZON      = 5     # T+5 实际涨跌幅计算窗口

# 预言机 API 配置
MODEL_A_URL     = "http://10.10.8.20:8000/predict"   # TimesFM
MODEL_B_URL     = ""    # 暂未配置，留空则跳过（输出 None）
API_TIMEOUT_SEC = 8     # 单次请求超时（降低到 8s，API 不可达时快速失败）
MAX_WORKERS     = 18    # 线程池大小（9 ETF × 2 Model，可完全并发）
API_RETRY       = 2     # 失败重试次数（ConnectionError 不重试，直接快速失败）

# 输出文件路径（与脚本同目录）
_PROJECT_ROOT   = os.path.dirname(os.path.abspath(__file__))
OUTPUT_CSV      = os.path.join(_PROJECT_ROOT, "oracle_arena_results.csv")
CSV_FIELDNAMES  = ["Date", "Code", "Model_A_Odds", "Model_B_Odds", "Actual_T5_Return"]

# 本地 parquet 数据湖（由 qmt_daily_sync.py 每日 15:30 落盘，完全离线可用）
# 文件名规则：510300.SH → 510300_SH.parquet
DATA_LAKE_PATH  = r"Z:\QuantpC_Workspace\Data\Market_Daily"

# 注：parquet 数据为前复权（qmt_daily_sync.py 使用 dividend_type='front'）
# 回测用前复权数据计算相对涨跌幅不影响准确性


# ═══════════════════════════════════════════════════════════════
# ③ QMT 数据加载（一次性全量拉取，本地缓存）
# ═══════════════════════════════════════════════════════════════
def load_market_data() -> dict:
    """
    【离线模式】直接从 qmt_daily_sync.py 落盘的 parquet 数据湖读取，
    完全无需 miniQMT 在线。

    文件格式：
      路径  : DATA_LAKE_PATH / {510300_SH}.parquet
      index : datetime.date（已排序）
      字段  : open, high, low, close, volume, amount
      复权  : 前复权（front-adjusted，qmt_daily_sync 落盘时设置）
    """
    import pandas as pd

    if not os.path.isdir(DATA_LAKE_PATH):
        raise RuntimeError(
            f"❌ 数据湖目录不存在: {DATA_LAKE_PATH}\n"
            f"  请先运行 qmt_daily_sync.py 落盘历史日线数据。"
        )

    # 回测区间解析（YYYYMMDD → datetime.date）
    from datetime import date as dt_date
    start_dt = dt_date(
        int(BACKTEST_START[:4]), int(BACKTEST_START[4:6]), int(BACKTEST_START[6:])
    )
    end_dt = dt_date(
        int(BACKTEST_END[:4]), int(BACKTEST_END[4:6]), int(BACKTEST_END[6:])
    )

    logger.info("══ [离线模式] 从本地 parquet 数据湖加载宏观 ETF 日线 ══")
    logger.info(f"  数据湖: {DATA_LAKE_PATH}")
    logger.info(f"  回测区间: {start_dt} → {end_dt}")
    logger.info(f"  标的共 {len(MACRO_ETF_UNIVERSE)} 只")

    valid = {}
    for code in MACRO_ETF_UNIVERSE:
        fname = code.replace(".", "_") + ".parquet"
        fpath = os.path.join(DATA_LAKE_PATH, fname)

        if not os.path.exists(fpath):
            logger.warning(f"  MISS [{code}] {fname} 不存在于数据湖，跳过")
            logger.warning(f"       （可能被 qmt_daily_sync 的 junk_pattern 过滤，需手动补录）")
            continue

        try:
            df = pd.read_parquet(fpath)
        except Exception as e:
            logger.error(f"  ERR  [{code}] 读取 parquet 失败: {e}")
            continue

        # index 是 datetime.date，过滤回测区间
        df = df.sort_index()
        df = df[(df.index >= start_dt) & (df.index <= end_dt)]

        if df.empty:
            logger.warning(f"  EMPTY [{code}] 回测区间内无数据")
            continue

        # 验证必要字段
        if "close" not in df.columns:
            logger.error(f"  ERR  [{code}] 缺少 close 列，跳过")
            continue

        # 清除 NaN close
        df = df.dropna(subset=["close"])
        df = df[df["close"] > 0]

        valid[code] = df
        logger.info(f"  OK   [{code}] {len(df)} 根日线  "
                    f"{str(df.index[0])} → {str(df.index[-1])}")

    if not valid:
        raise RuntimeError(
            "❌ 所有标的均无有效数据！\n"
            f"  请确认数据湖路径: {DATA_LAKE_PATH}\n"
            f"  并确认文件存在，如: {MACRO_ETF_UNIVERSE[0].replace('.','_')}.parquet"
        )

    logger.info(f"  共 {len(valid)} / {len(MACRO_ETF_UNIVERSE)} 只标的就绪。")
    return valid


def _ts_to_date(ts) -> str:
    """统一转为 YYYY-MM-DD 字符串（兼容 datetime.date / int ms / str）"""
    from datetime import date as dt_date
    if isinstance(ts, dt_date):
        return ts.strftime("%Y-%m-%d")
    try:
        ts_int = int(ts)
        if ts_int > 1_000_000_000_000:   # 毫秒级时间戳
            return datetime.fromtimestamp(ts_int / 1000).strftime("%Y-%m-%d")
        return str(ts)[:10]
    except Exception:
        return str(ts)[:10]


def build_trading_calendar(market_data: dict) -> list:
    """
    从所有标的的 index 取并集，生成全量交易日历（datetime.date 列表，已排序）。
    取并集保证：任何一只有数据的交易日都被纳入循环。
    """
    all_dates = set()
    for df in market_data.values():
        all_dates.update(df.index.tolist())   # index 是 datetime.date
    calendar = sorted(all_dates)
    logger.info(f"  交易日历：{len(calendar)} 个交易日  "
                f"({_ts_to_date(calendar[0])} → {_ts_to_date(calendar[-1])})")
    return calendar


# ═══════════════════════════════════════════════════════════════
# ④ API 请求层（多线程 + 重试 + 超时保护）
# ═══════════════════════════════════════════════════════════════
def _build_payload(code: str, date_str: str, close_series: list) -> dict:
    """
    构造发送给 TimesFM / 自定义预言机的标准 JSON 请求体。
    TimesFM HTTP 接口规范（参考官方 /predict endpoint）：
      inputs:    [[...历史收盘价序列...]]
      freq:      ["D"]（日频）
      horizon:   预测未来 N 步
    """
    return {
        "inputs":    [close_series],   # shape: [[v1, v2, ..., v20]]
        "freq":      ["D"],
        "horizon":   T5_HORIZON,
        "context":   {
            "code":    code,
            "date":    date_str,
            "lookback": LOOKBACK_BARS,
        }
    }


def _call_api(url: str, payload: dict, model_tag: str) -> Optional[float]:
    """
    发送 POST 请求并解析预测结果（归一化为单一浮点数 Odds）。
    返回 None 表示失败（超时、解析错误等）。
    不抛出异常：所有错误静默降级，由调用方记录。
    """
    if not url:
        return None   # 未配置的 Model 直接跳过

    for attempt in range(1, API_RETRY + 1):
        try:
            resp = requests.post(
                url,
                json=payload,
                timeout=API_TIMEOUT_SEC,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

            # 解析 TimesFM 标准响应格式：{"mean": [[...]], "quantiles": {...}}
            # "Odds" 定义为 T+5 预测均值涨跌幅
            prediction = _extract_odds(data, payload)
            return prediction

        except requests.exceptions.Timeout:
            logger.debug(f"  [{model_tag}] 请求超时（第{attempt}次），"
                         f"code={payload['context']['code']} date={payload['context']['date']}")
        except requests.exceptions.ConnectionError:
            # 服务器整体不可达（拒绝连接/DNS失败）→ 快速失败，不重试
            # 避免 API 宕机时每次都等满 timeout 秒
            logger.debug(f"  [{model_tag}] 连接被拒（服务不可达），快速失败")
            return None
        except requests.exceptions.HTTPError as e:
            logger.debug(f"  [{model_tag}] HTTP错误（第{attempt}次）: {e}")
        except (KeyError, ValueError, TypeError) as e:
            logger.debug(f"  [{model_tag}] 响应解析失败: {e} | resp={resp.text[:200] if 'resp' in dir() else 'N/A'}")
            return None   # 解析失败不重试
        except Exception as e:
            logger.debug(f"  [{model_tag}] 未知异常（第{attempt}次）: {type(e).__name__}: {e}")

        if attempt < API_RETRY:
            time.sleep(1.0 * attempt)   # 指数退避

    return None   # 全部重试失败


def _extract_odds(response_data: dict, payload: dict) -> Optional[float]:
    """
    从 API 响应中提取「预测 Odds」= T+5 均值预测值相对当前收盘价的涨跌幅。
    兼容 TimesFM 的多种响应格式：
      1. {"mean": [[p1, p2, p3, p4, p5]]}         → 取 p5（T+5 预测均值）
      2. {"forecast": [[p1, ..., p5]]}             → 同上
      3. {"predictions": [{"value": x}]}           → 取最后一项
      4. {"result": float}                         → 直接使用
    """
    current_close = payload["inputs"][0][-1]   # 当前 Date 的收盘价（序列最后一个）
    if current_close <= 0:
        return None

    # 格式 1 / 2：矩阵型预测
    for key in ("mean", "forecast", "output"):
        if key in response_data:
            vals = response_data[key]
            if isinstance(vals, list) and vals:
                inner = vals[0] if isinstance(vals[0], list) else vals
                if inner:
                    t5_pred = float(inner[-1])   # 取 horizon 末端（T+5）
                    return (t5_pred / current_close) - 1.0

    # 格式 3：列表型预测
    if "predictions" in response_data:
        preds = response_data["predictions"]
        if isinstance(preds, list) and preds:
            last_val = preds[-1]
            if isinstance(last_val, dict):
                t5_pred = float(last_val.get("value", last_val.get("mean", current_close)))
            else:
                t5_pred = float(last_val)
            return (t5_pred / current_close) - 1.0

    # 格式 4：直接返回涨跌幅
    if "result" in response_data:
        return float(response_data["result"])

    # 格式 5：顶层数字
    if "value" in response_data:
        t5_pred = float(response_data["value"])
        return (t5_pred / current_close) - 1.0

    logger.debug(f"  ⚠️ 无法识别响应格式，keys={list(response_data.keys())}")
    return None


# ═══════════════════════════════════════════════════════════════
# ⑤ 时间漫游引擎（核心回测循环）
# ═══════════════════════════════════════════════════════════════
def run_backtest(market_data: dict) -> list:
    """
    时间漫游主循环：
      for each trading_day d in calendar:
        for each code:
          ① 取 d 之前的 LOOKBACK_BARS 根 K 线（严禁未来函数）
          ② 并发调用 Model_A / Model_B
          ③ 计算真实 T+5 涨跌幅
          ④ 记录结果行
    """
    calendar = build_trading_calendar(market_data)
    total_days = len(calendar)
    results = []

    # 构造每只标的的 date→位置 映射（O(1) 查找，加速时间漫游）
    # key 是 datetime.date 对象
    code_index_map = {
        code: {date_key: i for i, date_key in enumerate(df.index)}
        for code, df in market_data.items()
    }

    logger.info("════════════════════════════════════════════════")
    logger.info(f"  开始时间漫游：{total_days} 个交易日 × {len(market_data)} 只标的")
    logger.info(f"  并发线程数：{MAX_WORKERS}  API超时：{API_TIMEOUT_SEC}s  重试：{API_RETRY}")
    logger.info("════════════════════════════════════════════════")

    start_wall = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="OracleWorker") as executor:
        for day_idx, current_ts in enumerate(calendar):
            date_str  = _ts_to_date(current_ts)
            day_tasks = {}   # future → (code, model_tag)

            # ── 为每只标的构造本日任务 ──
            for code, df in market_data.items():
                idx_map = code_index_map[code]
                if current_ts not in idx_map:
                    continue   # 该标的本日停牌 / 无数据

                pos = idx_map[current_ts]

                # ① 无未来函数检查：必须有足够的历史 K 线
                #    pos 是当日在序列中的位置（0-based）
                #    需要 pos >= LOOKBACK_BARS（即 [pos-LOOKBACK_BARS, pos-1] 共 LOOKBACK_BARS 根）
                if pos < LOOKBACK_BARS:
                    continue   # 历史数据不足，跳过

                # ② 切取历史窗口（严格不包含当日！pos-LOOKBACK_BARS 到 pos-1）
                #    注意：pos 本身是当日，不传给 API
                hist_slice  = df.iloc[pos - LOOKBACK_BARS : pos]
                close_hist  = hist_slice["close"].tolist()

                # 验证序列合法性（防止 NaN / 0 污染）
                if len(close_hist) < LOOKBACK_BARS or any(v <= 0 for v in close_hist):
                    continue

                payload = _build_payload(code, date_str, close_hist)

                # ③ 计算 T+5 真实涨跌幅（只用已知数据）
                t5_pos = pos + T5_HORIZON
                if t5_pos >= len(df):
                    t5_return = None   # 数据末尾不足 T+5
                else:
                    current_close = float(df.iloc[pos]["close"])
                    t5_close      = float(df.iloc[t5_pos]["close"])
                    t5_return = (t5_close / current_close - 1.0) if current_close > 0 else None

                # ④ 并发投递 Model_A / Model_B 任务
                fut_a = executor.submit(_call_api, MODEL_A_URL, payload, f"ModelA-{code}")
                day_tasks[fut_a] = (code, "A", payload, t5_return)

                if MODEL_B_URL:
                    fut_b = executor.submit(_call_api, MODEL_B_URL, payload, f"ModelB-{code}")
                    day_tasks[fut_b] = (code, "B", payload, t5_return)

            if not day_tasks:
                continue

            # ⑤ 收集本日所有任务结果（带超时兜底）
            # 按 code 汇总：{code: {A: odds, B: odds, t5: return}}
            day_results: dict = {}

            try:
                futures_iter = as_completed(day_tasks, timeout=API_TIMEOUT_SEC * API_RETRY + 5)
                for fut in futures_iter:
                    code, model_tag, payload_, t5_ret = day_tasks[fut]
                    try:
                        odds = fut.result(timeout=1)
                    except Exception as e:
                        logger.debug(f"  [{model_tag}-{code}] future异常: {e}")
                        odds = None

                    if code not in day_results:
                        day_results[code] = {
                            "Date":             date_str,
                            "Code":             code,
                            "Model_A_Odds":     None,
                            "Model_B_Odds":     None,
                            "Actual_T5_Return": t5_ret,
                        }
                    if model_tag == "A":
                        day_results[code]["Model_A_Odds"] = odds
                    else:
                        day_results[code]["Model_B_Odds"] = odds

            except FuturesTimeout:
                # as_completed 整批超时：把还未完成的 future 对应行以 None 补齐
                logger.debug(f"  [Day={date_str}] as_completed 整批超时，剩余 future 记为 None")
                for fut, (code, model_tag, payload_, t5_ret) in day_tasks.items():
                    if code not in day_results:
                        day_results[code] = {
                            "Date":             date_str,
                            "Code":             code,
                            "Model_A_Odds":     None,
                            "Model_B_Odds":     None,
                            "Actual_T5_Return": t5_ret,
                        }

            # 补全无 Model_B 的行（URL 为空时）
            for code, row in day_results.items():
                results.append(row)

            # ⑥ 进度日志（每 20 个交易日报一次）
            if (day_idx + 1) % 20 == 0 or (day_idx + 1) == total_days:
                elapsed    = time.time() - start_wall
                pct        = (day_idx + 1) / total_days * 100
                eta_sec    = (elapsed / (day_idx + 1)) * (total_days - day_idx - 1)
                a_success  = sum(1 for r in results if r["Model_A_Odds"] is not None)
                logger.info(
                    f"  进度: {day_idx+1:>4}/{total_days} ({pct:5.1f}%)  "
                    f"已记录: {len(results):>5}行  "
                    f"ModelA成功率: {a_success/max(len(results),1)*100:.1f}%  "
                    f"ETA: {eta_sec/60:.1f}min"
                )

    elapsed_total = time.time() - start_wall
    logger.info(f"  ✅ 时间漫游完成，共 {len(results)} 行数据，耗时 {elapsed_total/60:.1f} 分钟")
    return results


# ═══════════════════════════════════════════════════════════════
# ⑥ 结果落盘（CSV 原子写入）
# ═══════════════════════════════════════════════════════════════
def save_results(results: list) -> None:
    """将结果写入 CSV，按 Date + Code 排序，使用原子替换防损坏。"""
    if not results:
        logger.warning("⚠️ 结果集为空，跳过落盘。")
        return

    sorted_results = sorted(results, key=lambda r: (r["Date"], r["Code"]))
    tmp_path = OUTPUT_CSV + ".tmp"

    with open(tmp_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for row in sorted_results:
            writer.writerow({
                "Date":             row["Date"],
                "Code":             row["Code"],
                "Model_A_Odds":     _fmt_float(row["Model_A_Odds"]),
                "Model_B_Odds":     _fmt_float(row["Model_B_Odds"]),
                "Actual_T5_Return": _fmt_float(row["Actual_T5_Return"]),
            })

    os.replace(tmp_path, OUTPUT_CSV)   # 原子替换
    logger.info(f"  💾 结果已落盘：{OUTPUT_CSV}")
    logger.info(f"  📊 共 {len(sorted_results)} 行  字段：{CSV_FIELDNAMES}")


def _fmt_float(v) -> str:
    """格式化浮点数：None → '' ，有值 → 保留 6 位有效数字"""
    if v is None:
        return ""
    try:
        return f"{float(v):.6f}"
    except (TypeError, ValueError):
        return ""


# ═══════════════════════════════════════════════════════════════
# ⑦ 评测统计报告（控制台汇总）
# ═══════════════════════════════════════════════════════════════
def print_summary_report(results: list) -> None:
    """输出简明评测报告：方向准确率、覆盖率、平均 Odds 偏差。"""
    if not results:
        return

    total      = len(results)
    has_a      = [r for r in results if r["Model_A_Odds"]     is not None]
    has_b      = [r for r in results if r["Model_B_Odds"]     is not None]
    has_actual = [r for r in results if r["Actual_T5_Return"] is not None]

    def directional_acc(rows, model_key):
        """方向准确率：预测涨跌方向与实际方向一致的比率"""
        valid = [r for r in rows
                 if r[model_key] is not None and r["Actual_T5_Return"] is not None]
        if not valid:
            return 0.0, 0
        correct = sum(
            1 for r in valid
            if (r[model_key] > 0) == (r["Actual_T5_Return"] > 0)
        )
        return correct / len(valid) * 100, len(valid)

    acc_a, n_a = directional_acc(results, "Model_A_Odds")
    acc_b, n_b = directional_acc(results, "Model_B_Odds")

    logger.info("═" * 56)
    logger.info("  📈 评测汇总报告 (Oracle Arena Summary)")
    logger.info("═" * 56)
    logger.info(f"  总记录数        : {total:>6}")
    logger.info(f"  有T+5真实数据   : {len(has_actual):>6} ({len(has_actual)/total*100:.1f}%)")
    logger.info(f"  Model_A 覆盖率  : {len(has_a):>6} ({len(has_a)/total*100:.1f}%)")
    logger.info(f"  Model_B 覆盖率  : {len(has_b):>6} ({len(has_b)/total*100:.1f}%)")
    logger.info(f"  Model_A 方向准确率: {acc_a:5.1f}%  (n={n_a})")
    logger.info(f"  Model_B 方向准确率: {acc_b:5.1f}%  (n={n_b})")
    logger.info("═" * 56)

    # 按标的分组统计
    from collections import defaultdict
    by_code: dict = defaultdict(list)
    for r in results:
        by_code[r["Code"]].append(r)

    logger.info("  各标的 Model_A 方向准确率：")
    for code in sorted(by_code.keys()):
        acc, n = directional_acc(by_code[code], "Model_A_Odds")
        logger.info(f"    {code}: {acc:5.1f}%  (n={n})")
    logger.info("═" * 56)


# ═══════════════════════════════════════════════════════════════
# ⑧ 入口
# ═══════════════════════════════════════════════════════════════
def main():
    logger.info("╔══════════════════════════════════════════════════╗")
    logger.info("║   Oracle Arena — 双预言机竞技场历史回测评测器   ║")
    logger.info("╚══════════════════════════════════════════════════╝")
    logger.info(f"  Model_A (TimesFM): {MODEL_A_URL}")
    logger.info(f"  Model_B:           {MODEL_B_URL if MODEL_B_URL else '(未配置，跳过)'}")
    logger.info(f"  回测区间: {BACKTEST_START} → {BACKTEST_END}")
    logger.info(f"  输出文件: {OUTPUT_CSV}")

    # 确保 logs 目录存在
    os.makedirs(os.path.join(_PROJECT_ROOT, "logs"), exist_ok=True)

    try:
        # 阶段1：加载数据
        market_data = load_market_data()

        # 阶段2：时间漫游 + 并发 API 请求
        results = run_backtest(market_data)

        # 阶段3：落盘
        save_results(results)

        # 阶段4：统计报告
        print_summary_report(results)

        logger.info("✅ Oracle Arena 评测完成！")

    except KeyboardInterrupt:
        logger.warning("⚠️ 用户中断（Ctrl+C），正在保存已有数据…")
        if "results" in dir() and results:
            save_results(results)
        logger.info("  数据已保存，程序退出。")
        sys.exit(0)

    except Exception as e:
        logger.critical(f"❌ 致命错误: {type(e).__name__}: {e}")
        logger.critical(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
