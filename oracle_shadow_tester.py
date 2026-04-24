# ==========================================
# 部署在 Quant-PC (Z690)
# 文件名: oracle_shadow_tester.py
# 职责: 14:30 提取 QMT 真实 K 线 -> 请求预测 -> 写入影子账本
#
# [FIX 2026-04-09] 修复 download_history_data2 阻塞死锁问题：
#   - download_history_data2 无超时机制，QMT未连接时永久挂起
#   - 改为：线程 + 5s 超时 物理熔断
#   - 降级策略：下载失败则直接读本地缓存（get_market_data_ex）
#   - 增加 QMT 连接预检：先用 get_market_data_ex 空调用探测连接
# ==========================================
import requests
import pandas as pd
from datetime import datetime
import os
import threading
import time


# ── QMT 连接预检 ──────────────────────────────────────────────────────────────
def _check_qmt_alive(timeout_sec: float = 5.0) -> bool:
    """
    用 get_market_data_ex 对单只标的做一次极轻量查询，
    判断 miniQMT 进程是否在线。
    返回 True = 在线；False = 离线或超时。
    """
    result_box = [False]

    def _probe():
        try:
            from xtquant import xtdata
            data = xtdata.get_market_data_ex(
                field_list=['close'],
                stock_list=['510300.SH'],
                period='1d',
                count=1
            )
            # 只要能返回（即使空）就说明进程在线
            result_box[0] = True
        except Exception:
            result_box[0] = False

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    return result_box[0]


# ── 带超时熔断的 download_history_data2 ───────────────────────────────────────
def _download_with_timeout(code: str, timeout_sec: float = 5.0) -> bool:
    """
    在独立线程中执行 download_history_data2，
    超时未完成则放弃（不阻塞主线程）。
    返回 True = 正常完成；False = 超时/失败。
    """
    done_flag = [False]

    def _worker():
        try:
            from xtquant import xtdata
            xtdata.download_history_data2([code], period='1d')
            done_flag[0] = True
        except Exception as e:
            print(f"  ⚠️  {code} 下载异常: {e}")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    return done_flag[0]


# ── 核心数据抽取函数 ──────────────────────────────────────────────────────────
def get_real_ammo_from_qmt(etf_list, count=120):
    from xtquant import xtdata

    print("⏳ 正在调用 miniQMT 底层接口抽取真实 K 线...")

    # 第一步：逐标的尝试增量下载（带 5 秒物理熔断）
    # 超时的标的直接降级，用本地缓存读取，不影响其他标的
    for code in etf_list:
        ok = _download_with_timeout(code, timeout_sec=5.0)
        if not ok:
            print(f"  ⚡ {code} 下载超时/跳过 → 降级读本地缓存")

    # 第二步：统一从本地内存提取数据（含今日动态K线）
    market_data = xtdata.get_market_data_ex(
        field_list=['close'],
        stock_list=etf_list,
        period='1d',
        count=count
    )

    real_payload = []
    for code in etf_list:
        if code in market_data and not market_data[code].empty:
            closes = market_data[code]['close'].tolist()
            # 物理风控：只投喂长度达标的数据
            if len(closes) >= 32:
                real_payload.append({
                    "code": code,
                    "history_prices": closes
                })
            else:
                print(f"  ⚠️  {code} 本地数据不足 32 根 K 线（实际 {len(closes)} 根），跳过")
        else:
            print(f"  ⚠️  警告: 无法从 QMT 获取 {code} 的物理数据")

    return real_payload


# ── 主流程 ────────────────────────────────────────────────────────────────────
def run_shadow_test():
    target_date = datetime.now().strftime('%Y-%m-%d')
    oracle_url = "http://10.10.8.20:8000/predict_batch"
    shadow_db_file = "shadow_predictions.csv"

    core_universe = [
        "510300.SH", "510500.SH", "159845.SZ", "159502.SZ",  # 宽基
        "513100.SH", "518880.SH", "512760.SH", "512000.SH"   # 纳指/黄金/芯片/券商
    ]

    print(f"[{target_date} 14:30] 📡 预检 miniQMT 连接状态...")

    # ── 连接预检（5 秒超时）──────────────────────────────
    if not _check_qmt_alive(timeout_sec=5.0):
        print("🔥 物理熔断：miniQMT 进程离线或无响应（5s超时）。")
        print("   请先启动迅投 miniQMT 客户端并登录账号，再运行本脚本。")
        return

    print("✅ miniQMT 在线，开始装填实弹...")

    # 1. 获取真弹药
    test_universe = get_real_ammo_from_qmt(core_universe, count=120)

    if not test_universe:
        print("🔥 弹药库为空，无法向预言机发起请求！请检查 QMT 数据下载状态。")
        return

    payload = {"batch_data": test_universe}
    print(f"📡 数据抽取完毕，共 {len(test_universe)} 只标的。正在唤醒 vm200...")

    # 2. 发起跨设备张量计算请求（15 秒超时物理锁）
    try:
        response = requests.post(oracle_url, json=payload, timeout=15.0)

        if response.status_code == 200:
            predictions = response.json().get("predictions", {})
            print("✅ 预言机已成功返回真实分布，正在准备落盘...")

            # 3. 数据重组与物理落盘
            records = []
            for code, data in predictions.items():
                records.append({
                    "date": target_date,
                    "code": code,
                    "current_price": data['current_price'],
                    "Q50_median": data['q50'],
                    "Q80_optimistic": data['q80'],
                    "Q20_pessimistic": data['q20'],
                    "odds_ratio": data['odds_ratio']
                })

            df_new = pd.DataFrame(records)

            # 剔除无效预测（赔率为 0）并按赔率倒序排列
            df_new = df_new[df_new['odds_ratio'] != 0]
            df_new = df_new.sort_values(by='odds_ratio', ascending=False)

            print("\n🏆 今日预言机推荐 TOP 3 (实弹判决):")
            print(df_new[['code', 'current_price', 'odds_ratio', 'Q50_median']].head(3))

            # 写入本地 CSV 影子数据库（追加模式）
            if not os.path.exists(shadow_db_file):
                df_new.to_csv(shadow_db_file, index=False, encoding='utf-8-sig')
            else:
                df_new.to_csv(shadow_db_file, mode='a', header=False, index=False, encoding='utf-8-sig')

            print(f"\n💾 真实数据已封存 → {shadow_db_file}")

        else:
            print(f"❌ 预言机响应异常: HTTP {response.status_code}")

    except requests.exceptions.Timeout:
        print("🔥 物理熔断：预言机计算超时 (超过15秒)，检查 vm200 算力负载。")
    except Exception as e:
        print(f"🔥 物理连接断裂: {e}")


if __name__ == '__main__':
    run_shadow_test()