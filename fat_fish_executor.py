#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fat Fish Executor — 胖鱼火炮（尾盘击发执行器）
调度时间：每日 14:50 唤醒一次（由 autopilot_master.py 触发）

职责：
  - 读取 fat_fish_orders.yaml（由 14:40 Master 生成）
  - 先执行卖单（释放真实购买力），再执行买单（填装新槽位）
  - 执行完毕后将 orders 文件归档，防止重复执行

[架构] Fill-Based 记账（铁律一）
  ❌ 旧版：发单成功(seq>0) → 立即写 fat_fish_slots.yaml（乐观预写，幽灵持仓根因）
  ✅ 新版：发单成功 → 注册 _ff_pending → on_order_trade 成交回调 → 才写 fat_fish_slots.yaml
  超时60s未回调 → sweep 补录：物理查成交量，有成交则手动写槽位
"""

import os
import json
import time
import random
import shutil
import yaml
import threading
from datetime import datetime
from dotenv import load_dotenv
from xtquant import xtdata, xtconstant          # xtconstant 必须同行导入（quant-safe-patterns）
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount

load_dotenv()

# ================= 物理路径与账户配置（从 .env 读取，不硬编码）=================
STATE_DIR      = r"Z:\QuantpC_Workspace\Quant_Pilot\.state"
ORDERS_FILE    = os.path.join(STATE_DIR, "fat_fish_orders.yaml")
SLOTS_FILE     = os.path.join(STATE_DIR, "fat_fish_slots.yaml")   # 火炮拥有写入权
SIGNALS_FILE   = os.path.join(STATE_DIR, "fat_fish_signals.json") # 大脑产出信号，火炮消费
STATUS_FILE    = os.path.join(STATE_DIR, "autopilot_status.json")
ACTION_LOG_DIR = os.path.join(STATE_DIR, "action_logs")
QMT_ACCOUNT_ID = os.getenv("ACCOUNT_ID")
QMT_PATH       = os.getenv("QMT_PATH")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")
PROBE_NAME     = "fat_fish_executor"


import requests

# ─── N8N Webhook（按 quant-v4-patterns §13 规范）───────────────────────────────
def send_n8n_alert(title: str, message: str) -> None:
    """N8N 推送，失败静默（不阻断交易逻辑）。timeout=5sec。"""
    if not N8N_WEBHOOK_URL:
        return
    try:
        requests.post(N8N_WEBHOOK_URL,
                      json={"title": title, "message": message},
                      timeout=5)
    except Exception:
        pass  # 推送失败不影响交易逻辑


def write_probe(status: str, buy_cnt: int = 0, sell_cnt: int = 0, note: str = ""):
    """探针：将执行状态写入 autopilot_status.json"""
    try:
        try:
            with open(STATUS_FILE, 'r', encoding='utf-8') as f:
                all_status = json.load(f)
        except Exception:
            all_status = {}
        all_status[PROBE_NAME] = {
            "strategy_name": "胖鱼火炮",
            "script":        "fat_fish_executor.py",
            "pid":           os.getpid(),
            "status":        status,
            "fired_at":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "buy_orders":    buy_cnt,
            "sell_orders":   sell_cnt,
            "description":   note or f"尾盘击发 | 买{buy_cnt}卖{sell_cnt}",
        }
        with open(STATUS_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_status, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def append_action_log(action: str, target: str, price: float,
                      reason: str, extra: dict | None = None):
    """逐笔将交易动作写入 action_logs/action_YYYYMMDD.jsonl（与 T0_Grid 格式对齐）"""
    try:
        os.makedirs(ACTION_LOG_DIR, exist_ok=True)
        log_file = os.path.join(
            ACTION_LOG_DIR,
            f"action_{datetime.now().strftime('%Y%m%d')}.jsonl"
        )
        record = {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "strategy":  "Fat_Fish_Executor",
            "action":    action,
            "target":    target,
            "price":     round(price, 3),
            "reason":    reason,
            "extra":     extra or {},
        }
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_signals() -> list:
    """[火炮专属] 读取大脑生成的买入信号，并校验是否为**当日**信号。
    跨日信号（generated_at 日期 ≠ 今天）一律拒绝，视同空信号处理。
    """
    if not os.path.exists(SIGNALS_FILE):
        return []
    try:
        with open(SIGNALS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # ── 日期守卫：拒绝跨日过期信号 ──────────────────────────────
        generated_at = data.get('generated_at', '')
        if generated_at:
            signal_date = generated_at[:10]          # 取 YYYY-MM-DD
            today_str   = datetime.now().strftime('%Y-%m-%d')
            if signal_date != today_str:
                print(f"   🚫 [信号过期] signals.json 生成于 {signal_date}，今日为 {today_str}，拒绝使用！")
                return []

        return data.get('signals', [])
    except Exception as e:
        print(f"   ⚠️ [信号读取失败] {e}")
        return []



def load_slots() -> dict:
    """[火炮专属] 读取当前槽位状态。"""
    import yaml as _yaml
    if not os.path.exists(SLOTS_FILE):
        return {}
    try:
        with open(SLOTS_FILE, 'r', encoding='utf-8') as f:
            data = _yaml.safe_load(f)
        return data.get('slots', {}) if data else {}
    except Exception:
        return {}


def save_slots(slots: dict):
    """[火炮唯一写入入口] 发单成功后将新槽位正式写入 fat_fish_slots.yaml。"""
    import yaml as _yaml
    tmp = SLOTS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        _yaml.dump({'slots': slots}, f, allow_unicode=True)
    os.replace(tmp, SLOTS_FILE)  # 原子写入


def purge_signals():
    """[阅后即焚] 执行完毕后物理清空信号文件，防止次日重复读取。"""
    try:
        if os.path.exists(SIGNALS_FILE):
            os.remove(SIGNALS_FILE)
        tmp = SIGNALS_FILE + '.tmp'
        if os.path.exists(tmp):
            os.remove(tmp)
        print("🔥 [阅后即焚] fat_fish_signals.json 已物理清除，不会次日重复触发。")
    except Exception as e:
        print(f"   ⚠️ 信号清除失败: {e}")


# ── Fill-Based 全局状态（铁律一 + 铁律四）──────────────────────────────────
# {seq: {code, shares, buy_price, atr_14, sent_at}}
_ff_pending: dict[int, dict] = {}
_ff_pending_lock = threading.Lock()
# 已成交的待写槽位暂存（回调线程写，主线程读）
_ff_filled_slots: dict[str, dict] = {}  # {code: slot_dict}
_ff_slots_lock   = threading.Lock()
# 已成交的卖出（用于清除槽位）
_ff_sold_codes: set = set()
_ff_sold_lock    = threading.Lock()

PENDING_TIMEOUT_SEC = 60   # 超时 60s 未回调 → 进入 sweep 补录


class FatFishCallback(XtQuantTraderCallback):
    """
    Fill-Based 回调：只在 on_order_trade 收到成交通知后，
    才将槽位数据提交到 _ff_filled_slots（主线程落盘）。
    铁律一：绝对禁止在发单路径上写 fat_fish_slots.yaml。
    """

    def on_order_trade(self, trade):
        """成交回调 — BUY 成交写槽位，SELL 成交清槽位"""
        oid = trade.order_id
        # ── 买入成交：注册填充槽位 ──
        with _ff_pending_lock:
            meta = _ff_pending.pop(oid, None)
        if meta:
            code   = meta['code']
            filled = trade.traded_volume
            if filled <= 0:
                print(f"   ⚠️ [回调·零成交] {code} seq={oid} traded_volume=0，不写槽位。")
                return
            slot = {
                'buy_date':        datetime.now().strftime('%Y-%m-%d'),
                'buy_price':       round(trade.traded_price, 3),   # 用实际成交价，非估算价
                'shares':          int(filled),
                'highest_price':   round(trade.traded_price, 3),
                'stop_loss_price': round(
                    trade.traded_price - 2 * meta['atr_14'], 3
                ) if meta['atr_14'] > 0 else round(trade.traded_price * 0.95, 3),
            }
            with _ff_slots_lock:
                _ff_filled_slots[code] = slot
            print(f"   ✅ [Fill·槽位就绪] {code} 成交 {filled}股 @ {trade.traded_price:.3f}，"
                  f"止损={slot['stop_loss_price']:.3f}，等待主线程落盘。")
            return

        # ── 卖出成交：标记清槽 ──
        # 回调未必对应 fat_fish 发的单（多引擎共存），检查策略名过滤
        remark = getattr(trade, 'order_remark', '') or ''
        if 'Fat_Fish' in remark or 'EOD' in remark.upper():
            # 从持仓逆查 code（trade 对象包含 stock_code）
            code = getattr(trade, 'stock_code', None)
            if code:
                with _ff_sold_lock:
                    _ff_sold_codes.add(code)
                print(f"   ✅ [Fill·卖出确认] {code} sell 成交，将从槽位移除。")

    def on_order_error(self, order_error):
        """废单回调：将 pending 中对应委托清除（不写槽位）"""
        oid = order_error.order_id
        with _ff_pending_lock:
            meta = _ff_pending.pop(oid, None)
        if meta:
            print(f"   ❌ [废单] {meta['code']} seq={oid} 被拒绝 → 不写槽位，资金自然归还。")

    # 其余回调静默即可
    def on_stock_order(self, order): pass
    def on_stock_asset(self, asset): pass
    def on_stock_position(self, position): pass
    def on_order_stock_async_response(self, response): pass
    def on_cancel_order_stock_async_response(self, r): pass


def _sweep_pending(trader, acc):
    """
    超时 Sweep（补录）：对 > PENDING_TIMEOUT_SEC 的 pending 委托，
    物理查成交量，有成交则补写槽位；无成交则视为废单清除。
    """
    now = time.time()
    with _ff_pending_lock:
        stale = {seq: m for seq, m in _ff_pending.items()
                 if now - m['sent_at'] > PENDING_TIMEOUT_SEC}

    for seq, meta in stale.items():
        code = meta['code']
        try:
            trades = trader.query_stock_trades(acc) or []
            filled = sum(t.traded_volume for t in trades if t.order_id == seq)
            if filled > 0:
                avg_price = sum(
                    t.traded_price * t.traded_volume for t in trades if t.order_id == seq
                ) / filled
                slot = {
                    'buy_date':        datetime.now().strftime('%Y-%m-%d'),
                    'buy_price':       round(avg_price, 3),
                    'shares':          int(filled),
                    'highest_price':   round(avg_price, 3),
                    'stop_loss_price': round(
                        avg_price - 2 * meta['atr_14'], 3
                    ) if meta['atr_14'] > 0 else round(avg_price * 0.95, 3),
                }
                with _ff_slots_lock:
                    _ff_filled_slots[code] = slot
                print(f"   🔄 [Sweep·补录] {code} seq={seq} 补录 {filled}股 @ {avg_price:.3f}")
            else:
                print(f"   ⚠️ [Sweep·废单] {code} seq={seq} 超时且成交量=0，视为废单，不写槽位。")
        except Exception as e:
            print(f"   ⚠️ [Sweep·异常] {code} seq={seq}: {e}")
        finally:
            with _ff_pending_lock:
                _ff_pending.pop(seq, None)


def init_qmt_trader():
    """
    初始化 QMT 实盘交易接口，挂载 FatFishCallback。
    ⚠️ quant-safe-patterns：start() 后必须 sleep(5) 再 connect()
    """
    if not QMT_ACCOUNT_ID or not QMT_PATH:
        print("❌ [配置缺失] 环境变量 ACCOUNT_ID 或 QMT_PATH 未设置，请检查 .env 文件。")
        return None, None

    callback   = FatFishCallback()
    session_id = random.randint(100000, 999999)
    trader     = XtQuantTrader(QMT_PATH, session_id, callback)   # 挂载 Fill 回调
    trader.start()
    time.sleep(5)                          # ← 规范等待，不可省略

    connect_result = trader.connect()
    if connect_result == 0:
        acc = StockAccount(QMT_ACCOUNT_ID)
        trader.subscribe(acc)
        print("✅ [系统就绪] miniQMT 实盘接口连接成功（Fill-Based 回调已挂载）！")
        return trader, acc
    else:
        print(f"❌ [致命异常] miniQMT 连接失败，错误码: {connect_result}")
        trader.stop()
        return None, None


def _get_instrument_up_limit(code: str) -> float:
    """获取标的涨停价（ETF 一般是 ±10%，海外 ETF 可能不同）"""
    try:
        detail = xtdata.get_instrument_detail(code) or {}
        return detail.get('UpLimit', 0.0)
    except Exception:
        return 0.0


def execute_orders():
    print("=" * 60)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 胖鱼火炮 (Fat Fish Executor) 启动！")
    print("=" * 60)

    # ── 1. 物理检查：指令文件 + 信号文件是否存在 ────────────────────────
    orders_exist  = os.path.exists(ORDERS_FILE)
    signals_exist = os.path.exists(SIGNALS_FILE)

    if not orders_exist and not signals_exist:
        print("⏸️ 未发现今日作战指令 (fat_fish_orders.yaml) 也无信号文件 (fat_fish_signals.json)，系统静默退出。")
        write_probe("skipped", note="无指令文件也无信号文件")
        send_n8n_alert(
            "⏸️ 胖鱼火炮静默",
            f"14:50 执行器唤醒，但未发现今日 orders.yaml 也无 signals.json。\n可能原因：14:40 大脑未生成信号（无标的通过三铁律）或主脑未运行。"
        )
        return

    # ── 2. 读取指令（orders.yaml 可以不存在，只有卖单才硬性要求）──────
    orders = {}
    if orders_exist:
        with open(ORDERS_FILE, 'r', encoding='utf-8') as f:
            orders = yaml.safe_load(f) or {}

    sell_orders = orders.get('eod_sell', [])
    # 买入信号来自 signals.json（大脑隔离输出），不再依赖 orders['buy']
    buy_signals_raw = load_signals()   # 提前读取，用于空检测

    if not sell_orders and not buy_signals_raw:
        print("⏸️ 今日指令为空（无卖单，也无买入信号），系统静默退出。")
        write_probe("skipped", note="无卖单也无信号")
        return


    # ── 3. 挂载实盘接口 ─────────────────────────────────────────────
    trader, acc = init_qmt_trader()
    if not trader:
        write_probe("error", note="miniQMT 连接失败")
        send_n8n_alert(
            "❌ 胖鱼火炮 QMT 连接失败",
            "14:50 执行器无法连接 miniQMT，今日买入/卖出指令全部未执行！\n请立即人工检查 miniQMT 进程状态。"
        )
        return

    # sell_orders / buy_signals_raw 已在前面提前读取（用于空检测时已载入）
    write_probe("running", buy_cnt=len(buy_signals_raw), sell_cnt=len(sell_orders), note="开始击发")
    print(f"📊 作战指令：卖出 {len(sell_orders)} 笔，买入信号 {len(buy_signals_raw)} 条。")

    # ================= 阶段 1：先卖出（释放真实购买力）=================
    for order in sell_orders:
        code   = order['code']
        reason = order.get('reason', 'EOD_Sell')
        print(f"🔪 [执行清仓] {code} | 原因: {reason}")

        tick_map   = xtdata.get_full_tick([code])
        tick       = tick_map.get(code, {})
        last_price = tick.get('lastPrice', 0.0)

        if last_price <= 0:
            print(f"   ❌ [盲区阻断] {code} 无法获取有效现价，跳过。")
            continue

        # 从 QMT 真实查询可用数量（防 YAML 与实盘脱节）
        positions  = trader.query_stock_positions(acc)
        actual_vol = next(
            (int(p.can_use_volume) for p in positions if p.stock_code == code), 0
        )

        if actual_vol <= 0:
            print(f"   ⚠️ [空仓阻断] QMT 查无 {code} 可用底仓，跳过。")
            continue

        # ETF 卖出：用对手方最优价模拟市价，确保成交
        seq = trader.order_stock(
            acc, code,
            xtconstant.STOCK_SELL,
            actual_vol,
            xtconstant.MARKET_PEER_PRICE_FIRST,    # 对手方最优价
            last_price,
            "Fat_Fish_Executor",
            reason
        )
        print(f"   ✅ [卖出击发] 序号: {seq}，数量: {actual_vol}")
        append_action_log(
            action="卖出", target=code, price=last_price,
            reason=reason, extra={"qty": actual_vol, "seq": seq}
        )

    if sell_orders:
        time.sleep(2)   # 等待券商资金结算通道释放

    # ================= 阶段 2：后买入（读信号 → 实物落单 → 成交后写槽位）=================
    buy_signals = buy_signals_raw   # 已在入口提前读取，直接复用
    if buy_signals:
        print(f"📊 读到 {len(buy_signals)} 条买入信号，准备实物落单…")
        current_slots = load_slots()  # 加载当前实际槽位状态
        available_slots = 3 - len(current_slots)  # 增量检查（偿付第一性原理）
    else:
        print("⏸️ 无买入信号，略过买入阶段。")
        buy_signals = []
        current_slots = {}

    for signal in buy_signals:
        code      = signal['code']
        shares    = signal['shares']
        ref_price = signal['ref_price']
        atr_14    = signal.get('atr_14', 0.0)
        print(f"🎯 [执行突击] {code} | 计划: {shares}股 | 参考价: {ref_price}")

        # 防火墙：如果已经占有该标的槽位，跳过（防止大脑发出重复信号）
        if code in current_slots:
            print(f"   ⚠️ [{code}] 已占用槽位，跳过。")
            continue

        tick_map   = xtdata.get_full_tick([code])
        tick       = tick_map.get(code, {})
        last_price = tick.get('lastPrice', 0.0)

        if last_price <= 0:
            print(f"   ❌ [盲区阻断] {code} 无法获取有效现价，跳过。")
            continue

        # 买入价：对手方最优，确保吃到对手盘
        up_limit  = _get_instrument_up_limit(code)
        buy_price = last_price

        seq = trader.order_stock(
            acc, code,
            xtconstant.STOCK_BUY,
            shares,
            xtconstant.MARKET_PEER_PRICE_FIRST,
            buy_price,
            "Fat_Fish_Executor",
            "Momentum_Buy"
        )
        print(f"   ✓ [买入击发] 序号: {seq}，数量: {shares}，价格: {buy_price:.3f}"
              + (f"（涨停: {up_limit:.3f}）" if up_limit > 0 else ""))

        if seq > 0:
            # ✅ Fill-Based 铁律一：发单成功只注册 pending，绝不写槽位账本
            # 槽位写入由 FatFishCallback.on_order_trade() 在成交后执行
            with _ff_pending_lock:
                _ff_pending[seq] = {
                    'code':      code,
                    'shares':    shares,
                    'buy_price': buy_price,
                    'atr_14':    atr_14,
                    'sent_at':   time.time(),
                }
            print(f"   📋 [Pending 注册] {code} seq={seq} 已注册，等待成交回调写槽位…")
            append_action_log(
                action="买入委托", target=code, price=buy_price,
                reason="Momentum_Buy",
                extra={"qty": shares, "seq": seq,
                       "ref_price": ref_price, "atr_14": atr_14, "up_limit": up_limit}
            )
        else:
            print(f"   ❌ [下单失败] seq={seq}，底层拒单，不写槽位账本。")
            append_action_log(
                action="买入失败", target=code, price=buy_price,
                reason=f"QMT拒单 seq={seq}",
                extra={"qty": shares, "ref_price": ref_price}
            )

    # ================= 阶段 3：等待 Fill 回调 / Sweep 补录 =================
    if buy_signals:
        print(f"\n⏳ [等待成交] 开始等待 {len(buy_signals)} 笔委托的 Fill 回调（最多 {PENDING_TIMEOUT_SEC}s）…")
        wait_start = time.time()
        while time.time() - wait_start < PENDING_TIMEOUT_SEC:
            with _ff_pending_lock:
                remaining = len(_ff_pending)
            if remaining == 0:
                print("   ✅ 所有委托已收到 Fill 回调，无需 Sweep。")
                break
            time.sleep(2)
        else:
            # 超时：对还在 pending 的委托做物理 Sweep 补录
            print(f"   ⚠️ 超时 {PENDING_TIMEOUT_SEC}s，剩余 {len(_ff_pending)} 笔委托进入 Sweep 补录…")
            _sweep_pending(trader, acc)

        # ── 将回调/sweep 收集的槽位落盘 ──
        with _ff_slots_lock:
            filled_this_run = dict(_ff_filled_slots)
            _ff_filled_slots.clear()

        if filled_this_run:
            current_slots = load_slots()  # 重新读（可能卖出阶段已修改）
            current_slots.update(filled_this_run)
            save_slots(current_slots)
            for code, slot in filled_this_run.items():
                print(f"   💾 [槽位落盘] {code} 成交确认写入 fat_fish_slots.yaml"
                      f" | 成本={slot['buy_price']:.3f} 止损={slot['stop_loss_price']:.3f}")
                append_action_log(
                    action="买入成交", target=code, price=slot['buy_price'],
                    reason="Fill-Based 槽位写入",
                    extra={"qty": slot['shares'], "stop_loss_price": slot['stop_loss_price']}
                )
                send_n8n_alert(
                    f"✅ 胖鱼开仓成交 {code}",
                    f"标的 {code} 已实物成交！\n"
                    f"  成交价: {slot['buy_price']:.3f}\n"
                    f"  成交量: {slot['shares']} 股\n"
                    f"  止损线: {slot['stop_loss_price']:.3f}\n"
                    f"  槽位已写入 fat_fish_slots.yaml，Guard 棘轮启动。"
                )
        else:
            print("   ℹ️  本次无买入成交，fat_fish_slots.yaml 无变更。")

        # ── 卖出成交后清槽位 ──
        with _ff_sold_lock:
            sold_this_run = set(_ff_sold_codes)
            _ff_sold_codes.clear()
        if sold_this_run:
            current_slots = load_slots()
            for code in sold_this_run:
                if code in current_slots:
                    del current_slots[code]
                    print(f"   🗑️  [槽位清除] {code} 卖出成交，已从槽位移除。")
            save_slots(current_slots)

    # ================= 阶段 4：物理归档绝缘 + 阅后即焚 =================
    archive_dir  = os.path.join(STATE_DIR, "history")
    os.makedirs(archive_dir, exist_ok=True)
    archive_name = f"fat_fish_orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"
    archive_path = os.path.join(archive_dir, archive_name)
    # orders 文件仅在存在时归档（无买单时 master 可能未生成）
    if os.path.exists(ORDERS_FILE):
        shutil.move(ORDERS_FILE, archive_path)
        print(f"💾 指令执行完毕，已归档至: {archive_path}")

    # 阅后即焚：物理清空信号文件，防止次日重复读取
    purge_signals()

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🏁 火炮收起，等待明日召唤。")

    write_probe("done",
                buy_cnt=len(buy_signals),
                sell_cnt=len(sell_orders),
                note=f"执行完毕，归档→{archive_name}")
    # ─── 执行完成汇总推送 ─────────────────────────────────────────────
    _sell_summary = ", ".join(o.get('code','?') for o in sell_orders) if sell_orders else "无"
    _buy_summary  = ", ".join(
        f"{s['code']}({s['shares']}股)" for s in buy_signals
    ) if buy_signals else "无"
    send_n8n_alert(
        f"🏁 胖鱼火炮执行完成",
        f"14:50 尾盘击发已完成！摘要：\n"
        f"  🔪 清仓单: {len(sell_orders)} 笔 → {_sell_summary}\n"
        f"  🎯 买入信号: {len(buy_signals)} 条 → {_buy_summary}\n"
        f"  归档: {archive_name}\n"
        f"  时间: {datetime.now().strftime('%H:%M:%S')}"
    )
    trader.stop()



if __name__ == "__main__":
    execute_orders()