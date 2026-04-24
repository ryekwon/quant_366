# ==========================================
# 部署: Quant-PC
# 命名: refine_core_universe.py (全自动星图采集器)
# 职责: 达尔文机制海选 -> 物理体检 -> 输出动态 40 席
#
# [FIX 2026-04-09] 三项修复：
#   1. 改用 get_stock_list_in_sector("场内基金") 获取全量 ETF（覆盖 52x 科创系列）
#   2. download_history_data2 批量调用加线程+超时熔断（防死锁）
#   3. 输出 json 路径改为脚本所在目录（防 CWD 漂移）
# ==========================================
from xtquant import xtdata
import pandas as pd
import json
import os
import threading
import time

# ── 输出 CSV 路径（Top100 流动性榜）─────────────────────────────────────────
_TOP100_CSV_RELPATH = ".state/top100_liquidity.csv"

# 脚本所在目录（防止从其他 CWD 调用时路径漂移）
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 输出文件放到项目根目录（tools 的上一级）
_PROJECT_DIR = os.path.dirname(_SCRIPT_DIR)

# ── ETF 代码前缀白名单（全系列）─────────────────────────────────────────────
# 深市: 15x (含创业板 ETF、跨境 ETF)
# 沪市: 51x / 52x / 58x (含科创板 58x、普通 ETF 51x/52x)
_ETF_PREFIXES = ('15', '51', '52', '58')


# ── 批量获取 ETF 中文名称 ───────────────────────────────────────────────────
def _get_etf_names(codes: list) -> dict:
    """
    批量调用 get_instrument_detail 获取 ETF 中文简称。
    返回 {code: name} 字典；未能获取的返回空字符串。
    """
    name_map = {}
    for code in codes:
        try:
            detail = xtdata.get_instrument_detail(code)
            # detail 可能是 dict 或 None
            if detail and isinstance(detail, dict):
                name_map[code] = detail.get('InstrumentName', detail.get('instrument_name', ''))
            else:
                name_map[code] = ''
        except Exception:
            name_map[code] = ''
    return name_map


# ── 带超时熔断的批量下载 ───────────────────────────────────────────────────
def _batch_download_with_timeout(codes: list, period: str = '1d',
                                  timeout_sec: float = 60.0) -> bool:
    """
    在独立线程中批量执行 download_history_data2，
    超时未完成则放弃（不阻塞主线程）。
    注意：此版本 QMT 的 download_history_data2 不支持 count 参数，
    全量下载后由 get_market_data_ex(count=N) 截取所需长度。
    返回 True = 正常完成；False = 超时/失败（降级读本地缓存）。
    """
    done_flag = [False]

    def _worker():
        try:
            xtdata.download_history_data2(codes, period=period)
            done_flag[0] = True
        except Exception as e:
            print(f"  ⚠️  批量下载异常: {e}")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    if not done_flag[0]:
        print(f"  ⚡ 批量下载超时（>{timeout_sec}s），降级读本地缓存继续执行...")
    return done_flag[0]


# ── QMT 连接预检 ──────────────────────────────────────────────────────────────
def _check_qmt_alive(timeout_sec: float = 5.0) -> bool:
    """用极轻量查询探测 miniQMT 是否在线，5s 超时。"""
    result_box = [False]

    def _probe():
        try:
            xtdata.get_market_data_ex(
                field_list=['close'], stock_list=['510300.SH'], period='1d', count=1
            )
            result_box[0] = True
        except Exception:
            pass

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    return result_box[0]


# ── 全市场 ETF 采集（含 52x 科创系列）────────────────────────────────────────
def fetch_all_market_etfs() -> list:
    """
    优先使用 get_stock_list_in_sector("场内基金") 获取全量场内基金，
    再用前缀白名单过滤出纯 ETF（剔除 LOF / 分级基金等）。
    备用方案：分交所拉取再合并（保留以防板块接口失效）。
    """
    print("📡 正在向 QMT 索要全市场场内基金代码矩阵...")

    # 此版本 miniQMT 板块名使用英文缩写 'SH' / 'SZ'（中文名如"上交所"失效）
    # 先尝试场内基金板块（若 QMT 版本支持）
    for sector_name in ["场内基金", "ETF", "基金"]:
        try:
            fund_list = xtdata.get_stock_list_in_sector(sector_name)
            if fund_list:
                etf_list = [code for code in fund_list if code.startswith(_ETF_PREFIXES)]
                if etf_list:
                    print(f"  ✅ [{sector_name}] 板块返回 {len(fund_list)} 只，过滤后 ETF: {len(etf_list)} 只")
                    return etf_list
        except Exception:
            pass

    # 主方案：按交所代号拉取全量（SH/SZ 是此 QMT 版本正确板块名）
    print("  🔄 使用 SH/SZ 板块拉取全量标的...")
    sh_list = xtdata.get_stock_list_in_sector('SH') or []
    sz_list = xtdata.get_stock_list_in_sector('SZ') or []

    # 沪市 ETF: 51x / 52x / 58x（含科创板 ETF）
    sh_etfs = [code for code in sh_list if code.startswith(('51', '52', '58'))]
    # 深市 ETF: 15x（含创业板 ETF、跨境 ETF）
    sz_etfs = [code for code in sz_list if code.startswith('15')]

    etf_list = sh_etfs + sz_etfs
    print(f"  ✅ 锁定 {len(etf_list)} 只 ETF "
          f"(SH: {len(sh_etfs)}, SZ: {len(sz_etfs)})")
    return etf_list


# ── 主选股流程（达尔文机制）─────────────────────────────────────────────────
def run_darwin_selection(top_n: int = 200, final_seats: int = 60,
                          output_file: str = ".state/oracle_v2_universe.json"):
    """
    达尔文三轮淘汰：
      Round 1 -- 流动性初筛（全量 ETF → Top200，5天成交额）
               底层标的通过率分1/3，200只候选可确保最终 60 席帱实
      Round 2 -- 深度体检（Top200 → 60席，250天 K 线）
      Round 3 -- 物理指标双过滤（乖离率 + 波动率）
    """


    # ── 连接预检 ────────────────────────────────────────────────────────────
    print("📡 预检 miniQMT 连接状态...")
    if not _check_qmt_alive(timeout_sec=5.0):
        print("🔥 物理熔断：miniQMT 离线或无响应（5s超时）。")
        print("   请先启动迅投 miniQMT 客户端并登录账号，再运行本脚本。")
        return []

    print("✅ miniQMT 在线，启动达尔文选种程序...")

    # ── Round 0：获取全量 ETF ─────────────────────────────────────────────
    all_etfs = fetch_all_market_etfs()
    if not all_etfs:
        print("🔥 无法获取 ETF 列表，退出！")
        return []

    # ── Round 1：流动性初筛（5天, 带超时熔断）──────────────────────────────
    print(f"\n⏳ Round 1 — 全量 {len(all_etfs)} 只 ETF 流动性初筛（5天成交额）...")
    print("   注意：首次运行下载可能需要 30-60 秒，盘后/周末执行效果最佳")
    _batch_download_with_timeout(all_etfs, period='1d', timeout_sec=90.0)

    vol_data = xtdata.get_market_data_ex(
        field_list=['amount'], stock_list=all_etfs, period='1d', count=5
    )

    liquidity_rank = []
    for code in all_etfs:
        if code in vol_data and not vol_data[code].empty:
            avg_amount = vol_data[code]['amount'].mean()
            if avg_amount > 0:
                liquidity_rank.append((code, avg_amount))

    liquidity_rank.sort(key=lambda x: x[1], reverse=True)
    top_candidates = [item[0] for item in liquidity_rank[:top_n]]
    # 保留流动性数值字典，供落盘时写入指标
    liquidity_dict = {code: amt for code, amt in liquidity_rank}

    print(f"🩸 Round 1 完毕：{len(all_etfs)} → {len(top_candidates)} 只流动性猛兽")
    if not top_candidates:
        print("🔥 流动性数据为空，可能 QMT 本地缓存不足，退出！")
        return []

    # ── 输出 Top 100 流动性 CSV（含中文名称）────────────────────────────────
    top100_list = liquidity_rank[:100]
    print(f"\n🏅 正在获取 Top {len(top100_list)} 只 ETF 中文名称...")
    top100_codes = [c for c, _ in top100_list]
    name_map = _get_etf_names(top100_codes)

    top100_rows = []
    for rank, (code, amt) in enumerate(top100_list, 1):
        top100_rows.append({
            "排名":       rank,
            "代码":       code,
            "名称":       name_map.get(code, ''),
            "5日均成交额（亿）": round(amt / 1e8, 2),
        })

    top100_df = pd.DataFrame(top100_rows)
    csv_out = os.path.join(_PROJECT_DIR, _TOP100_CSV_RELPATH)
    os.makedirs(os.path.dirname(csv_out), exist_ok=True)
    top100_df.to_csv(csv_out, index=False, encoding='utf-8-sig')
    print(f"   ✅ Top 100 流动性榜已写入: {csv_out}")

    # 打印 Top10 作为诊断日志
    print("\n📊 流动性 Top 10 预览:")
    for i, (code, amt) in enumerate(liquidity_rank[:10], 1):
        name = name_map.get(code, '')
        print(f"   {i:2d}. {code}  {name:12s}  日均成交额: {amt/1e8:.2f} 亿")

    # ── Round 2：深度体检（250天, 带超时熔断）──────────────────────────────
    print(f"\n⏳ Round 2 — {len(top_candidates)} 只标的深度体检（250天 K 线）...")
    _batch_download_with_timeout(top_candidates, period='1d', timeout_sec=60.0)

    market_data = xtdata.get_market_data_ex(
        field_list=['close', 'amount'],
        stock_list=top_candidates,
        period='1d',
        count=250
    )

    # ── Round 3：物理指标双过滤 ──────────────────────────────────────────────
    refined_pool = []
    rejected_log = []

    for code in top_candidates:
        if code not in market_data or market_data[code].empty:
            rejected_log.append(f"{code}: 无 K 线数据")
            continue

        df = market_data[code]

        # 过滤次新（< 120 天历史）
        if len(df) < 120:
            rejected_log.append(f"{code}: 次新（仅{len(df)}天）")
            continue

        # ── 计算所有指标（无论通过与否都记录，方便诊断）─────────────────
        avg_amount_5d   = liquidity_dict.get(code, 0)
        ma250           = df['close'].mean()
        current_price   = df['close'].iloc[-1]
        bias            = abs(current_price - ma250) / ma250 if ma250 > 0 else 0
        returns         = df['close'].pct_change().dropna()
        volatility      = returns.std()
        history_days    = len(df)
        # ATR-14（日振幅均值，用于 T0 spread_pct 参考）
        if 'high' in df.columns and 'low' in df.columns:
            atr14 = (df['high'] - df['low']).tail(14).mean()
            atr14_pct = atr14 / current_price if current_price > 0 else 0
        else:
            atr14_pct = volatility * 1.6   # 用波动率估算（高斯近似）

        # ── 物理指标 A：均值引力检测 ─────────────────────────────────────
        if current_price <= 0:
            rejected_log.append(f"{code}: 价格无效")
            continue
        if bias > 0.40:
            rejected_log.append(f"{code}: 引力失控 (乖离{bias*100:.1f}%)")
            print(f"🚫 剔除引力失控标的: {code} (乖离率 {bias*100:.1f}%)")
            continue

        # ── 物理指标 B：僵尸波动过滤 ─────────────────────────────────────
        if volatility < 0.005:
            rejected_log.append(f"{code}: 死水 (波动{volatility*100:.3f}%)")
            print(f"🚫 剔除死水标的: {code} (日波动 {volatility*100:.3f}%)")
            continue

        refined_pool.append({
            "code":           code,
            "current_price":  round(float(current_price), 4),
            "avg_amount_5d":  round(float(avg_amount_5d) / 1e8, 2),   # 单位：亿元
            "ma250":          round(float(ma250), 4),
            "bias_pct":       round(float(bias) * 100, 2),             # 单位：%
            "volatility_pct": round(float(volatility) * 100, 4),      # 单位：%
            "atr14_pct":      round(float(atr14_pct) * 100, 4),        # 单位：%
            "history_days":   history_days,
            "updated_at":     pd.Timestamp.now().strftime('%Y-%m-%d')
        })

        # 满席即止（保留流动性最强的 final_seats 只）
        if len(refined_pool) >= final_seats:
            break

    # ── 落盘输出（带完整指标的 JSON）─────────────────────────────────────────
    # 支持绝对路径和相对路径（相对路径则拼接项目根目录）
    if os.path.isabs(output_file):
        out_path = output_file
    else:
        out_path = os.path.join(_PROJECT_DIR, output_file)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    output_data = {
        "meta": {
            "generated_at":   pd.Timestamp.now().strftime('%Y-%m-%d %H:%M'),
            "total_scanned":  len(all_etfs),
            "liquidity_top_n": len(top_candidates),
            "final_seats":    len(refined_pool),
            "filters": {
                "max_bias_pct":      40.0,
                "min_volatility_pct": 0.5,
                "min_history_days":  120
            }
        },
        "universe": refined_pool
    }

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=4, ensure_ascii=False)

    # ── 同步输出最终宇宙 CSV（含名称，供人类查看）──────────────────────────
    universe_csv_path = out_path.replace('.json', '.csv')
    universe_codes = [rec['code'] for rec in refined_pool]
    universe_name_map = _get_etf_names(universe_codes)

    universe_rows = []
    for i, rec in enumerate(refined_pool, 1):
        universe_rows.append({
            "排名":           i,
            "代码":           rec['code'],
            "名称":           universe_name_map.get(rec['code'], ''),
            "现价":           rec['current_price'],
            "5日均成交额（亿）":  rec['avg_amount_5d'],
            "MA250":          rec['ma250'],
            "乖离率_pct":      rec['bias_pct'],
            "波动率_pct":      rec['volatility_pct'],
            "ATR14_pct":      rec['atr14_pct'],
            "历史天数":        rec['history_days'],
            "更新日期":        rec['updated_at'],
        })

    universe_df = pd.DataFrame(universe_rows)
    universe_df.to_csv(universe_csv_path, index=False, encoding='utf-8-sig')

    print(f"\n{'='*60}")
    print(f"🏆 达尔文机制执行完毕！")
    print(f"   全量 ETF: {len(all_etfs)} 只")
    print(f"   流动性初筛后: {len(top_candidates)} 只")
    print(f"   体检通过（最终宇宙）: {len(refined_pool)} 只")
    print(f"   JSON  → {out_path}")
    print(f"   CSV   → {universe_csv_path}")
    print(f"   Top100 → {csv_out}")
    print(f"{'='*60}")
    print(f"\n🎯 最终 {len(refined_pool)} 席宇宙 (按流动性排序):")
    print(f"   {'排名':4s}  {'代码':12s}  {'日均成交':>10s}  {'波动率':>8s}  {'乖离率':>7s}  {'ATR14':>8s}")
    print(f"   {'----':4s}  {'------':12s}  {'--------':>10s}  {'------':>8s}  {'------':>7s}  {'-----':>8s}")
    for i, rec in enumerate(refined_pool, 1):
        print(f"   {i:4d}. {rec['code']:12s}  "
              f"{rec['avg_amount_5d']:>8.2f}亿  "
              f"{rec['volatility_pct']:>7.3f}%  "
              f"{rec['bias_pct']:>6.1f}%  "
              f"{rec['atr14_pct']:>7.3f}%")

    return refined_pool


if __name__ == "__main__":
    run_darwin_selection()