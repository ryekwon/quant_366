#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fat Fish Master — 胖鱼波段大脑 v2 (Data Fusion Edition)
调度时间: 每日 14:40（由 autopilot_master.py 触发）

核心架构：离线历史 + 实时 Tick 缝合 (Data Fusion)
  T-1 历史数据 (Parquet) + 14:40 实时 Tick → 缝合今日行 → 计算因子
  → fat_fish_orders.yaml (买/卖指令，供执行器 14:50 消费)
  → fat_fish_slots.yaml  (持仓槽位状态机)
"""

import os
import time
import yaml
import json
import requests
import numpy as np
import pandas as pd
from datetime import datetime, date
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─── N8N Webhook（策略三件套之一，按 quant-v4-patterns §13 规范）──────────────
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")

def send_n8n_alert(title: str, message: str) -> None:
    """N8N 推送，失败静默（不阻断主逻辑）。timeout=5sec。"""
    if not N8N_WEBHOOK_URL:
        return
    try:
        requests.post(N8N_WEBHOOK_URL,
                      json={"title": title, "message": message},
                      timeout=5)
    except Exception:
        pass  # 推送失败不影响交易逻辑

# xtquant 标准导入（必须与 xtconstant 同行，见 quant-safe-patterns）
from xtquant import xtdata

# ================= 物理路径与全局参数 =================
DAILY_DATA_DIR = r"Z:\QuantpC_Workspace\Data\Market_Daily"
ETF_LIST_PATH  = r"Z:\QuantpC_Workspace\Data\refined_etf_list.yaml"
STATE_DIR      = r"Z:\QuantpC_Workspace\Quant_Pilot\.state"
SLOTS_FILE     = os.path.join(STATE_DIR, "fat_fish_slots.yaml")
ORDERS_FILE    = os.path.join(STATE_DIR, "fat_fish_orders.yaml")
SIGNALS_FILE   = os.path.join(STATE_DIR, "fat_fish_signals.json")  # 买入信号中间文件（大脑写，火炮读）

TOTAL_SLOTS      = 3
CAPITAL_PER_SLOT = 20000.0

# 成交量外推参数：14:40 已过 220 分钟，全天 240 分钟
ELAPSED_MINUTES = 220
TOTAL_MINUTES   = 240
VOL_SCALE       = TOTAL_MINUTES / ELAPSED_MINUTES   # ≈ 1.091


# ─── 工具：ETF 裸码 → 带交易所后缀的 xtquant 标准代码 ─────────────────────
def _etf_code_to_xt(bare_code: str) -> str:
    """
    refined_etf_list.yaml 存裸码（如 '511360'），parquet 文件名为 511360_SH.parquet。
    SZ 前缀: 159xxx / 151xxx 等
    """
    sz_prefixes = ('159', '151', '160', '161', '162', '163', '164', '165',
                   '166', '167', '168', '169')
    return bare_code + ('.SZ' if bare_code.startswith(sz_prefixes) else '.SH')


# ================= 状态机文件 I/O =================

def ensure_state_files():
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(SLOTS_FILE):
        with open(SLOTS_FILE, 'w', encoding='utf-8') as f:
            yaml.dump({'slots': {}}, f)


def load_slots() -> dict:
    with open(SLOTS_FILE, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    return data.get('slots', {}) if data else {}


def save_slots_only(slots: dict):
    """[大脑唯一合法写槽位入口] 仅更新已持仓槽位的棘轮止损线，禁止写入新槽位。"""
    with open(SLOTS_FILE, 'w', encoding='utf-8') as f:
        yaml.dump({'slots': slots}, f, allow_unicode=True)


def save_orders(orders: dict):
    """写入卖出指令文件（供执行器消费）"""
    with open(ORDERS_FILE, 'w', encoding='utf-8') as f:
        yaml.dump(orders, f, allow_unicode=True)


def save_signals(signals: list):
    """[信号隔离] 将买入信号写入中间通信文件，绝不写 fat_fish_slots.yaml。
    格式：[{code, shares, ref_price, atr_14, signal_time}, ...]
    """
    import json
    payload = {
        'generated_at': datetime.now().isoformat(),
        'signals': signals,
    }
    tmp = SIGNALS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SIGNALS_FILE)  # 原子替换，防止写到一半被执行器读到


# ================= 数据层 =================

def load_history(xt_code: str) -> pd.DataFrame | None:
    """
    读取 T-1 历史 Parquet，返回以 date 为 index 的 DataFrame。
    列：open, high, low, close, vol, amount, pct_change
    至少需要 80 行（保证 RSRS rolling(60) 有效）。
    """
    file_name = xt_code.replace('.', '_') + '.parquet'
    file_path = os.path.join(DAILY_DATA_DIR, file_name)
    if not os.path.exists(file_path):
        return None
    try:
        df = pd.read_parquet(file_path)
        df.columns = [col.lower() for col in df.columns]
        # parquet 列名是 'volume'，统一改为 'vol'
        if 'volume' in df.columns:
            df.rename(columns={'volume': 'vol'}, inplace=True)
        df = df.sort_index(ascending=True)
        return df if len(df) >= 80 else None
    except Exception:
        return None


def fuse_today_tick(df: pd.DataFrame, tick: dict) -> pd.DataFrame | None:
    """
    数据缝合：将实时 Tick 作为"今日预估行"追加到历史 DataFrame 末尾。

    缝合规则：
      close / high / low = lastPrice（14:40 快照价格，用于 MA / 突破判断）
      vol    = tick.volume  × (240/220)   （全天成交量线性外推）
      amount = tick.amount  × (240/220)   （全天成交额线性外推）

    返回缝合后的 DataFrame，若 Tick 无效则返回原始历史（降级模式）。
    """
    last_price = tick.get('lastPrice', 0)
    cur_volume = tick.get('volume', 0)
    cur_amount = tick.get('amount', 0)

    if last_price <= 0 or cur_volume <= 0:
        # Tick 无效（停牌/无行情），降级为纯历史计算
        return df

    est_vol    = cur_volume * VOL_SCALE
    est_amount = cur_amount * VOL_SCALE

    today = date.today()

    # 构造今日缝合行（index 类型与 parquet 保持一致：datetime.date object）
    new_row = pd.DataFrame({
        'open':       [last_price],
        'high':       [last_price],
        'low':        [last_price],
        'close':      [last_price],
        'vol':        [est_vol],
        'amount':     [est_amount],
        'pct_change': [0.0],       # 占位，不参与任何因子计算
    }, index=[today])

    # 对齐列（防止 parquet 有额外列）
    new_row = new_row.reindex(columns=df.columns, fill_value=0.0)

    # 若今日已在 DataFrame 中（盘中曾下载过），先删后追
    if today in df.index:
        df = df.drop(today)

    return pd.concat([df, new_row])


# ================= 因子计算 =================

def calc_fat_fish_factors(df: pd.DataFrame) -> pd.Series | None:
    """
    向量化计算所有突破与风控因子，返回最后一行 Series。
    若关键字段仍为 NaN，返回 None（触发跳过，不参与排序）。
    """
    df = df.copy()

    # ─── ATR-14 （移动止损基准） ──────────────────────────────────────
    df['prev_close'] = df['close'].shift(1)
    tr1 = df['high'] - df['low']
    tr2 = (df['high'] - df['prev_close']).abs()
    tr3 = (df['low']  - df['prev_close']).abs()
    df['tr']     = np.maximum(tr1, np.maximum(tr2, tr3))
    df['atr_14'] = df['tr'].rolling(14).mean()

    # ─── 突破因子（20 日新高 + 放量） ────────────────────────────────
    df['max_high_20'] = df['high'].rolling(20).max().shift(1)
    df['ma_amount_20'] = df['amount'].rolling(20).mean().shift(1)  # 🌟 量纲统一：用成交额替代成交量
    df['ma_20']       = df['close'].rolling(20).mean()
    df['ma_10']       = df['close'].rolling(10).mean()

    # ─── RSRS Z-score 情绪高潮因子 ───────────────────────────────────
    window_rsrs = 18
    cov_hl = df['high'].rolling(window_rsrs).cov(df['low'])
    var_l  = df['low'].rolling(window_rsrs).var()
    df['beta']      = cov_hl / var_l.replace(0, np.nan)   # 除零保护
    df['beta_mean'] = df['beta'].rolling(60).mean()
    df['beta_std']  = df['beta'].rolling(60).std()
    df['z_score']   = (df['beta'] - df['beta_mean']) / df['beta_std'].replace(0, np.nan)
    df['recent_climax'] = df['z_score'].rolling(5).max() > 1.2

    # ─── 动能截面分（横截面排序用） ──────────────────────────────────
    df['momentum_score'] = (
        (df['close'] / df['prev_close'] - 1)
        * (df['amount'] / df['ma_amount_20'].replace(0, np.nan))  # 🌟 量纲统一：成交额比
    )

    row = df.iloc[-1]

    # 关键字段 NaN 守门：返回 None 触发跳过
    must_valid = ['close', 'atr_14', 'max_high_20', 'ma_amount_20',
                  'ma_20', 'ma_10', 'momentum_score']
    if any(pd.isna(row[f]) for f in must_valid):
        return None

    return row


# ================= 主逻辑 =================

def run_master():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🧠 胖鱼波段大脑 v2 (Data Fusion) 启动...")
    ensure_state_files()

    # ── 🧹 防御性清除：每次大脑启动先删除上次遗留的 signals.json ──────
    # 原因：若执行器上次因 Bug 提前退出（未调用 purge_signals），旧信号会
    # 在次日被误读。大脑是每日信号的唯一合法来源，启动即意味着上周期作废。
    if os.path.exists(SIGNALS_FILE):
        try:
            os.remove(SIGNALS_FILE)
            print(f"🧹 [信号自愈] 清除上次遗留的旧 signals 文件（防执行器读到过期信号）")
        except Exception as e_purge:
            print(f"   ⚠️ 旧 signals 清除失败: {e_purge}（继续运行）")

    # ─── 读取 ETF 池 ─────────────────────────────────────────────────
    with open(ETF_LIST_PATH, 'r', encoding='utf-8') as f:
        raw_data = yaml.safe_load(f)
    # ⚠️ YAML 顶层结构为 {'etf_list': [...]}, 不是裸列表
    raw_list = raw_data.get('etf_list', []) if isinstance(raw_data, dict) else (raw_data or [])
    etf_pool = [_etf_code_to_xt(item['code']) for item in raw_list if 'code' in item]
    print(f"🎯 载入精选猎物池: {len(etf_pool)} 只")

    # ─── 批量拉取全池实时 Tick（一次网络调用，效率最优）────────────
    print(f"⚡ [Data Fusion] 批量拉取 {len(etf_pool)} 只 ETF 实时 Tick...")
    try:
        tick_map = xtdata.get_full_tick(etf_pool)
        tick_ok  = len(tick_map)
        print(f"   ✅ 成功获取 {tick_ok} 只 Tick（{len(etf_pool) - tick_ok} 只无行情）")
    except Exception as e:
        print(f"   ⚠️ Tick 批量拉取失败: {e}，降级为纯历史模式（信号可能滞后）")
        tick_map = {}

    current_slots = load_slots()
    orders        = {'buy': [], 'eod_sell': []}

    # ================= 阶段 1：构建融合数据映射 + 全局宽度熔断 =========
    market_breadth_count = 0
    valid_count          = 0
    latest_data_map      = {}

    for code in etf_pool:
        # 1a. 读取历史 Parquet (T-1)
        df_hist = load_history(code)
        if df_hist is None:
            continue

        # 1b. 缝合今日 Tick（14:40 实时快照 + 成交量外推）
        tick = tick_map.get(code, {})
        df_fused = fuse_today_tick(df_hist, tick)

        # 1c. 计算因子（最后一行 = 今日融合数据）
        row = calc_fat_fish_factors(df_fused)
        if row is None:
            continue

        latest_data_map[code] = row
        if row['close'] > row['ma_20']:
            market_breadth_count += 1
        valid_count += 1

    breadth_ratio = market_breadth_count / valid_count if valid_count > 0 else 0
    global_veto   = breadth_ratio < 0.20
    if global_veto:
        print(f"🚨 [全局熔断] 市场宽度 {breadth_ratio*100:.1f}% < 20%，系统物理锁死开仓接口！")
        send_n8n_alert(
            "🚨 胖鱼全局熔断",
            f"市场宽度 {breadth_ratio*100:.1f}%（{market_breadth_count}/{valid_count} 只站上 MA20），已低于 20% 阈值，\n当日胖鱼开仓接口物理锁死，不产生任何新买入信号。"
        )
    else:
        print(f"✅ [环境安全] 市场宽度 {breadth_ratio*100:.1f}%（{market_breadth_count}/{valid_count} 只站上 MA20），允许右侧突击。")

    # ================= 阶段 2：已有槽位清算与防线更新 =================
    slots_to_remove = []

    for code, state in current_slots.items():
        if code not in latest_data_map:
            print(f"⚠️ [{code}] 今日数据不可用，保持昨日止损线不动。")
            continue

        today_data = latest_data_map[code]
        atr_14     = today_data['atr_14']

        # 棘轮机制：止损线只涨不降
        new_high = max(float(state.get('highest_price', 0)), float(today_data['high']))
        current_slots[code]['highest_price']   = round(new_high, 3)
        current_slots[code]['stop_loss_price'] = round(float(new_high - 2 * atr_14), 3)

        # 尾盘逃顶：情绪高潮后 MA10 跌破 → 发出卖出指令
        trend_break   = today_data['close'] < today_data['ma_10']
        recent_climax = bool(today_data.get('recent_climax', False))
        if recent_climax and trend_break:
            print(f"🔪 [右侧逃顶] {code} 触发情绪高潮后 MA10 破位，释放槽位！")
            orders['eod_sell'].append({'code': code, 'reason': 'RSRS_MA10_Break'})
            slots_to_remove.append(code)
            send_n8n_alert(
                f"🔪 胖鱼右侧逃顶 {code}",
                f"标的 {code} 触发 RSRS 情绪高潮（近5日Z-score>1.2）+ MA10 破位，\n14:50 执行器将执行尾盘清仓指令（RSRS_MA10_Break）。"
            )

    for code in slots_to_remove:
        del current_slots[code]

    # ================= 阶段 3：空闲槽位填装（横截面绞杀） =================
    available_slots = TOTAL_SLOTS - len(current_slots)
    print(f"📦 当前占用槽位: {len(current_slots)}/{TOTAL_SLOTS} | 空闲: {available_slots}")

    if available_slots > 0 and not global_veto:
        candidates = []
        for code, data in latest_data_map.items():
            if code in current_slots or code in slots_to_remove:
                continue

            # 物理开仓三铁律 (成交额量纲，跨数据源绝对统一)
            cond_breakout = data['close'] > data['max_high_20']                          # 今日突破 20 日新高
            cond_vol      = data['amount'] > 1.5 * data['ma_amount_20']                 # 🌟 成交额放量 1.5 倍
            cond_atr_ext  = (data['close'] - data['ma_20']) <= 2.5 * data['atr_14']    # 🌟 乖离防线放宽至 2.5 ATR

            # 👁️ [诊断探针] 突破新高但死在其他关卡的标的，打印原因便于复盘
            if cond_breakout and not (cond_vol and cond_atr_ext):
                reason = []
                if not cond_vol:
                    ratio = data['amount'] / data['ma_amount_20'] if data['ma_amount_20'] > 0 else 0
                    reason.append(f"未放量(额比={ratio:.2f}x)")
                if not cond_atr_ext:
                    diff  = data['close'] - data['ma_20']
                    limit = 2.5 * data['atr_14']
                    reason.append(f"偏离过大(差={diff:.3f}, 限={limit:.3f})")
                print(f"   🔍 [未通过] {code}: 突破新高, 但 {' | '.join(reason)}")

            if cond_breakout and cond_vol and cond_atr_ext:
                score = data['momentum_score']
                if pd.isna(score):
                    continue
                candidates.append({
                    'code':     code,
                    'close':    float(data['close']),
                    'atr_14':   float(data['atr_14']),
                    'momentum': float(score),
                })

        candidates.sort(key=lambda x: x['momentum'], reverse=True)

        # =====================================================
        # [DISABLED] 预言机测谎模块（待 TimesFM 服务 http://10.10.8.20:8000 验证后解除注释）
        # oracle_validator.py 回测评分通过后再激活此模块
        # =====================================================
        # ORACLE_URL   = "http://10.10.8.20:8000/predict_batch"
        # ORACLE_MIN_ODDS = 1.2   # 赔率门槛：低于此值 = 讯多证据不足，一票否决
        #
        # if candidates:
        #     print(f"🕵️ 物理初筛发现 {len(candidates)} 只突破猎物，正在提交预言机进行测谎...")
        #     payload = []
        #     for cand in candidates:
        #         code = cand['code']
        #         try:
        #             raw = xtdata.get_market_data_ex(
        #                 field_list=['close'], stock_list=[code],
        #                 period='1d', count=119
        #             ).get(code)
        #             if raw is not None and not raw.empty:
        #                 closes = raw['close'].tolist()
        #                 tick  = tick_map.get(code, {})
        #                 lp    = tick.get('lastPrice', 0)
        #                 closes.append(lp if lp > 0 else (closes[-1] if closes else 0))
        #                 payload.append({"code": code, "history_prices": closes})
        #             else:
        #                 print(f"  ⚠️ {code} 本地缓存无数据，按假突破处理")
        #         except Exception as e_tick:
        #             print(f"  ⚠️ {code} 数据组装失败: {e_tick}，按假突破处理")
        #
        #     try:
        #         resp = requests.post(ORACLE_URL, json={"batch_data": payload}, timeout=10)
        #         predictions = resp.json().get("predictions", {})
        #         vetted = []
        #         for cand in candidates:
        #             code = cand['code']
        #             pred = predictions.get(code)
        #             if not pred:
        #                 print(f"  ⚠️ {code} 测谎失败（预言机无返回），按假突破处理。")
        #                 continue
        #             odds = pred.get('odds_ratio', 0.0)
        #             if odds < ORACLE_MIN_ODDS:
        #                 print(f"  🚨 [假突破拦截] {code} 赔率 {odds:.2f} < {ORACLE_MIN_ODDS}，一票否决！")
        #             else:
        #                 print(f"  ✅ [测谎通过] {code} 真突破确认（赔率 {odds:.2f}）")
        #                 cand['odds_ratio'] = round(odds, 4)
        #                 vetted.append(cand)
        #         candidates = vetted
        #         print(f"  🔬 测谎结果: {len(candidates)} 只通过高维验证")
        #     except requests.exceptions.Timeout:
        #         print("🔥 [测谎超时] 预言机 >10s 无响应，取消本次右侧开仓。")
        #         candidates = []
        #     except Exception as e_oracle:
        #         print(f"🔥 预言机连接失败，取消本次右侧开仓：{e_oracle}")
        #         candidates = []

        # === 测谎已禁用，以物理初筛 candidates 按动能排序取前 N ===
        candidates.sort(key=lambda x: x['momentum'], reverse=True)
        selected = candidates[:available_slots]

        if not selected:
            print("📭 [扫描结束] 今日全池无标的满足突破 + 放量 + 未延伸三铁律。")
            send_n8n_alert(
                "📭 胖鱼今日无信号",
                f"全池 {valid_count} 只 ETF 扫描完毕，无标的同时满足：\n① 突破20日新高 ② 成交额放量1.5倍 ③ 乖离未超2.5ATR\n市场宽度: {breadth_ratio*100:.1f}%"
            )
        else:
            buy_signals = []
            for tgt in selected:
                code = tgt['code']
                c_price    = tgt['close']
                buy_shares = int((CAPITAL_PER_SLOT / c_price) / 100) * 100

                if buy_shares >= 100:
                    print(f"🎯 [信号生成] {code} 动能分 {tgt['momentum']:.4f}，≈{buy_shares}股。")
                    print(f"   ⚠️ [物理隔离] 信号仅写入 fat_fish_signals.json，禁止提前写槽位！")
                    # ✅ 只写信号，绝对禁止修改 current_slots（火炮成交后才有资格写）
                    buy_signals.append({
                        'code':       code,
                        'shares':     buy_shares,
                        'ref_price':  round(c_price, 3),
                        'atr_14':     round(float(tgt['atr_14']), 4),
                        'signal_time': datetime.now().isoformat(),
                    })
                else:
                    print(f"⚠️ [{code}] 资金不足一手（{CAPITAL_PER_SLOT:.0f}/{c_price:.3f}），跳过。")

            if buy_signals:
                save_signals(buy_signals)
                print(f"📡 买入信号已写入 fat_fish_signals.json，共 {len(buy_signals)} 条，等待火炮实物落单。")
                _sig_lines = "\n".join(
                    f"  {i+1}. {s['code']} | ≈{s['shares']}股 | 参考价 {s['ref_price']}"
                    for i, s in enumerate(buy_signals)
                )
                send_n8n_alert(
                    f"🎯 胖鱼信号生成 {len(buy_signals)} 条",
                    f"大脑(14:40)已生成 {len(buy_signals)} 条买入信号，等待 14:50 火炮落单：\n{_sig_lines}\n\n市场宽度: {breadth_ratio*100:.1f}% ✅"
                )

    # ─── 落盘（大脑只写止损棘轮更新 + 卖出指令，严禁写新买入槽位）──
    save_slots_only(current_slots)   # ← 只含棘轮更新后的已有槽位，无新槽位
    save_orders(orders)              # ← 卖出指令供执行器消费
    print(
        f"💾 大脑落盘完成：已有槽位 {len(current_slots)} 个（止损线已更新） | "
        f"卖单指令 {len(orders['eod_sell'])} 笔 | 买入信号（待火炮落单）"
    )
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🏁 胖鱼大脑运行结束，等待 14:50 执行器唤醒。")


if __name__ == "__main__":
    run_master()