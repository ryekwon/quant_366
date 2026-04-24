#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fat Fish Guard — 胖鱼防线（实时止损盾牌）
运行周期：09:30 - 14:57 死循环（由 autopilot_master.py 在早盘拉起，watchdog 保护）

职责：
  - 每 0.5 秒轮询 fat_fish_slots.yaml 中所有槽位的 stop_loss_price
  - 一旦 lastPrice < stop_loss_price，立即市价斩仓，物理释放槽位
  - 不计算任何指标，极简纯粹
"""

import os
import json
import time
import random
import yaml
import requests
from datetime import datetime
from dotenv import load_dotenv
from xtquant import xtdata, xtconstant          # xtconstant 必须同行导入（quant-safe-patterns）
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount

load_dotenv()

# ================= 物理路径与账户配置 =================
STATE_DIR       = r"Z:\QuantpC_Workspace\Quant_Pilot\.state"
SLOTS_FILE      = os.path.join(STATE_DIR, "fat_fish_slots.yaml")
STATUS_FILE     = os.path.join(STATE_DIR, "autopilot_status.json")
ACTION_LOG_DIR  = os.path.join(STATE_DIR, "action_logs")
LOCK_FILE       = os.path.join(STATE_DIR, "fat_fish_guard.lock")   # 🔒 进程锁
QMT_ACCOUNT_ID  = os.getenv("ACCOUNT_ID")
QMT_PATH        = os.getenv("QMT_PATH")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")
PROBE_NAME      = "fat_fish_guard"
SWEEP_RATIO = 0.90


# ====================================================================
# 🔒 进程唯一性锁（与 t0_multigrid_executor 对齐）
#    防止 autopilot_master.py watchdog 在旧进程还存活时重复拉起
# ====================================================================

def acquire_lock_with_ttl(lock_path: str, max_age_seconds: int = 300) -> bool:
    """TTL 自愈型进程锁。默认 300 秒 (5 分钟) 超时后强行粉碎。"""
    if os.path.exists(lock_path):
        file_age = time.time() - os.path.getmtime(lock_path)
        if file_age > max_age_seconds:
            print(f"⚠️ [系统自愈] fat_fish_guard 发现残留孤儿锁 ({file_age:.1f}s)，强行粉碎并接管...")
            try:
                os.remove(lock_path)
            except OSError:
                pass
        else:
            print(f"🚫 [并发拦截] fat_fish_guard 进程锁生效中 (存活 {file_age:.1f}s)，本次重复调用静默退出。")
            return False
    try:
        with open(lock_path, 'w') as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return False


def release_lock(lock_path: str):
    if os.path.exists(lock_path):
        try:
            os.remove(lock_path)
        except OSError:
            pass


def send_n8n_alert(title: str, message: str):
    """N8N Webhook 推送（静默失败，不阻塞主循环）"""
    if not N8N_WEBHOOK_URL:
        return
    try:
        requests.post(
            N8N_WEBHOOK_URL,
            json={"title": title, "message": message,
                  "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            timeout=5
        )
    except Exception:
        pass


def append_action_log(action: str, target: str, price: float,
                      reason: str, extra: dict | None = None):
    """
    逐笔将交易动作写入 action_logs/action_YYYYMMDD.jsonl
    格式与 T0_Grid 完全对齐，支持统一审计诊断。
    """
    try:
        os.makedirs(ACTION_LOG_DIR, exist_ok=True)
        log_file = os.path.join(
            ACTION_LOG_DIR,
            f"action_{datetime.now().strftime('%Y%m%d')}.jsonl"
        )
        record = {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "strategy":  "Fat_Fish_Guard",
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


def write_probe(status: str, extra: dict | None = None):
    """
    探针：将运行状态写入 autopilot_status.json（供 Dashboard 消费）
    status: 'running' | 'stopped' | 'error' | 自定义文本
    """
    try:
        try:
            with open(STATUS_FILE, 'r', encoding='utf-8') as f:
                all_status = json.load(f)
        except Exception:
            all_status = {}

        slot_count = len(load_slots())
        all_status[PROBE_NAME] = {
            "strategy_name": "胖鱼防线",
            "script":        "fat_fish_guard.py",
            "pid":           os.getpid(),
            "status":        status,
            "last_tick":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "slot_count":    slot_count,
            "description":   f"0.5s 轮询防线 | 当前监控 {slot_count} 个槽位",
            **(extra or {}),
        }
        with open(STATUS_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_status, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def is_trading_time() -> bool:
    """物理交易时间锁，防盘外脏数据触发误操作"""
    t = datetime.now().strftime("%H%M%S")
    return "093000" <= t <= "113000" or "130000" <= t <= "145700"


def load_slots() -> dict:
    """读取当前占用槽位及止损红线"""
    if not os.path.exists(SLOTS_FILE):
        return {}
    try:
        with open(SLOTS_FILE, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        return data.get('slots', {}) if data else {}
    except Exception:
        return {}


def remove_slot_and_save(code: str, slots_data: dict):
    """防线击穿后：释放槽位并**原子覆写**状态机文件（防崩溃写半截导致文件损坏）"""
    if code in slots_data:
        del slots_data[code]
        tmp = SLOTS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            yaml.dump({'slots': slots_data}, f, allow_unicode=True)
        os.replace(tmp, SLOTS_FILE)   # 原子替换，崩溃安全
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🗑️ 槽位 [{code}] 已原子释放 → fat_fish_slots.yaml 更新。")



def init_qmt_trader():
    """
    初始化 QMT 实盘交易接口。
    ⚠️ quant-safe-patterns：start() 后必须 sleep(5) 再 connect()
    """
    if not QMT_ACCOUNT_ID or not QMT_PATH:
        print("❌ [配置缺失] 环境变量 ACCOUNT_ID 或 QMT_PATH 未设置，请检查 .env 文件。")
        return None, None

    session_id = random.randint(100000, 999999)
    trader = XtQuantTrader(QMT_PATH, session_id)
    trader.start()
    time.sleep(5)                          # ← 规范等待，不可省略

    connect_result = trader.connect()
    if connect_result == 0:
        acc = StockAccount(QMT_ACCOUNT_ID)
        trader.subscribe(acc)
        print("✅ [系统就绪] miniQMT 实盘接口连接成功！")
        return trader, acc
    else:
        print(f"❌ [系统异常] miniQMT 连接失败，错误码: {connect_result}")
        trader.stop()
        return None, None


def run_guard():
    # 🔒 进程唯一性保护：防止 watchdog 在旧进程还存活时重复拉起
    os.makedirs(STATE_DIR, exist_ok=True)
    if not acquire_lock_with_ttl(LOCK_FILE):
        return

    try:
        print("=" * 60)
        print("🛡️ 胖鱼防线 (Fat Fish Guard) 启动监控...")
        print("底层逻辑：0.5s 轮询，Tick 跌穿 2×ATR 红线即刻斩仓")
        print("=" * 60)

        trader, acc = init_qmt_trader()
        if not trader:
            write_probe("error", {"description": "miniQMT 连接失败，内循环未启动"})
            return

        subscribed_codes: set = set()
        _last_heartbeat   = 0.0   # 探针心跳计时器

        write_probe("running")

        try:
            while True:
                # ── 1. 交易时间锁 ──────────────────────────────────────────
                if not is_trading_time():
                    time.sleep(5)
                    continue

                # 探针 Heartbeat：每 30s 写一次状态
                if time.time() - _last_heartbeat > 30:
                    write_probe("running")
                    _last_heartbeat = time.time()

                # ── 2. 读取最新槽位状态机 ────────────────────────────────
                current_slots = load_slots()
                active_codes  = list(current_slots.keys())

                if not active_codes:
                    time.sleep(3)
                    continue

                # ── 3. 动态订阅：新进槽位需要先订阅行情 ──────────────────
                for code in active_codes:
                    if code not in subscribed_codes:
                        xtdata.subscribe_quote(code, period='tick', count=1)
                        subscribed_codes.add(code)
                        sl = current_slots[code].get('stop_loss_price', 'N/A')
                        print(f"📡 [雷达锁定] {code} 实时防线启动，止损红线: {sl}")

                # ── 4. 批量拉取实时 Tick ──────────────────────────────────
                ticks = xtdata.get_full_tick(active_codes)

                # ── 5. 毫秒级红线判定 ────────────────────────────────────
                for code in active_codes:
                    tick_data  = ticks.get(code, {})
                    last_price = tick_data.get('lastPrice', 0.0)

                    if last_price <= 0.0:     # 脏数据/停牌，跳过
                        continue

                    stop_loss = current_slots[code].get('stop_loss_price', 0.0)
                    sell_vol  = current_slots[code].get('shares', 0)

                    if last_price >= stop_loss or sell_vol <= 0:
                        continue

                    # ── 💥 防线击穿，限价扫盘斩仓（绝对成交保证）────────
                    print("\n" + "!" * 60)
                    print(f"🚨 [防线击穿] {datetime.now().strftime('%H:%M:%S.%f')} | {code}")
                    print(f"📉 现价 {last_price:.3f} < 止损红线 {stop_loss:.3f} | 触发核按钮！")
                    print("!" * 60 + "\n")

                    # 9折限价扫盘：FIX_PRICE 不会被交易所弹回，
                    # 即使盘口买单瞬间蒸发，也能向下扫盘直到成交
                    sweep_price = round(last_price * SWEEP_RATIO, 3)

                    seq = trader.order_stock(
                        acc, code,
                        xtconstant.STOCK_SELL,
                        sell_vol,
                        xtconstant.FIX_PRICE,    # 限价单，不会被交易所拒单
                        sweep_price,             # 9折价，向下扫盘至成交
                        "Fat_Fish_Guard",
                        "ATR_STOP_LOSS"
                    )

                    if seq and seq > 0:
                        msg = (
                            f"📉 [{code}] ATR 止损触发\n"
                            f"现价: {last_price:.3f} | 红线: {stop_loss:.3f}\n"
                            f"扫盘价: {sweep_price:.3f} | 数量: {sell_vol}股\n"
                            f"委托序号: {seq}"
                        )
                        print(f"✅ [斩仓击发] {msg.replace(chr(10), ' | ')}")
                        send_n8n_alert("🚨 胖鱼防线确认斩仓", msg)
                        append_action_log(
                            action="斩仓",
                            target=code,
                            price=sweep_price,
                            reason=f"ATR止损 | 现{last_price:.3f} < 红线{stop_loss:.3f}",
                            extra={"qty": sell_vol, "seq": seq,
                                   "sweep_ratio": SWEEP_RATIO, "last_price": last_price}
                        )
                        remove_slot_and_save(code, current_slots)
                        subscribed_codes.discard(code)
                    else:
                        err = f"❌ {code} 斩仓拒单 (seq={seq})，紧急人工介入！"
                        print(err)
                        send_n8n_alert("🆘 危险！胖鱼防线斩仓失败", err)
                        append_action_log(
                            action="斩仓失败",
                            target=code,
                            price=sweep_price,
                            reason=f"QMT拒单 | 现{last_price:.3f} < 红线{stop_loss:.3f}",
                            extra={"qty": sell_vol, "seq": seq, "error": True}
                        )

                time.sleep(0.5)   # 0.5s 轮询，A股够用

        except KeyboardInterrupt:
            print("\n⏹️ 胖鱼防线已手动关闭。")
        finally:
            trader.stop()

    finally:
        release_lock(LOCK_FILE)

if __name__ == "__main__":
    run_guard()
