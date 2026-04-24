# -*- coding: utf-8 -*-
# ==============================================================================
# 🎯 [部署节点] : Quant-PC
# 📦 [核心职责] : 截面动量司令部 (Cross-Sectional Momentum Radar)
# ⚙️ [触发时机] : 每日盘后 (15:30+) 或盘前 (09:00 前) 运行一次
# 🔗 [上游]     : .state/oracle_v2_universe.json (精英 ETF 候选池)
#                 Z:/QuantpC_Workspace/Data/Market_Daily/{code}.parquet (日线本地缓存)
#                 （完全离线：无需 miniQMT，直接读 parquet 文件）
# 🔗 [下游]     : .state/momentum_slots.json     (输出给 Executor)
# ⚙️ [算法核心] : M-Score = 线性回归斜率 / 年化波动率
#                无主观指标，纯数学打分，只相信价格本身
# ==============================================================================
import os
import sys
import json
import csv
import logging
import numpy as np
import pandas as pd
from scipy.stats import linregress
from datetime import datetime, date, timedelta

from dotenv import load_dotenv
try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

load_dotenv()
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")

# ──────────────────────────────────────────────────────────────
# 🛡️ Windows 控制台 UTF-8 补丁
# ──────────────────────────────────────────────────────────────
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# ──────────────────────────────────────────────────────────────
# ⚡ 完全离线模式：直接读 Parquet 文件，无需 miniQMT
# ──────────────────────────────────────────────────────────────
# xtquant 仅在 executor 层使用（需要下单）；master 层纯数学计算，不需要

# ==============================================================================
# 📌 配置区（所有可调参数集中在此，禁止散落在代码中）
# ==============================================================================

_DIR = os.path.dirname(os.path.abspath(__file__))

# ── 输入/输出文件 ─────────────────────────────────────────────
UNIVERSE_FILE    = os.path.join(_DIR, ".state", "oracle_v2_universe.json")
OUTPUT_JSON      = os.path.join(_DIR, ".state", "momentum_slots.json")
T0_POOL_CSV      = os.path.join(_DIR, ".state", "t0_absolute_pool.csv")
# 日线 Parquet 数据目录（由 qmt_daily_sync 每日落盘，文件名规则：{6位代码}_{SH|SZ}.parquet）
MARKET_DAILY_DIR = os.path.join(os.path.dirname(_DIR), "Data", "Market_Daily")

# ── 日线历史数据回看窗口 ──────────────────────────────────────
LOOKBACK_DAYS = 250   # 约1年，与 qmt_daily_sync 保持对齐
WINDOW        = 20    # 动量计算窗口（约1个月）

# ── 筛选防线 ─────────────────────────────────────────────────
# oracle_v2_universe.json 中 avg_amount_5d 单位 = 亿元
MIN_AVG_AMOUNT_YI = 5.0   # 5 亿人民币流动性底线（低于此的视为冷门标的）
MAX_RSI_THRESHOLD = 75.0  # 鱼尾防线：拒绝极度超买接盘
TOP_N             = 3     # 只取最强前 3 名

# ── 日志 ─────────────────────────────────────────────────────
LOG_DIR = os.path.join(_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(os.path.join(_DIR, ".state"), exist_ok=True)

_LOG_FILE = os.path.join(LOG_DIR, f"{date.today().strftime('%Y%m%d')}_momentum_master.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
_logger = logging.getLogger("momentum")


# ==============================================================================
# 🛠️ 工具函数
# ==============================================================================

def send_webhook(title: str, message: str):
    """N8N 推送：失败静默，timeout=5s（见 quant-v4-patterns §13）。"""
    if not _HAS_REQUESTS or not N8N_WEBHOOK_URL:
        return
    try:
        requests.post(
            N8N_WEBHOOK_URL,
            json={"title": title, "message": message,
                  "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            timeout=5,
        )
    except Exception:
        pass


def _load_t0_set() -> set:
    """
    加载 T+0 交易白名单（quant-v4-patterns §12.5 铁律）。
    永远通过读取 t0_absolute_pool.csv，永不用代码前缀推导。
    失败时返回空集合，所有标的按 T+1 保守处理。
    """
    t0_codes: set = set()
    if not os.path.exists(T0_POOL_CSV):
        _logger.warning(f"⚠️ T+0 白名单文件不存在: {T0_POOL_CSV}，所有标的按 T+1 处理")
        return t0_codes
    try:
        with open(T0_POOL_CSV, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            for row in reader:
                if row and row[0].strip():
                    code = row[0].strip()
                    if len(code) == 9 and "." in code:   # 跳过非代码行（如表头）
                        t0_codes.add(code)
        _logger.info(f"🛡️ T+0 白名单加载：{len(t0_codes)} 只可 T+0 交易")
    except Exception as e:
        _logger.warning(f"⚠️ T+0 白名单读取失败: {e}，所有标的按 T+1 处理")
    return t0_codes


def _last_trading_day_str() -> str:
    """推算最近交易日字符串（跳过周末，不处理节假日）。"""
    d = date.today()
    wday = d.weekday()
    if wday == 5:
        d -= timedelta(days=1)
    elif wday == 6:
        d -= timedelta(days=2)
    return d.strftime("%Y%m%d")


def _code_to_parquet(code: str) -> str:
    """
    将 QMT 标准代码（如 510050.SH）转换为 parquet 文件路径。
    文件命名规则：{6位代码}_{SH|SZ}.parquet
    """
    parts = code.split(".")
    if len(parts) == 2:
        fname = f"{parts[0]}_{parts[1]}.parquet"
    else:
        fname = f"{code}.parquet"  # fallback
    return os.path.join(MARKET_DAILY_DIR, fname)


def _load_parquet_close(code: str) -> pd.DataFrame | None:
    """
    从本地 Parquet 文件读取日线 close/volume/amount 数据。
    完全离线，无需 miniQMT 连接。
    返回 DataFrame（index=date，columns=[close, volume, amount]）或 None（文件不存在/读取失败）。
    """
    path = _code_to_parquet(code)
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_parquet(path, columns=["close", "volume", "amount"])
        # 确保 index 是日期格式，便于切片
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        return df
    except Exception as e:
        _logger.warning(f"⚠️ [{code}] 读取 parquet 失败: {e}")
        return None


def calculate_momentum_score(prices: np.ndarray, volumes: np.ndarray | None = None) -> dict:
    """
    计算核心数学动量得分 (M-Score)。

    参数
    ----
    prices  : 最近 WINDOW 天的收盘价数组（已降维，shape=(WINDOW,)）
    volumes : 最近 5 天成交量（可选，仅用于内部参考，流动性过滤由 universe 层完成）

    返回
    ----
    dict: {m_score, slope, volatility, rsi}
    """
    n = len(prices)
    if n < WINDOW or np.any(np.isnan(prices)):
        return {"m_score": -999.0, "slope": 0.0, "volatility": 0.0, "rsi": 0.0}

    # 1. 归一化价格路径（起点→0，跨品种公平比较）
    normalized = (prices - prices[0]) / prices[0]

    # 2. 线性回归斜率 (Slope per day)
    x = np.arange(n, dtype=float)
    slope, *_ = linregress(x, normalized)

    # 3. 年化波动率（惩罚项）
    daily_ret = np.diff(prices) / prices[:-1]
    vol = float(np.std(daily_ret) * np.sqrt(252))
    if vol < 1e-8 or np.isnan(vol):
        vol = 1e-8   # 防零除

    # 💣 4. M-Score：斜率 / 波动率（夏普比率的截面近似）
    m_score = float(slope) / vol

    # 5. RSI(14) — 防守过滤（鱼尾识别）
    gains  = np.where(daily_ret > 0, daily_ret, 0.0)
    losses = np.where(daily_ret < 0, -daily_ret, 0.0)
    g14    = gains[-14:]  if len(gains)  >= 14 else gains
    l14    = losses[-14:] if len(losses) >= 14 else losses
    avg_g  = float(np.mean(g14)) if len(g14) > 0 else 0.0
    avg_l  = float(np.mean(l14)) if len(l14) > 0 else 0.0
    rsi    = 100.0 if avg_l < 1e-12 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)

    return {
        "m_score":    round(m_score, 4),
        "slope":      round(float(slope), 6),
        "volatility": round(vol, 4),
        "rsi":        round(rsi, 2),
    }


# ==============================================================================
# 🚀 主函数
# ==============================================================================

def run_momentum_radar():
    _logger.info("=" * 60)
    _logger.info(f"🚀 截面动量司令部 启动 | {datetime.now():%Y-%m-%d %H:%M:%S}")
    _logger.info(f"   动量窗口: {WINDOW}天  流动性底线: {MIN_AVG_AMOUNT_YI}亿  TOP_N: {TOP_N}")
    _logger.info("=" * 60)

    # ── 预加载 T+0 白名单（master 层写入 trade_rule，Executor 直接读取）──
    t0_set = _load_t0_set()

    # ── 步骤 1：加载 ETF 候选池 ───────────────────────────────
    if not os.path.exists(UNIVERSE_FILE):
        _logger.error(f"❌ 找不到候选池文件: {UNIVERSE_FILE}")
        _logger.error("   请先运行 etf_rotation_master.py 或 etf_ou_grid_master.py 生成宇宙文件。")
        return

    with open(UNIVERSE_FILE, encoding="utf-8") as f:
        universe_data = json.load(f)

    raw_pool = universe_data.get("universe", [])
    _logger.info(f"📚 候选池装载完毕：共 {len(raw_pool)} 只 ETF")

    # ── 步骤 2：流动性初筛（利用 universe 中已有的 avg_amount_5d）──
    liquid_pool = [
        item for item in raw_pool
        if float(item.get("avg_amount_5d", 0)) >= MIN_AVG_AMOUNT_YI
    ]
    candidates = [item["code"] for item in liquid_pool]
    _logger.info(
        f"💧 流动性过滤（≥{MIN_AVG_AMOUNT_YI}亿）："
        f"{len(raw_pool)} → {len(candidates)} 只"
    )

    if not candidates:
        _logger.error("❌ 无候选标的通过流动性筛选，终止。")
        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump([], f)
        return

    # ── 步骤 3：从本地 Parquet 文件读取日线（完全离线，无需 miniQMT）─────
    _logger.info(f"📥 读取本地日线 Parquet（无需 miniQMT）：最近 {LOOKBACK_DAYS} 个交易日，标的数={len(candidates)}")
    _logger.info(f"   数据目录: {MARKET_DAILY_DIR}")

    # 预构建 name 查询表（从 universe json，避免调用 xtdata）
    _name_map = {item["code"]: item.get("name", "") for item in liquid_pool}

    # ── 步骤 4：逐标的计算 M-Score ────────────────────────────
    results = []
    skip_no_data    = 0
    skip_short_hist = 0
    skip_neg_slope  = 0

    for code in candidates:
        # ── 读取 Parquet（完全离线，无需 miniQMT）──────────────
        df = _load_parquet_close(code)
        if df is None or df.empty:
            skip_no_data += 1
            continue

        if "close" not in df.columns:
            skip_no_data += 1
            continue

        df = df.dropna(subset=["close"])

        if len(df) < WINDOW:
            skip_short_hist += 1
            continue

        prices = df["close"].tail(WINDOW).values.astype(float)

        stats = calculate_momentum_score(prices)

        # 第一道防线：仅允许正斜率（上涨趋势）标的进入赛圈
        if stats["slope"] <= 0:
            skip_neg_slope += 1
            continue

        stats["code"] = code
        # 附带 universe 元数据供输出报表使用
        meta = next((item for item in liquid_pool if item["code"] == code), {})
        stats["avg_amount_5d"] = meta.get("avg_amount_5d", 0)
        stats["atr14_pct"]     = meta.get("atr14_pct", 0)
        # T+0/T+1 分类（master 层写入，Executor 直接读取，无需再查白名单）
        stats["trade_rule"]    = "T+0" if code in t0_set else "T+1"
        # ETF 名称：优先从 universe json 读取（已有，无需 xtdata 连接）
        stats["name"] = _name_map.get(code, "")
        results.append(stats)

    _logger.info(
        f"📊 扫描结束：通过 {len(results)} 只 | "
        f"无数据 {skip_no_data} | 历史不足 {skip_short_hist} | 负斜率淘汰 {skip_neg_slope}"
    )

    if not results:
        _logger.warning("⚠️ 市场极度冰冷，没有任何标的满足动量底线。")
        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump([], f)
        send_webhook(
            "🚨 动量司令部 市场极冷告警",
            f"扫描 {len(candidates)} 只标的后，无任何标的满足动量底线。\n"
            f"候选池: {len(raw_pool)} → 流动性过滤后: {len(candidates)} → 有效分数: 0\n"
            f"🕐 {datetime.now():%Y-%m-%d %H:%M:%S}"
        )
        return

    # ── 步骤 5：达尔文排位赛（M-Score 降序）──────────────────
    df_results = pd.DataFrame(results)
    df_results = df_results.sort_values(by="m_score", ascending=False)

    # ── 步骤 6：第二道防线 — 鱼尾过滤（RSI 超买）─────────────
    df_filtered = df_results[df_results["rsi"] < MAX_RSI_THRESHOLD].reset_index(drop=True)
    n_rsi_cut   = len(df_results) - len(df_filtered)
    if n_rsi_cut > 0:
        _logger.info(f"🐟 RSI 鱼尾过滤（RSI≥{MAX_RSI_THRESHOLD}）：剔除 {n_rsi_cut} 只")

    # ── 步骤 7：提取精锐 ──────────────────────────────────────
    top_targets = df_filtered.head(TOP_N).to_dict("records")

    _logger.info("-" * 60)
    _logger.info(f"🏆 截面动量雷达扫描完毕，锁定最强 {len(top_targets)} 只标的：")
    for i, t in enumerate(top_targets):
        _logger.info(
            f"  [{i+1}] {t['code']} {t.get('name', '')} | "
            f"M-Score: {t['m_score']:+.4f} | "
            f"斜率: {t['slope']:+.6f} | "
            f"波动: {t['volatility']:.4f} | "
            f"RSI: {t['rsi']:.1f} | "
            f"成交: {t.get('avg_amount_5d', 0):.2f}亿 | "
            f"ATR14: {t.get('atr14_pct', 0):.2f}% | "
            f"交易制度: {t.get('trade_rule', '?')}"
        )
    _logger.info("-" * 60)

    # ── 步骤 8：落盘输出 → 下游 Executor 消费 ────────────────
    output = [
        {
            "code":          t["code"],
            "name":          t.get("name", ""),
            "trade_rule":    t.get("trade_rule", "T+1"),   # T+0 / T+1（白名单查表写入，Executor 直接读）
            "m_score":       t["m_score"],
            "slope":         t["slope"],
            "volatility":    t["volatility"],
            "rsi":           t["rsi"],
            "avg_amount_5d": t.get("avg_amount_5d", 0),
            "atr14_pct":     t.get("atr14_pct", 0),
        }
        for t in top_targets
    ]

    tmp_path = OUTPUT_JSON + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)
    os.replace(tmp_path, OUTPUT_JSON)   # 原子替换（铁律四：防并发写损坏）

    _logger.info(f"✅ 狩猎名单已安全落盘 → {OUTPUT_JSON}")
    _logger.info(f"   更新时间: {datetime.now():%Y-%m-%d %H:%M:%S}")

    # ── 步骤 9：N8N 推送扫描结果 ──────────────────────────────
    lines = []
    for i, t in enumerate(output):
        rule  = t.get("trade_rule", "?")
        emoji = "🟢" if rule == "T+0" else "🟡"
        lines.append(
            f"[{i+1}] {emoji}{rule} {t['code']} {t.get('name', '')}\n"
            f"    M-Score={t['m_score']:+.4f} | RSI={t['rsi']:.1f} | "
            f"ATR={t.get('atr14_pct',0):.2f}% | 成交={t.get('avg_amount_5d',0):.1f}亿"
        )
    send_webhook(
        f"🏆 动量司令部 扫描完毕 TOP{len(output)}",
        "\n".join(lines) + f"\n\n🕐 {datetime.now():%Y-%m-%d %H:%M:%S}"
    )


# ==============================================================================
if __name__ == "__main__":
    run_momentum_radar()