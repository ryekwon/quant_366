# -*- coding: utf-8 -*-
"""
Auto Pilot V4.0：动态 YAML + 孤儿软着陆 + 深渊阻断器 + Taker 委托 + 跳空保护版
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
新增模块（相对 V3.3）：
  ★ 任务1  YAML 结构映射：atr_multiplier → spread_pct，构建 runtime_state 字典
  ★ 任务2  孤儿资产软着陆状态机：sell_only 标签，等网格上轨自然平仓
  ★ 任务3  深渊阻断器：持仓均价 -8% 或 最后买入价 -3% → 市价清仓
  ★ 优化1  Taker 超价委托：askPrice[0]+0.001 / bidPrice[0]-0.001
  ★ 优化2  跳空/临停保护：volume==0 跳过、开盘/午盘前 3 分钟屏蔽、Gap 重置中枢
保留原有：QMT 连接、GridTraderCallback、_ensure_qmt_ready、断线重连五重保护
"""
import re
_VALID_CODE = re.compile(r'^\d{6}\.(SH|SZ)$')
import os
import sys
import json
import yaml
import time
import math
import datetime
import subprocess
import requests
import psutil
import msvcrt  # Windows file locking
from dotenv import load_dotenv
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
from xtquant import xtdata, xtconstant
from quant_logger import record_action
import threading

load_dotenv()
QMT_PATH        = os.getenv("QMT_PATH")
ACCOUNT_ID      = os.getenv("ACCOUNT_ID")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")

# 🛡️ 解决 Windows 控制台打印 Emoji 导致的 UnicodeEncodeError
if sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        # 兼容旧版本 Python 3.6
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 🛡️ 使用绝对路径防止 autopilot_master 以不同 CWD 启动时发生 PermissionError
# 注意：_SCRIPT_DIR 在下方 logging 配置前先定义，此处提前占位引用
# （_SCRIPT_DIR 真正定义在 line ~98，此处借用同样的逻辑）
_STATE_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR    = os.path.join(_STATE_SCRIPT_DIR, ".state")
STATE_FILE   = os.path.join(STATE_DIR, "grid_state.json")
TARGETS_FILE = os.path.join(STATE_DIR, "grid_targets.yaml")
LOCK_FILE    = os.path.join(STATE_DIR, "executor.lock")
# 🛡️ T0 抽屉账本：盘中断点续传专用，仅存 t0_inventory 快照
# 生命周期：启动时加载 → 每次成交后更新 → EOD 清仓后物理销毁
T0_LEDGER_FILE = os.path.join(STATE_DIR, "t0_ledger.json")
last_mtime   = 0   # 🛡️ YAML 物理时间戳锚点

# ─── 深渊阻断器阈值 ─────────────────────────────────────────
ABYSS_AVG_COST_PCT  = 0.92   # 持仓均价 × 92% = -8% 触发线
ABYSS_LAST_BUY_PCT  = 0.97   # 最后买入价 × 97% = -3% 触发线

# ─── 瀑布熔断阈值（地缘政治/系统性崩盘保护）───────────────────────
WATERFALL_FUSE_PCT  = -0.04  # 单日跌幅 < -4%：冻结所有买入，仅保留卖出

# ─── 巡逻日志与溢价风控配置 ─────────────────────────────────────────
PATROL_INTERVAL   = 300   # 巡逻日志间隔（秒），默认 5 分钟
PREMIUM_THRESHOLD = 0.005  # 溢价率阈值：0.5%（收紧，防止高溢价买入）
ABSOLUTE_TP_PCT   = 0.03   # 🌋 绝对止盈线：3% （优先级最高，无视网格）
CROSS_BORDER_KEYWORDS = ['纳指', '纳斯达克', '标普', '日经', '恒生', '港股', '海外', '跨境', 'QDII', '德国', '美国', '亚太', '越南', '印度', '沙特', '巴西', '油气', '互联']

# ─── 资金超配系数（虚拟杠杆）───────────────────────────────────
# 利用网格极低的全满概率，放大名义购买力，提升资金周转率
# CEO 可随时修改此参数。1.0 = 不启用超配，3.0 = 3 倍虚拟杠杆
CAPITAL_LEVERAGE = 3.0

# ─── T0 Fill-Based Accounting 全局注册表 ─────────────────────
# runtime_state 提升为模块级，供 on_order_trade 回调线程访问
runtime_state: dict = {}

_t0_pending_lock = threading.Lock()
# {seq: {"code": str, "direction": "buy"|"sell", "qty": int, "sent_at": float,
#        "grid_slot": str (买入时必填，抽屉编号)}}
_t0_pending: dict[int, dict] = {}

_runtime_io_lock = threading.Lock()  # 保护 runtime_state 字典及落盘 IO（主/回调双线程）

# ─── Inventory 抽屉编号自增计数器（每日启动归零，每次买单 +1）──
# {code: int}  — 不落盘，每日重新从 1 开始编号
_t0_inv_slot_counter: dict[str, int] = {}

PENDING_TIMEOUT_SEC = 60
PENDING_SWEEP_SEC   = 30

# ─── 跳空保护：屏蔽时间窗口（HHMM 字符串） ────────────────────
# 09:25~09:30 集合竞价撮合期：bid/ask 队列未形成，Taker 价格经常为 0，必须屏蔽
GUARD_WINDOWS = [("0925", "0930"), ("0930", "0933"), ("1300", "1303")]


import logging
from pathlib import Path as _Path

# ─── 文件日志（参照 T1 写法，同时写文件 + 终端）──────────────────
# 🛡️ 使用绝对路径 + 时间戳文件名，防止重启时文件被占位截断
_SCRIPT_DIR = _Path(__file__).parent.resolve()
_LOGS_DIR   = _SCRIPT_DIR / 'logs'
_LOGS_DIR.mkdir(exist_ok=True)
_today_str = datetime.datetime.now().strftime('%Y%m%d')
_start_ts  = datetime.datetime.now().strftime('%H%M%S')
_log_path  = _LOGS_DIR / f'{_today_str}_{_start_ts}_t0_multigrid_executor.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.FileHandler(str(_log_path), encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
def log(msg):
    logging.info(msg)
    sys.stdout.flush()

# 🛡️ 劫持 print → logging，让所有 print() 自动同时写入日志文件
# 原因：主循环大量使用 print()，-WindowStyle Hidden 启动时 stdout 被丢弃导致日志为空
import builtins as _builtins
_orig_print = _builtins.print
def _print_to_log(*args, **kwargs):
    msg = " ".join(str(a) for a in args)
    logging.info(msg)
    kwargs.pop('file', None)  # 忽略 file 参数，统一写 logging
    _orig_print(*args, **kwargs)
_builtins.print = _print_to_log


def send_n8n_alert(title, message):
    if not N8N_WEBHOOK_URL:
        return
    try:
        payload = {"title": title, "message": message,
                   "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
        # 使用 POST 隐式传递，确保 JSON 结构绝对安全
        resp = requests.post(N8N_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"⚠️ N8N 告警发送失败 (HTTP {resp.status_code}): {message}")
    except Exception as e:
        print(f"⚠️ N8N 告警发送异常: {e}")


# ═══════════════════════════════════════════════════════════════
# 机器学习数据引擎 (CSV 数据采集)
# ═══════════════════════════════════════════════════════════════

import csv

class MLTradeLogger:
    """
    专门记录交易细节，用于后期机器学习分析
    输出路径: <项目根>/logs/trading_ml_data.csv
    """
    CSV_PATH = os.path.join(_LOGS_DIR, "trading_ml_data.csv")
    HEADERS = [
        "timestamp", "code", "name", "action", "price", "volume", 
        "base_price", "spread_pct", "current_lots", "max_lots", 
        "status", "avg_cost", "last_buy_price"
    ]

    def __init__(self):
        # 确保日志目录存在
        os.makedirs(os.path.dirname(self.CSV_PATH), exist_ok=True)
        # 如果文件不存在，写入表头
        if not os.path.exists(self.CSV_PATH):
            with open(self.CSV_PATH, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(self.HEADERS)

    def record(self, trade_data: dict):
        try:
            with open(self.CSV_PATH, 'a', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f)
                row = [trade_data.get(h, "") for h in self.HEADERS]
                writer.writerow(row)
        except Exception as e:
            print(f"⚠️ ML 日志写入失败: {e}")

# 初始化全局记录器
ML_LOGGER = MLTradeLogger()


def _now_hhmm() -> str:
    return datetime.datetime.now().strftime("%H%M")


def _in_guard_window() -> bool:
    """判断当前时间是否处于盘口保护窗口内（开盘/午盘前 3 分钟）"""
    now = _now_hhmm()
    return any(start <= now <= end for start, end in GUARD_WINDOWS)


# ═══════════════════════════════════════════════════════════════
# QMT 进程检测与自动启动（完整保留原 V3.3）
# ═══════════════════════════════════════════════════════════════

QMT_PROC_NAMES = ["XtMiniQmt.exe", "XtItClient.exe"]


def _is_qmt_running() -> bool:
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() in [p.lower() for p in QMT_PROC_NAMES]:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def _ensure_qmt_ready() -> bool:
    if _is_qmt_running():
        print("✅ 检测到 miniQMT 客户端已在运行，跳过自动启动流程。")
        send_n8n_alert("🟢 QMT 状态", "miniQMT 客户端已在运行，Auto Pilot 正常启动中。")
        return True

    print("⚠️  未检测到 miniQMT 客户端，正在通知 N8N 并尝试自动启动...")
    send_n8n_alert(
        "⚠️ QMT 客户端未启动",
        "未检测到 miniQMT 进程，Auto Pilot 将自动触发 start_miniQMT.py 尝试启动客户端。"
    )

    script_dir = os.path.dirname(os.path.abspath(__file__))
    launcher = os.path.join(script_dir, "start_miniQMT.py")
    if not os.path.exists(launcher):
        msg = f"找不到 {launcher}，无法自动启动 miniQMT，请手动登录。"
        print(f"❌ {msg}")
        send_n8n_alert("🚨 QMT 启动失败", msg)
        return False

    print(f"🚀 后台触发登录脚本: {launcher}")
    try:
        _sub_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.Popen(
            [sys.executable, launcher],
            env=_sub_env,
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )
    except Exception as e:
        msg = f"执行 start_miniQMT.py 时发生异常: {e}"
        print(f"❌ {msg}")
        send_n8n_alert("🚨 QMT 启动异常", msg)
        return False

    WAIT_LAUNCH = 120
    print(f"⏳ 等待 miniQMT 进程出现（最多 {WAIT_LAUNCH} 秒）...")
    deadline = time.time() + WAIT_LAUNCH
    while time.time() < deadline:
        time.sleep(3)
        if _is_qmt_running():
            time.sleep(5)
            msg = "miniQMT 客户端进程已检测到，Auto Pilot 继续启动。"
            print(f"🟢 {msg}")
            send_n8n_alert("🟢 QMT 启动成功", msg)
            return True
        if proc.poll() is not None and proc.returncode != 0:
            msg = f"start_miniQMT.py 意外退出 (退出码 {proc.returncode})，请检查登录配置或手动登录。"
            print(f"❌ {msg}")
            send_n8n_alert("🚨 QMT 启动失败", msg)
            return False

    msg = f"等待 miniQMT 进程超时（{WAIT_LAUNCH} 秒），请手动检查客户端状态。"
    print(f"❌ {msg}")
    send_n8n_alert("🚨 QMT 启动超时", msg)
    return False


# ═══════════════════════════════════════════════════════════════
# QMT 回调（完整保留原 V3.3）
# ═══════════════════════════════════════════════════════════════

class GridTraderCallback(XtQuantTraderCallback):
    def on_disconnected(self):
        send_n8n_alert("🚨 架构警报", "QMT 交易网关已断开！")

    def on_order_trade(self, trade):
        """Fill-based 成交回调：从 _t0_pending 取元数据，更新 runtime_state。

        [分笔成交修复] QMT 合同号 = order_id，同一委托可能分多笔推送。
        原来 pop() 导致第一笔成交后 pending 被删，后续分笔被当外部单忽略。
        修复：get() + 累加 filled_so_far，达到 target_qty 时才 pop。
        """
        global runtime_state

        filled_qty = trade.traded_volume
        filled_px  = trade.traded_price
        code       = trade.stock_code

        # 1. 取出 pending 元数据（不 pop — 支持分笔成交）
        with _t0_pending_lock:
            meta = _t0_pending.get(trade.order_id, None)

        # 通用战报日志（无论是否本引擎委托）
        detail = xtdata.get_instrument_detail(code) or {}
        name   = detail.get("InstrumentName", code)

        if meta is None:
            # 非本引擎委托：区分策略类型，避免误报
            _strat = getattr(trade, 'strategy_name', '') or ''
            if _strat.lower() in ('sniper', 'etf_rota', 'etfrota'):
                # Sniper / 轮动的成交由各自引擎推 N8N，T0 仅记日志，不重复推送
                print(f"✅ [T0成交·过路] {code} {name} strategy={_strat} 价={filled_px:.3f} qty={filled_qty}")
            else:
                # 真正的手动单或未知策略，发外部成交推送
                print(f"✅ [T0成交·外部] {code} {name} 价={filled_px:.3f} qty={filled_qty}")
                send_n8n_alert("🤖 T0 外部成交", f"{code} {name} | 价={filled_px:.3f} qty={filled_qty}")
            return

        direction  = meta["direction"]
        target_qty = meta.get("qty", 0)

        # 2. 累加已成交量，完全成交才 pop
        with _t0_pending_lock:
            m = _t0_pending.get(trade.order_id)
            if m:
                m["filled_so_far"] = m.get("filled_so_far", 0) + filled_qty
                filled_so_far = m["filled_so_far"]
                if target_qty > 0 and filled_so_far >= target_qty:
                    _t0_pending.pop(trade.order_id, None)
            else:
                filled_so_far = filled_qty

        # 3. 写入 t0_inventory（全程持 _runtime_io_lock）
        with _runtime_io_lock:
            rs = runtime_state.get(code)
            if rs is None:
                return

            if direction == "buy":
                # ── 买入：按 grid_slot 路由写入 t0_inventory 抽屉 ──────────────
                # pending 元数据中携带 grid_slot（下单时由主循环写入，唯一标识本次委托的抽屉编号）
                slot_key = str(meta.get("grid_slot", "0"))
                inv = rs.setdefault("t0_inventory", {})
                if slot_key not in inv:
                    # 首笔成交时创建抽屉，buy_price 取首笔成交价
                    inv[slot_key] = {
                        "buy_price":  round(float(filled_px), 4),
                        "filled_qty": 0,
                        "status":     "holding"
                    }
                inv[slot_key]["filled_qty"] += filled_qty   # ← 分笔累加，绝不 pop/重置
                print(f"  [T0·Inv·Buy] slot={slot_key} +{filled_qty} → "
                      f"filled_qty={inv[slot_key]['filled_qty']} / target={target_qty}")

            elif direction == "sell":
                # ── 卖出：按最老 holding slot 递减扣减 ────────────────────────
                inv = rs.get("t0_inventory", {})
                remaining = filled_qty
                for slot_k in sorted(inv.keys(), key=lambda x: int(x) if str(x).isdigit() else 0):
                    s = inv[slot_k]
                    if s.get("status") != "holding" or s.get("filled_qty", 0) <= 0:
                        continue
                    deduct = min(s["filled_qty"], remaining)
                    s["filled_qty"] -= deduct
                    remaining -= deduct
                    if s["filled_qty"] <= 0:
                        s["status"] = "sold"
                    if remaining <= 0:
                        break
                print(f"  [T0·Inv·Sell] -{filled_qty} → unmatched={remaining}")

        _save_runtime_state(runtime_state, STATE_FILE)
        _save_t0_state()   # 🛡️ [实时落盘] 每次 inventory 变更后立即持久化到 t0_ledger.json
        action = "🔴 低吸建仓" if direction == "buy" else "🟢 高抛收网"
        _lots = _inv_lots(rs)
        _vol  = _inv_volume(rs)
        msg = (f"{action} | {code} {name} | 成交价: {filled_px:.3f} | 数量: {filled_qty}"
               f" | 累计: {filled_so_far}/{target_qty} | 持仓格数: {_lots} | 持股: {_vol}")
        print(f"✅ [T0成交·Inv] {msg}")
        send_n8n_alert("🤖 T0 Auto Pilot 交易战报", msg)
        ML_LOGGER.record({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "code":      code, "name": name,
            "action":    action, "price": filled_px,
            "volume":    filled_qty, "status": "FILLED",
        })

    def on_order_error(self, order_error):
        """处理下单失败（如资金不足、废单等）"""
        code = order_error.stock_code
        msg = f"❌ 【下单失败】 {code} | 原因: {order_error.error_msg}"
        print(msg)
        send_n8n_alert("🚨 交易异常", msg)
        
        ML_LOGGER.record({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "code":      code,
            "action":    "ORDER_ERROR",
            "status":    "FAILED",
            "name":      order_error.error_msg  # 将错误信息存入 name 列方便复盘
        })

    def on_cancel_error(self, cancel_error):
        """处理撤单失败"""
        msg = f"⚠️ 【撤单失败】 订单ID: {cancel_error.order_id} | 原因: {cancel_error.error_msg}"
        print(msg)

    def on_order_stock(self, order):
        """报单确认（此时尚未成交）"""
        print(f"📝 【报单已提交】 {order.stock_code} | {order.order_remark} | 价格: {order.price} | 状态: {order.order_status}")


# ═══════════════════════════════════════════════════════════════
# ★ 任务1 辅助：将 YAML 映射到 runtime_state
# ═══════════════════════════════════════════════════════════════

def acquire_lock_with_ttl(lock_path, max_age_seconds=60):
    """带 TTL + PID 存活校验的双保险进程锁。
    TTL=60s（原 300s）：防止孤儿锁卡住整个交易日。
    PID 校验：锁文件记录的 PID 进程死亡时立即粉碎，无需等 TTL。
    """
    if os.path.exists(lock_path):
        file_age = time.time() - os.path.getmtime(lock_path)
        # ── PID 存活校验（优先级最高，0 秒响应）──────────────────
        pid_alive = False
        try:
            lock_pid = int(open(lock_path).read().strip())
            import psutil
            pid_alive = psutil.pid_exists(lock_pid)
        except Exception:
            pid_alive = False  # 读取失败视为死亡

        if not pid_alive:
            print(f"⚠️ [系统自愈] 锁文件 PID 进程已死亡，立即粉碎孤儿锁并接管...")
            try:
                os.remove(lock_path)
            except OSError:
                pass
        elif file_age > max_age_seconds:
            print(f"⚠️ [系统自愈] 发现残留孤儿锁 ({file_age:.1f}秒 > TTL {max_age_seconds}s)。强行粉碎并接管控制权...")
            try:
                os.remove(lock_path)
            except OSError:
                pass
        else:
            print(f"🚫 [并发拦截] 进程锁生效中 PID={lock_pid} 存活 {file_age:.1f}秒，本次调度自动静默退让。")
            return False
    try:
        with open(lock_path, 'w') as f: f.write(str(os.getpid()))
        return True
    except: return False

def release_lock(lock_path):
    if os.path.exists(lock_path):
        try: os.remove(lock_path)
        except OSError: pass

def safe_execute_and_lock(xt_trader, acc, code, order_type, qty, price, strategy_name, order_remark, save_func):
    """原子化下单与落盘封装"""
    try:
        res = xt_trader.order_stock(acc, code, order_type, qty, xtconstant.FIX_PRICE, price, strategy_name, order_remark)
        if res > 0:
            save_func()
        return res
    except Exception as e:
        print(f"❌ Execution Critical Error: {e}")
        return -1

def _build_runtime_state_from_yaml(yaml_targets: dict, json_state: dict,
                                   ticks: dict) -> dict:
    """
    将 YAML（新格式）+ JSON 历史状态 合并为统一的内存 runtime_state。
    字段定义：
      spread_pct      = yaml['atr_multiplier']     (网格间距百分比)
      trade_amount    = yaml['trade_amount']         (单格绝对金额 元)
      max_lots        = yaml['max_lots']
      current_lots    = json 恢复或 0
      base_price      = json 恢复或 当前 tick lastPrice
      last_buy_price  = json 恢复或 0               (深渊阻断器基准)
      status          = 'active'
    """
    rs = {}
    for code, cfg in yaml_targets.items():
        old = json_state.get(code, {})
        tick_price = ticks.get(code, {}).get('lastPrice', 0)
        base = old.get('base_price', tick_price) or tick_price
        yaml_atr_pct = float(cfg.get('spread_pct', 0.02))  # t0_master 写入的基础 ATR
        if yaml_atr_pct <= 0 or yaml_atr_pct > 0.15:
            yaml_atr_pct = 0.02
        
        # 🛡️ 补丁：增加 .get() 保护，防止字典缺失字段
        rs[code] = {
            "name":           str(cfg.get('name', code)),
            "tag":            str(cfg.get('tag', 'Other')), # 🏷️ 分类标签支持
            "trade_amount":   int(cfg.get('trade_amount', 0)),
            "atr_multiplier": float(cfg.get('atr_multiplier', 1.0)),
            "max_lots":       int(cfg.get('max_lots', 5)),
            "current_lots":   int(old.get('current_lots', old.get('position', 0) // 100)),
            "base_price":     float(base),
            "last_buy_price": float(old.get('last_buy_price', 0)),
            "status":         "active",
            "spread_pct":     float(cfg.get('spread_pct', 0.01)),
            "_atr_pct":       float(old.get('_atr_pct', yaml_atr_pct)),
        }
        if base > 0:
            raw_spread_pct = rs[code]['_atr_pct'] * rs[code]['atr_multiplier']
            # 🛡️ 初始化时也应用物理钳制
            if code == '518680.SH':
                est = 0.004
            else:
                est = max(0.005, min(0.012, raw_spread_pct))
                
            print(f"   [{rs[code]['name']}] 中枢: {base:.3f} | "
                  f"ATR乘数: {rs[code]['atr_multiplier']:.1f}x | "
                  f"预估网格: {est*100:.2f}% | "
                  f"单格: Y{rs[code]['trade_amount']}")
    return rs



def reconcile_positions_with_real(yaml_targets: dict) -> dict:
    """[Fill-Based v4 · Inventory + Boot Recovery] 启动时优先从 t0_ledger.json 恢复断点。

    改造要点（断点续传 4步重构）：
    ① 启动时检测 .state/t0_ledger.json 是否存在且格式合法
    ② 存在且合法 → 直接将其 t0_inventory 加载到内存，slot_counter 也同步恢复
    ③ 不存在/格式损坏 → 退化为原先零库存基准（安全回退）
    ④ 废除「每日启动无脑 t0_inventory = {}」行为

    物理安全：t0_ledger.json 仅在 EOD 清仓完成后才被 _purge_t0_ledger() 删除。
    因此盘前 09:30 启动时不存在账本 = 真正的零库存（前日已销毁）；
    盘中 14:00 崩溃重启时存在账本 = 恢复孤儿持仓，14:52 EOD 清仓不失效。
    """
    global _t0_inv_slot_counter

    # ── 尝试从硬盘账本恢复断点 ──────────────────────────────────────
    ledger_snapshot: dict = {}
    if os.path.exists(T0_LEDGER_FILE):
        try:
            with open(T0_LEDGER_FILE, 'r', encoding='utf-8') as _f:
                _raw = json.load(_f)
            # 格式合法性校验：必须是字典，且各 code 下有 t0_inventory 键
            if isinstance(_raw, dict) and all(
                isinstance(v, dict) and 't0_inventory' in v for v in _raw.values()
            ):
                ledger_snapshot = _raw
                _saved_at = next(iter(_raw.values()), {}).get('saved_at', '未知')
                print(f"🔄 [Boot Recovery] 检测到 t0_ledger.json，满血复活断点状态 "
                      f"(落盘时间: {_saved_at})")
                print(f"   恢复标的: {list(ledger_snapshot.keys())}")
            else:
                print(f"⚠️ [Boot Recovery] t0_ledger.json 格式异常，忽略，零库存启动。")
        except Exception as _e:
            print(f"⚠️ [Boot Recovery] 读取 t0_ledger.json 失败: {_e}，零库存启动。")
    else:
        print("🛡️ [Reconcile-v4] 未发现账本快照，Fill-Based 零库存基准初始化（不查 QMT 总持仓）")

    # ── 构建 runtime_state ──────────────────────────────────────────
    state = {}
    _t0_inv_slot_counter = {}   # 先清零，再按断点恢复
    for code, cfg in yaml_targets.items():
        saved = ledger_snapshot.get(code, {})
        recovered_inv = saved.get('t0_inventory', {})
        recovered_slot = int(saved.get('slot_counter', 0))
        recovered_base = float(saved.get('base_price', 0.0))
        recovered_last_buy = float(saved.get('last_buy_price', 0.0))

        # 过滤掉 status != holding 的已平仓抽屉，防止重复卖出
        active_inv = {
            k: v for k, v in recovered_inv.items()
            if v.get('status') == 'holding' and v.get('filled_qty', 0) > 0
        }

        if active_inv:
            _t0_inv_slot_counter[code] = recovered_slot
            holding_vol = sum(v['filled_qty'] for v in active_inv.values())
            print(f"   ✅ [{code}] 恢复 {len(active_inv)} 个抽屉，持股 {holding_vol} 股，"
                  f"slot_counter={recovered_slot}")
        else:
            _t0_inv_slot_counter[code] = 0

        state[code] = {
            "name":           str(cfg.get("name", code)),
            "tag":            str(cfg.get("tag", "Other")),
            "trade_amount":   int(cfg.get("trade_amount", 0)),
            "atr_multiplier": float(cfg.get("atr_multiplier", 1.0)),
            "max_lots":       int(cfg.get("max_lots", 5)),
            # ▼ 核心：断点续传 — 有账本则恢复，无账本则为空字典
            "t0_inventory":   active_inv,
            "base_price":     recovered_base,
            "last_buy_price": recovered_last_buy,
            "status":         "active",
            "spread_pct":     float(cfg.get("spread_pct", 0.01)),
            "_atr_pct":       float(cfg.get("spread_pct", 0.01)),
        }
    print(f"✅ [Reconcile-v4] Inventory 初始化完毕，T0 管辖标的: {list(state.keys())}")
    return state


def _inv_lots(rs: dict) -> int:
    """从 t0_inventory 统计持有中的抽屉数量（替代 current_lots）"""
    return sum(1 for s in rs.get("t0_inventory", {}).values()
               if s.get("status") == "holding" and s.get("filled_qty", 0) > 0)


def _inv_volume(rs: dict) -> int:
    """从 t0_inventory 汇总持有总股数（替代 volume）"""
    return sum(s.get("filled_qty", 0)
               for s in rs.get("t0_inventory", {}).values()
               if s.get("status") == "holding")


def _t0_get_min_holding_price(rs: dict) -> float:
    """[物理价差锁 v2] T0 锚点提取：
    同时检查 t0_inventory(已成交) + _t0_pending(未确认 buy)，
    返回最低真实/待确认买入价。
    零持仓且无 pending 时返回 inf，首格建仓完全透明。

    修复：原版只查 inventory，pending 状态下 inventory 为空 → 价差锁失效。
    """
    prices = [
        float(s.get("buy_price", 0.0))
        for s in rs.get("t0_inventory", {}).values()
        if s.get("status") == "holding" and float(s.get("buy_price", 0.0)) > 0
    ]
    # 同时把尚未回调确认的 pending buy 也纳入锚点
    code = rs.get('_code_ref')  # 由主循环写入
    if code:
        with _t0_pending_lock:
            for meta in _t0_pending.values():
                if meta.get('code') == code and meta.get('direction') == 'buy':
                    p = float(meta.get('buy_price', 0.0))
                    if p > 0:
                        prices.append(p)
    return min(prices) if prices else float("inf")

def _save_runtime_state(rs: dict, path: str):
    """将内存 runtime_state 序列化为 JSON 落盘（含 IO 锁保护）"""
    with _runtime_io_lock:
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(rs, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ 状态落盘失败: {e}")


def _save_t0_state():
    """[状态持久化] 将 runtime_state 中所有标的的 t0_inventory 快照写入 T0_LEDGER_FILE。

    调用时机：on_order_trade 回调中每次 inventory 实质性变更（新建/累加/销毁）后立即调用。
    线程安全：调用方必须已持有 _runtime_io_lock，或在函数内部加锁（此处在加锁外调用即可）。
    格式：{"code": {"t0_inventory": {...}, "slot_counter": int}, ...}
    注意：本函数内部独立加锁，不依赖上层锁状态，防止回调并发竞争。
    """
    with _runtime_io_lock:
        try:
            snapshot = {}
            for code, rs in runtime_state.items():
                inv = rs.get("t0_inventory")
                if inv is not None:  # 包括空字典，标记为「已清仓」
                    snapshot[code] = {
                        "t0_inventory":  inv,
                        "slot_counter":  _t0_inv_slot_counter.get(code, 0),
                        "base_price":    rs.get("base_price", 0.0),
                        "last_buy_price": rs.get("last_buy_price", 0.0),
                        "saved_at":      time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp_path = T0_LEDGER_FILE + ".tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(snapshot, f, indent=2, ensure_ascii=False)
            # 原子替换：先写 .tmp 再 rename，防止写到一半进程崩溃导致文件损坏
            os.replace(tmp_path, T0_LEDGER_FILE)
        except Exception as e:
            print(f"⚠️ [Ledger] t0_ledger.json 落盘失败: {e}")


def _purge_t0_ledger():
    """[EOD 物理销毁] 收盘清仓完成后调用，删除 t0_ledger.json。
    确保次日系统以绝对干净的零库存状态重启，不携带任何过期抽屉记录。
    """
    with _runtime_io_lock:
        try:
            if os.path.exists(T0_LEDGER_FILE):
                os.remove(T0_LEDGER_FILE)
                print("🧹 [EOD·Purge] t0_ledger.json 已物理销毁，次日零库存启动就绪。")
            tmp_path = T0_LEDGER_FILE + ".tmp"
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception as e:
            print(f"⚠️ [EOD·Purge] 删除 t0_ledger.json 失败: {e}")

def _get_trader_session():
    """封装 QMT 交易网关的连接和账户订阅逻辑"""
    if not _ensure_qmt_ready():
        print("❌ miniQMT 客户端未就绪，Auto Pilot 中止启动。")
        return None, None

    session_id = int(time.time())
    xt_trader = XtQuantTrader(QMT_PATH, session_id)
    xt_trader.register_callback(GridTraderCallback())
    xt_trader.start()

    MAX_CONN_RETRY = 5
    conn_result = -1
    for _attempt in range(1, MAX_CONN_RETRY + 1):
        conn_result = xt_trader.connect()
        if conn_result == 0:
            break
        print(f"   ⏳ 交易网关未就绪 (错误码: {conn_result})，10 秒后重试 ({_attempt}/{MAX_CONN_RETRY})...")
        time.sleep(10)

    if conn_result != 0:
        msg = (f"QMT 交易网关连接失败 (错误码: {conn_result})，"
               f"已重试 {MAX_CONN_RETRY} 次，请检查 QMT_PATH 配置。")
        print(f"❌ {msg}")
        send_n8n_alert("🚨 QMT 连接失败", msg)
        return None, None

    acc = StockAccount(ACCOUNT_ID)
    sub_result = xt_trader.subscribe(acc)
    if sub_result != 0:
        print(f"❌ 账户订阅失败 (错误码: {sub_result})，请检查账号配置。")
        return None, None
    print(f"✅ QMT 交易网关连接成功，账户 {ACCOUNT_ID} 已就绪。")
    time.sleep(1)
    return xt_trader, acc

def run_multi_grid():
    global last_mtime, runtime_state

    # ── 0. 进程唯一性锁 ─────────────────────────────────────────
    os.makedirs(STATE_DIR, exist_ok=True)
    LOCK_FILE = os.path.join(STATE_DIR, "executor.lock")
    if not acquire_lock_with_ttl(LOCK_FILE): return

    try:
        log(f"{'='*60}")
        log(f"🚀 T0 Auto Pilot 4.0 启动 (Fill-Based v2) | PID={os.getpid()} | 日志={_log_path}")
        log(f"{'='*60}")
        print(f"[{time.strftime('%H:%M:%S')}] [Start] T0 Auto Pilot 4.0 启动 (Fill-Based v2)...")

        # ── 1. 读取 YAML ─────────────────────────────────────────
        if not os.path.exists(TARGETS_FILE):
            print(f"❌ 找不到 {TARGETS_FILE}，请先运行 t0_master.py。")
            return
        with open(TARGETS_FILE, 'r', encoding='utf-8') as f:
            yaml_targets = yaml.safe_load(f) or {}
        last_mtime = os.path.getmtime(TARGETS_FILE)
        if not yaml_targets:
            print("❌ YAML 目标池为空，退出。")
            return
        active_codes = list(yaml_targets.keys())
        print(f"[YAML] 加载目标: {active_codes}")

        # ── 2. 确保 miniQMT 就绪并建立连接 ──────────────────────
        xt_trader, acc = _get_trader_session()
        if not xt_trader: return

        # ── 3. 🌊 潮汐资金池：基于真实现金 × 杠杆动态覆写 trade_amount ──
        try:
            account_obj = xt_trader.query_stock_asset(acc)
            real_cash = float(getattr(account_obj, 'cash', 0) or 0)
            safe_cash = max(0.0, real_cash - 10000)
            virtual_capital = safe_cash * CAPITAL_LEVERAGE

            yaml_total_budget = sum(
                int(cfg.get('trade_amount', 0)) * int(cfg.get('max_lots', 5))
                for cfg in yaml_targets.values()
                if isinstance(cfg, dict)
            )

            print(f"\n🌊 [潮汐资金池] 实盘可用现金: {real_cash:.2f} | "
                  f"杠杆: {CAPITAL_LEVERAGE}x | 虚拟购买力: {virtual_capital:.2f}")

            if virtual_capital < 30000:
                print(f"⚠️  [潮汐资金池] 虚拟资金 {virtual_capital:.2f} < 30000，放弃缩放。")
            elif yaml_total_budget <= 0:
                print("⚠️  [潮汐资金池] YAML 总需求为零，跳过缩放。")
            else:
                scale_factor = virtual_capital / yaml_total_budget
                print(f"⚖️  [缩放计算] 原始需求: {yaml_total_budget} | 缩放: {scale_factor:.3f}x")
                for code, cfg in yaml_targets.items():
                    if not isinstance(cfg, dict): continue
                    orig = int(cfg.get('trade_amount', 0))
                    if orig <= 0: continue
                    scaled = int((orig * scale_factor) / 100) * 100
                    cfg['trade_amount'] = max(100, scaled)
                    print(f"   [{code}] trade_amount: {orig} → {cfg['trade_amount']}")
        except Exception as _e:
            print(f"⚠️  [潮汐资金池] 查询账户失败，使用 YAML 原始配置: {_e}")

        # ── 4. Fill-Based v2: 零库存基准初始化 ──────────────────
        runtime_state.update(reconcile_positions_with_real(yaml_targets))
        json_state = runtime_state  # backward-compat alias

        # ── 5. 订阅行情 ─────────────────────────────────────────
        all_codes = list(set(active_codes))
        all_codes = [c for c in all_codes if _VALID_CODE.match(str(c))]
        for code in all_codes:
            xtdata.subscribe_quote(code, period='tick')
        time.sleep(2)
        ticks = xtdata.get_full_tick(all_codes)

        # ── 6. 初始化价格中枢（用当前 Tick）────────────────────
        for code in all_codes:
            rs = runtime_state.get(code)
            if rs and rs.get('base_price', 0) <= 0:
                tp = ticks.get(code, {}).get('lastPrice', 0)
                if tp > 0:
                    rs['base_price'] = tp
                    rs['last_buy_price'] = 0.0

        # ── 7. 策略领土防火墙（读取 T1/轮动/狙击账本）────────────
        sniper_codes = []
        sniper_file = ".state/sniper_holdings.json"
        if os.path.exists(sniper_file):
            try:
                with open(sniper_file, 'r') as f:
                    sniper_codes = list(json.load(f).keys())
            except: pass

        rotation_protected_codes: set = set()
        _rot_targets_file = os.path.join(STATE_DIR, "rotation_targets.yaml")
        if os.path.exists(_rot_targets_file):
            try:
                with open(_rot_targets_file, 'r', encoding='utf-8') as f:
                    _rd = yaml.safe_load(f) or {}
                    rotation_protected_codes.update(_rd.get('targets', []))
            except: pass
        _rot_holdings_file = os.path.join(STATE_DIR, "rotation_holdings.json")
        if os.path.exists(_rot_holdings_file):
            try:
                with open(_rot_holdings_file, 'r', encoding='utf-8') as f:
                    rotation_protected_codes.update((json.load(f) or {}).keys())
            except: pass

        t1_protected_codes: set = set()
        _t1_ledger_file = os.path.join(STATE_DIR, "t1_grid_ledger.yaml")
        if os.path.exists(_t1_ledger_file):
            try:
                with open(_t1_ledger_file, 'r', encoding='utf-8') as f:
                    _t1d = yaml.safe_load(f) or {}
                    t1_protected_codes.update(_t1d.keys())
                print(f"🛡️ [Firewall/T1] 保护 T1 标的: {sorted(t1_protected_codes)}")
            except: pass

        all_protected_codes = rotation_protected_codes | t1_protected_codes
        # T0 自身日内零库存，不检测孤儿（孤儿检测依赖 QMT 总持仓，已废除）
        auto_ignore_codes: set = set()

        _save_runtime_state(runtime_state, STATE_FILE)
        print("\n[Status] Fill-Based v2 状态机初始化完毕，进入轮询侦听状态...")

        # ─────────────────────────────────────────────────────────
        # 9. 核心大循环
        # ─────────────────────────────────────────────────────────
        all_active_codes = list(runtime_state.keys())
        _reconnect_attempts = 0
        MAX_RECONNECT = 3
        _heartbeat_last = 0
        _t0_sweep_t = 0.0

        try:
            while True:
                # ══ 收盘自动退出 ════════════════════════════════════
                if _now_hhmm() >= "1530":
                    log(f"🌙 [EOD] {_now_hhmm()} >= 15:30，交易结束，T0 引擎主循环正常退出。")
                    print(f"[{time.strftime('%H:%M:%S')}] 🌙 [EOD] 收盘退出，T0 引擎安全停止。")
                    break
                # ════════════════════════════════════════════════════
                # ── YAML 物理热重载 ──────────────────────────────
                try:
                    current_mtime = os.path.getmtime(TARGETS_FILE)
                    if current_mtime != last_mtime:
                        print(f"\n🔄 [Hot Reload] 检测到 {TARGETS_FILE} 变更，重载中...")
                        with open(TARGETS_FILE, 'r', encoding='utf-8') as f:
                            new_yaml = yaml.safe_load(f) or {}
                        for code, cfg in new_yaml.items():
                            if code not in runtime_state:
                                print(f"   🆕 新合约: {code}，动态注入网格线...")
                                xtdata.subscribe_quote(code, period='tick')
                                fresh_ticks = xtdata.get_full_tick([code])
                                tick_p = fresh_ticks.get(code, {}).get('lastPrice', 0)
                                runtime_state[code] = {
                                    "name":           str(cfg.get('name', code)),
                                    "tag":            str(cfg.get('tag', 'Other')),
                                    "trade_amount":   int(cfg.get('trade_amount', 0)),
                                    "atr_multiplier": float(cfg.get('atr_multiplier', 1.0)),
                                    "max_lots":       int(cfg.get('max_lots', 5)),
                                    "t0_inventory":   {},   # ← 新抽屉，等待回调填充
                                    "base_price":     float(tick_p),
                                    "last_buy_price": 0.0,
                                    "status":         "active",
                                    "spread_pct":     float(cfg.get('spread_pct', 0.02)),
                                    "_atr_pct":       float(cfg.get('spread_pct', 0.02)),
                                }
                            else:
                                runtime_state[code]['trade_amount']   = int(cfg.get('trade_amount', 0))
                                runtime_state[code]['max_lots']       = int(cfg.get('max_lots', 5))
                                runtime_state[code]['atr_multiplier'] = float(cfg.get('atr_multiplier', 1.0))
                        for code in list(runtime_state.keys()):
                            if code not in new_yaml and runtime_state[code]['status'] == 'active':
                                print(f"   🏚️ {code} 从配置移除，切换为 sell_only...")
                                runtime_state[code]['status'] = 'sell_only'
                        last_mtime = current_mtime
                        all_active_codes = list(runtime_state.keys())
                        print(f"✅ [Hot Reload] 清洗完毕！当前合规巡逻标的: {all_active_codes}")
                        _save_runtime_state(runtime_state, STATE_FILE)
                except Exception as e:
                    print(f"⚠️ 热重载探针异常: {e}")

                try:
                    # ── 9a. 心跳 ────────────────────────────────
                    if time.time() - _heartbeat_last > 600:
                        print(f"💓 [Heartbeat] {time.strftime('%H:%M:%S')} - 系统健康轮询中...")
                        _heartbeat_last = time.time()

                    # ── 补丁C：pending 超时巡检 ──────────────────
                    if xt_trader and time.time() - _t0_sweep_t > PENDING_SWEEP_SEC:
                        _t0_sweep_stale_pending(xt_trader, acc)
                        _t0_sweep_t = time.time()

                    # ── 9b. 拉取全量 Tick ────────────────────────
                    ticks = xtdata.get_full_tick(all_active_codes)
                    _reconnect_attempts = 0

                    # ── 9b. 盘口保护窗口 ─────────────────────────
                    in_guard = _in_guard_window()
                    if in_guard:
                        print(f"[{_now_hhmm()}] [盘口保护] 处于开盘/午盘流动性恢复期，本轮跳过。")
                        time.sleep(5)
                        continue

                    # ── 9c. 逐标的处理 ──────────────────────────
                    for code in list(runtime_state.keys()):
                        rs = runtime_state[code]

                        if code in auto_ignore_codes:
                            continue
                        if rs['status'] == 'halted':
                            continue

                        tick = ticks.get(code)
                        if tick is None:
                            continue

                        current_price = tick.get('lastPrice', 0)
                        volume        = tick.get('volume', 0)

                        if current_price <= 0 or volume == 0:
                            continue

                        # ★ 实时 ATR_pct 估算
                        ask_now = tick.get('askPrice', [current_price])[0]
                        bid_now = tick.get('bidPrice', [current_price])[0]
                        if ask_now > 0 and bid_now > 0 and current_price > 0:
                            raw_atr = (ask_now - bid_now) / current_price * 50
                            raw_atr = max(0.005, min(0.04, raw_atr))
                        else:
                            raw_atr = rs.get('_atr_pct', 0.02)
                        rs['_atr_pct'] = raw_atr

                        # 🛡️ 物理阻断器 Clamp
                        # [修复] safe_spread_pct 必须以 YAML spread_pct 为下限
                        # 原版：max(0.005, min(0.012, raw)) 会把 YAML 的 1.04% 压低至 0.5%
                        # 正确：ATR 动态值只能在 YAML 配置上方浮动，不能向下突破
                        yaml_spread_pct = float(rs.get('spread_pct', 0.01))
                        raw_spread_pct  = raw_atr * rs.get('atr_multiplier', 1.0)
                        if code == '518680.SH':
                            safe_spread_pct = max(yaml_spread_pct, raw_spread_pct)
                        else:
                            safe_spread_pct = max(yaml_spread_pct, raw_spread_pct)

                        # 写入 rs._code_ref 供 _t0_get_min_holding_price 识别 pending
                        rs['_code_ref'] = code

                        # ★ 跳空 Gap 检测 → 重置中枢
                        base_price = rs['base_price']
                        gap_ratio  = abs(current_price - base_price) / base_price if base_price > 0 else 0
                        if gap_ratio > safe_spread_pct * 2 and rs.get('_gap_reset_done') is None:
                            print(f"[{code}] [Gap重置] 跳空 {gap_ratio*100:.2f}%，"
                                  f"中枢重置: {base_price:.3f} → {current_price:.3f}")
                            rs['base_price']      = current_price
                            rs['_gap_reset_done'] = True
                            base_price = current_price

                        actual_spread = base_price * safe_spread_pct

                        # 🚨 巡逻日志
                        last_log_time = rs.get('_last_patrol_time', 0)
                        if time.time() - last_log_time > PATROL_INTERVAL:
                            target_buy_price = base_price - actual_spread
                            dist_to_buy = (current_price / target_buy_price - 1) * 100 if target_buy_price > 0 else 0
                            log_parts = [f"🔍 [{code}] [{rs.get('tag', 'Other')}] {rs['name']} 巡逻"]
                            if rs['status'] == 'sell_only':
                                log_parts.append("[状态: Sell_Only]")
                            if dist_to_buy > 0:
                                log_parts.append(f"[距离下轨还有 {dist_to_buy:.2f}%]")
                            else:
                                log_parts.append("[已触及下轨/待买入]")
                            iopv = tick.get('iopv', 0)
                            if iopv > 0:
                                p_rate = (current_price / iopv - 1) * 100
                                log_parts.append(f"[当前溢价: {p_rate:.2f}%]")
                            print(" ".join(log_parts))
                            rs['_last_patrol_time'] = time.time()

                        # ★ 任务3：深渊阻断器（仅 active 且持仓满格时检查）
                        if rs['status'] == 'active' and _inv_lots(rs) >= rs['max_lots'] > 0:
                            avg_cost = rs.get('last_buy_price', 0)  # Fill-based: 使用内部买入记录
                            last_buy = rs.get('last_buy_price', 0)
                            abyss_by_avg  = avg_cost > 0 and current_price <= avg_cost * ABYSS_AVG_COST_PCT
                            abyss_by_last = last_buy > 0 and current_price <= last_buy  * ABYSS_LAST_BUY_PCT

                            if abyss_by_avg or abyss_by_last:
                                reason = (f"均价 {avg_cost:.3f}×92%={avg_cost*0.92:.3f}" if abyss_by_avg
                                          else f"最后买入 {last_buy:.3f}×97%={last_buy*0.97:.3f}")
                                print(f"💥 [{code}] [灾难止损] 触发！当前价 {current_price:.3f} 跌破 {reason}，头寸已清空！")
                                send_n8n_alert(
                                    f"💥 [{code}] 灾难止损触发",
                                    f"当前价 {current_price:.3f} | {reason}\n头寸已市价全清！"
                                )
                                bid1 = tick.get('bidPrice', [current_price])[0]
                                sell_price = round(bid1 - 0.001, 3)
                                qty = (_inv_volume(rs) // 100) * 100
                                qty = max(0, qty)

                                seq = xt_trader.order_stock(
                                    acc, code, xtconstant.STOCK_SELL,
                                    qty, xtconstant.FIX_PRICE, sell_price, 'T0_Grid', 'T0_SL'
                                ) if xt_trader else -1
                                if seq > 0:
                                    with _t0_pending_lock:
                                        _t0_pending[seq] = {
                                            "code": code, "direction": "sell",
                                            "qty": qty, "sent_at": time.time()
                                        }
                                    rs['status'] = 'halted'
                                    _save_runtime_state(runtime_state, STATE_FILE)
                                print(f"💥 [{code}] [灾难止损] 指令已发，单号: {seq}")
                                continue

                        # ★ 获取对手价（Taker 模式）
                        ask1 = tick.get('askPrice', [current_price])[0]
                        bid1 = tick.get('bidPrice', [current_price])[0]
                        buy_price        = round(ask1 + 0.001, 3)
                        sell_price_taker = round(bid1 - 0.001, 3)
                        if sell_price_taker <= 0:
                            sell_price_taker = round(current_price * 0.999, 3)
                        if buy_price <= 0:
                            buy_price = round(current_price * 1.001, 3)

                        # ── 计算买入手数 ──────────────────────────
                        trade_amount = rs['trade_amount']
                        if trade_amount > 0:
                            buy_qty = math.floor((trade_amount / current_price) / 100) * 100
                        else:
                            buy_qty = 0
                        max_lots = rs['max_lots']

                        # ──────── 【低吸】买入逻辑 ────────────────
                        _buy_allowed = rs['status'] == 'active' and buy_qty >= 100 and _now_hhmm() < "1400"

                        if rs['status'] == 'active' and buy_qty >= 100 and _now_hhmm() >= "1400":
                            # [节流] 尾盘熔断提示每标的最多每60秒打一次，避免日志暴涨
                            _fuse_last = rs.get('_fuse_log_time', 0)
                            if time.time() - _fuse_last >= 60:
                                print(f"  [{code}] [尾盘熔断] {_now_hhmm()} >= 14:00，日内买入通道物理焊死。")
                                rs['_fuse_log_time'] = time.time()

                        if _buy_allowed:
                            # 🛡️ 瀑布熔断
                            pre_close = tick.get('lastClose', tick.get('preClose', tick.get('lastPrice', 0)))
                            if pre_close > 0:
                                intraday_drop = current_price / pre_close - 1
                                if intraday_drop < WATERFALL_FUSE_PCT:
                                    print(f"  [{code}] [瀑布熔断] 盘中暴跌 {intraday_drop*100:.2f}%，冻结买入。")
                                    _buy_allowed = False

                            # 🛡️ 防绞杀熔断
                            if _buy_allowed:
                                daily_open = tick.get('open', tick.get('openPrice', 0))
                                if daily_open > 0 and current_price < daily_open * 0.97:
                                    print(f"  [{code}] [防绞杀熔断] 跌破开盘价-3%，当日买入封锁。")
                                    _buy_allowed = False

                            # 溢价率风控已废除：T0 标的由人工审核写入 YAML，不再自动过滤

                            # ── 物理价差锁（Dynamic Price Spacing Lock）──────
                            # 核心法则：下一格触发线不得高于「最低持仓成交价 - 1个完整spread」
                            # 防止跳空后在同价位连续买入多格，消耗子弹却无法拉开成本空间。
                            # 零持仓时 _t0_get_min_holding_price 返回 inf，首格不受拦截。
                            _t0_min_fill = _t0_get_min_holding_price(rs)
                            _t0_dyn_ceil = _t0_min_fill * (1.0 - safe_spread_pct)
                            _t0_static_rail = base_price - actual_spread
                            _t0_effective_rail = min(_t0_static_rail, _t0_dyn_ceil)
                            if _t0_dyn_ceil < _t0_static_rail:
                                print(
                                    f"  [{code}] 🔒 [价差锁] 最低持仓价={_t0_min_fill:.4f} "
                                    f"动态天花板={_t0_dyn_ceil:.4f} "
                                    f"(静态下轨={_t0_static_rail:.4f} 现价={current_price:.4f}) "
                                    f"→ {'✅通过' if current_price <= _t0_dyn_ceil else '🚫拦截，价差不足一格'}"
                                )
                            # ─────────────────────────────────────────────────

                            if _buy_allowed and current_price <= _t0_effective_rail \
                                    and _inv_lots(rs) < max_lots:
                                # Fill-Based v3 Inventory: 注册 pending 携带 grid_slot，不乐观写账
                                slot_id = _t0_inv_slot_counter.get(code, 0) + 1
                                _t0_inv_slot_counter[code] = slot_id
                                seq = xt_trader.order_stock(
                                    acc, code, xtconstant.STOCK_BUY,
                                    buy_qty, xtconstant.FIX_PRICE, buy_price, 'T0_Grid', 'T0_Buy'
                                ) if xt_trader else 77777
                                print(f"✅ 买单已提交: {code} | 价格: {buy_price:.3f} | 数量: {buy_qty} | 抽屉: slot_{slot_id} | 单号: {seq}")

                                if seq > 0:
                                    with _t0_pending_lock:
                                        _t0_pending[seq] = {
                                            "code": code, "direction": "buy",
                                            "qty":  buy_qty, "sent_at": time.time(),
                                            "grid_slot": str(slot_id),
                                            "buy_price": buy_price,  # ← [修复] 供价差锁 pending 查询
                                        }
                                    rs['base_price']     = current_price
                                    rs['last_buy_price'] = current_price
                                    _save_runtime_state(runtime_state, STATE_FILE)

                                record_action(
                                    strategy="T0_Grid", action="买入", target=code,
                                    price=buy_price,
                                    reason=f"触及下轨低吸，当前抽屉数: {_inv_lots(rs)}",
                                    extra={"qty": buy_qty, "seq": seq, "slot": slot_id}
                                )
                                ML_LOGGER.record({
                                    "timestamp":      time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "code":           code, "name": rs['name'],
                                    "action":         "Buy_Order", "price": current_price,
                                    "volume":         buy_qty, "base_price": rs['base_price'],
                                    "spread_pct":     rs['spread_pct'],
                                    "current_lots":   _inv_lots(rs),
                                    "max_lots":       max_lots,
                                    "status":         rs['status'],
                                    "last_buy_price": rs['last_buy_price']
                                })

                        # ─────────────────────────────────────────
                        # sell_only 孤儿清退（Inventory 版）
                        # ─────────────────────────────────────────
                        if rs['status'] == 'sell_only':
                            actual_sell_qty = (_inv_volume(rs) // 100) * 100
                            if actual_sell_qty > 0:
                                seq = xt_trader.order_stock(
                                    acc, code, xtconstant.STOCK_SELL,
                                    actual_sell_qty, xtconstant.FIX_PRICE, sell_price_taker,
                                    'T0_Grid', 'T0_Orphan'
                                ) if xt_trader else 88892
                                if seq > 0:
                                    with _t0_pending_lock:
                                        _t0_pending[seq] = {
                                            'code': code, 'direction': 'sell',
                                            'qty': actual_sell_qty, 'sent_at': time.time()
                                        }
                                print(f"🏚️ [{code}] {rs['name']} 孤儿一次性全清（Inv）！"
                                      f"{actual_sell_qty} 股 @ {sell_price_taker:.3f} | 单号: {seq}")
                                send_n8n_alert(
                                    f"🏚️ 孤儿清退 {code}",
                                    f"{rs['name']} 一次性卖出 {actual_sell_qty} 股 @ {sell_price_taker:.3f}"
                                )
                                record_action(
                                    strategy="T0_Grid", action="孤儿清退", target=code,
                                    price=sell_price_taker,
                                    reason="孤儿资产一次性全清（不在YAML目标中）",
                                    extra={"qty": actual_sell_qty, "seq": seq}
                                )
                                ML_LOGGER.record({
                                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "code": code, "name": rs['name'],
                                    "action": "Orphan_FullSell", "price": sell_price_taker,
                                    "volume": actual_sell_qty, "status": "sell_only"
                                })
                                del runtime_state[code]
                                all_active_codes = list(runtime_state.keys())
                                _save_runtime_state(runtime_state, STATE_FILE)
                            continue  # sell_only 处理完毕

                        # ═══════════════════════════════════════════
                        # 【高抛 / EOD】网格卖出（active 专属）
                        # ═══════════════════════════════════════════
                        if rs['status'] == 'active' and _inv_lots(rs) > 0:
                            try:
                                # Fill-Based v3 Inventory: 从抽屉账本读取持仓，不信 QMT
                                total_inv_vol = _inv_volume(rs)
                                if total_inv_vol <= 0:
                                    print(f"🌖 [清仓识别] {code} inventory 总股=0，账本已清零。")
                                    _save_runtime_state(runtime_state, STATE_FILE)
                                    continue

                                tracked_cost = rs.get('base_price', 0.0)
                                actual_profit_pct = (
                                    (current_price / tracked_cost) - 1
                                    if tracked_cost > 0 else 0
                                )

                                # 🌋 绝对止盈（3%，全清）
                                if actual_profit_pct >= ABSOLUTE_TP_PCT:
                                    total_qty = (total_inv_vol // 100) * 100
                                    seq = xt_trader.order_stock(
                                        acc, code, xtconstant.STOCK_SELL,
                                        total_qty, xtconstant.FIX_PRICE, sell_price_taker,
                                        'T0_Grid', 'T0_TP'
                                    ) if xt_trader else 88889
                                    if seq > 0:
                                        with _t0_pending_lock:
                                            _t0_pending[seq] = {
                                                'code': code, 'direction': 'sell',
                                                'qty': total_qty, 'sent_at': time.time()
                                            }
                                    print(f"🌋 [{code}] 绝对止盈！浮盈 {actual_profit_pct*100:.2f}% | 单号:{seq}")
                                    send_n8n_alert(
                                        f"🌋 [{code}] 止盈",
                                        f"浮盈 {actual_profit_pct*100:.2f}%，全清指令已发"
                                    )
                                    continue

                                # 🕳️ 深渊阻断（-8%均价 或 -3%最后买价，全清）
                                last_buy = rs.get('last_buy_price', 0)
                                if (current_price <= tracked_cost * ABYSS_AVG_COST_PCT) or \
                                   (last_buy > 0 and current_price <= last_buy * ABYSS_LAST_BUY_PCT):
                                    total_qty_sl = (total_inv_vol // 100) * 100
                                    seq = xt_trader.order_stock(
                                        acc, code, xtconstant.STOCK_SELL,
                                        total_qty_sl, xtconstant.FIX_PRICE, sell_price_taker,
                                        'T0_Grid', 'T0_SL'
                                    ) if xt_trader else 88890
                                    if seq > 0:
                                        with _t0_pending_lock:
                                            _t0_pending[seq] = {
                                                'code': code, 'direction': 'sell',
                                                'qty': total_qty_sl, 'sent_at': time.time()
                                            }
                                    print(f"🕳️ [{code}] 深渊阻断触发！全清 {total_qty_sl} 股 | 单号:{seq}")
                                    continue

                            except Exception as e:
                                print(f"⚠️ [网格卖出 TP/Sync 检查异常]: {e}")

                            upper_rail = base_price + actual_spread

                            # ★★★ EOD 弹层式强制清仓（14:52 触发）★★★
                            # 绝不依赖 volume 全局计数器。
                            # 逐一遍历 t0_inventory holding 抽屉发单，发单后强行归零 inventory。
                            if _now_hhmm() >= "1452":
                                inv = rs.get("t0_inventory", {})
                                holding_slots = {
                                    k: v for k, v in inv.items()
                                    if v.get("status") == "holding" and v.get("filled_qty", 0) > 0
                                }
                                if holding_slots:
                                    eod_seqs = []
                                    for slot_k, slot_v in sorted(holding_slots.items(),
                                                                  key=lambda x: int(x[0]) if str(x[0]).isdigit() else 0):
                                        eod_qty = (slot_v["filled_qty"] // 100) * 100
                                        if eod_qty <= 0:
                                            continue
                                        seq = xt_trader.order_stock(
                                            acc, code, xtconstant.STOCK_SELL,
                                            eod_qty, xtconstant.FIX_PRICE, sell_price_taker,
                                            'T0_Grid', 'T0_EOD'
                                        ) if xt_trader else 88891
                                        if seq > 0:
                                            with _t0_pending_lock:
                                                _t0_pending[seq] = {
                                                    'code': code, 'direction': 'sell',
                                                    'qty': eod_qty, 'sent_at': time.time()
                                                }
                                        eod_seqs.append((slot_k, eod_qty, seq))

                                    if eod_seqs:
                                        total_eod = sum(q for _, q, _ in eod_seqs)
                                        slot_detail = " | ".join(f"slot{k}={q}股" for k, q, _ in eod_seqs)
                                        print(f"🌙 [{code}] [收盘弹层清仓] 14:52 触发！共出 {total_eod}股 @ {sell_price_taker:.3f}")
                                        print(f"   抽屉明细: {slot_detail}")
                                        send_n8n_alert(
                                            f"🌙 [{code}] 收盘弹层清仓",
                                            f"14:52 触发 | 出 {total_eod}股 @ {sell_price_taker:.3f}\n{slot_detail}"
                                        )
                                        record_action(
                                            strategy="T0_Grid", action="收盘清仓", target=code,
                                            price=sell_price_taker,
                                            reason="14:52 弹层式全清，防止 T0 抽屉留存过夜",
                                            extra={"qty": total_eod, "slots": len(eod_seqs)}
                                        )

                                    # ★★★ 关键：EOD 卖单全部发出后，强行归零 inventory ★★★
                                    with _runtime_io_lock:
                                        rs["t0_inventory"] = {}
                                    _save_runtime_state(runtime_state, STATE_FILE)
                                    # 🧹 [EOD 物理销毁] EOD 清仓后立即销毁 t0_ledger.json
                                    # 必须在所有标的的 EOD 清仓均发出后执行（逐标的检查，最后一次触发时销毁）
                                    # 此处每个 holding_slots 非空的标的处理完毕后各自触发一次销毁，幂等安全
                                    _purge_t0_ledger()
                                continue  # EOD 已处理，本轮跳过

                            if current_price < upper_rail:
                                continue

                            # 常规网格高抛：从最老 holding slot 读取精确卖出量（铁律三：绝对对称）
                            inv = rs.get("t0_inventory", {})
                            oldest_slot = None
                            for slot_k in sorted(inv.keys(), key=lambda x: int(x) if str(x).isdigit() else 0):
                                if inv[slot_k].get("status") == "holding" and inv[slot_k].get("filled_qty", 0) > 0:
                                    oldest_slot = slot_k
                                    break
                            sell_qty = (inv[oldest_slot]["filled_qty"] // 100) * 100 if oldest_slot else 0
                            if sell_qty <= 0:
                                continue

                            seq = xt_trader.order_stock(
                                acc, code, xtconstant.STOCK_SELL,
                                sell_qty, xtconstant.FIX_PRICE, sell_price_taker,
                                'T0_Grid', 'T0_Sell'
                            ) if xt_trader else 88888
                            if seq > 0:
                                with _t0_pending_lock:
                                    _t0_pending[seq] = {
                                        "code": code, "direction": "sell",
                                        "qty": sell_qty, "sent_at": time.time()
                                    }
                                rs['base_price'] = current_price
                                _save_runtime_state(runtime_state, STATE_FILE)

                            print(f"✅ [{code}] 高抛 | 单号:{seq} | slot={oldest_slot} 卖 {sell_qty}股 | 剩余抽屉:{_inv_lots(rs)}")
                            record_action(
                                strategy="T0_Grid", action="卖出", target=code,
                                price=current_price, reason="触及网格上轨完成套利",
                                extra={"qty": sell_qty, "seq": seq, "slot": oldest_slot}
                            )
                            ML_LOGGER.record({
                                "timestamp":    time.strftime("%Y-%m-%d %H:%M:%S"),
                                "code":         code, "name": rs['name'],
                                "action":       "Sell_Order", "price": current_price,
                                "volume":       sell_qty, "base_price": rs['base_price'],
                                "spread_pct":   rs['spread_pct'],
                                "current_lots": _inv_lots(rs), "max_lots": max_lots,
                                "status":       rs['status']
                            })
                            _save_runtime_state(runtime_state, STATE_FILE)

                    time.sleep(0.5)

                except KeyboardInterrupt:
                    raise

                except Exception as e:
                    _reconnect_attempts += 1
                    err_msg = str(e)
                    print(f"\n🔴 【运行时断线】 捕获到异常 (第 {_reconnect_attempts} 次)：{err_msg}")
                    send_n8n_alert(
                        "🔴 QMT 运行时断线",
                        f"xtdata 连接中断（第 {_reconnect_attempts} 次），错误信息：{err_msg}\n正在自动尝试重连..."
                    )

                    if _reconnect_attempts > MAX_RECONNECT:
                        fatal_msg = f"已连续重连失败 {MAX_RECONNECT} 次，Auto Pilot 放弃重连，请人工介入。"
                        print(f"💀 {fatal_msg}")
                        send_n8n_alert("💀 QMT 彻底崩溃", fatal_msg)
                        raise SystemExit(1)

                    print(f"⏳ 等待 30 秒后重试连接 miniQMT...")
                    time.sleep(30)
                    try:
                        xt_trader, acc = _get_trader_session()
                        if not xt_trader:
                            print("❌ 重连失败，退出。")
                            break
                        all_active_codes = list(runtime_state.keys())
                        for code in all_active_codes:
                            xtdata.subscribe_quote(code, period='tick')
                        print("✅ 重连成功，继续轮询...")
                        _reconnect_attempts = 0
                    except Exception as re_e:
                        print(f"❌ 重连过程中异常: {re_e}")

        except KeyboardInterrupt:
            print("\n⚠️ 用户中断！正在安全退出...")

    finally:
        release_lock(LOCK_FILE)
        print("🔒 进程锁已释放，Auto Pilot 安全退出。")


def _t0_sweep_stale_pending(xt_trader, acc):
    """补丁C(Inventory版)：扫描超时 pending 订单，主动对账并补记入 t0_inventory 或废单回滚。"""
    now = time.time()
    stale = {}
    with _t0_pending_lock:
        for seq, meta in list(_t0_pending.items()):
            if now - meta.get('sent_at', now) > PENDING_TIMEOUT_SEC:
                stale[seq] = meta
    if not stale:
        return
    print(f"[T0·Sweep] 发现 {len(stale)} 个超时委托，逐一对账...")
    for seq, meta in stale.items():
        code      = meta['code']
        direction = meta['direction']
        qty       = meta['qty']
        try:
            trades = xt_trader.query_stock_trades(acc) or []
            filled  = sum(t.traded_volume for t in trades if t.order_id == seq)
            if filled > 0:
                rs = runtime_state.get(code)
                if rs:
                    with _runtime_io_lock:
                        if direction == 'buy':
                            # 买入超时补记：写入对应 grid_slot 抽屉
                            slot_key = str(meta.get("grid_slot", "0"))
                            inv = rs.setdefault("t0_inventory", {})
                            if slot_key not in inv:
                                inv[slot_key] = {"buy_price": 0.0, "filled_qty": 0, "status": "holding"}
                            inv[slot_key]["filled_qty"] += filled
                        else:
                            # 卖出超时补记：按最老 holding slot 递减
                            inv = rs.get("t0_inventory", {})
                            remaining = filled
                            for slot_k in sorted(inv.keys(), key=lambda x: int(x) if str(x).isdigit() else 0):
                                s = inv[slot_k]
                                if s.get("status") != "holding" or s.get("filled_qty", 0) <= 0:
                                    continue
                                deduct = min(s["filled_qty"], remaining)
                                s["filled_qty"] -= deduct
                                remaining -= deduct
                                if s["filled_qty"] <= 0:
                                    s["status"] = "sold"
                                if remaining <= 0:
                                    break
                    _save_runtime_state(runtime_state, STATE_FILE)
                print(f"  [T0·Sweep·Inv] seq={seq} {code} {direction} 补记 {filled} 股 | "
                      f"持仓格数:{_inv_lots(rs) if rs else 'N/A'}")
            else:
                print(f"  [T0·Sweep] seq={seq} {code} 未成交，废单回滚")
            with _t0_pending_lock:
                _t0_pending.pop(seq, None)
        except Exception as e:
            print(f"  [T0·Sweep] seq={seq} 对账异常: {e}")



if __name__ == "__main__":
    run_multi_grid()
