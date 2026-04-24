# ==============================================================================
# 🎯 [部署节点] : Quant-PC
# 职责: 兼容 T0/T1，使用【绝对物理白名单】判定基因，输出黄金席位
# ==============================================================================
import json, os, csv
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
from xtquant import xtdata

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

load_dotenv()
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")

from hurst_engine import calculate_hurst_variance_scaling
from rough_cir_estimator import calibrate_rough_cir

_DIR = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_FILE = os.path.join(_DIR, ".state/oracle_v2_universe.json") # 全局精英池
OUTPUT_SLOTS = os.path.join(_DIR, ".state", "etf_grid_slots.json")

# 🔒 【T0 绝对物理白名单】(你人工或爬虫维护的真理库)
T0_POOL_FILE = os.path.join(_DIR, ".state/t0_absolute_pool.csv") 

MAX_HURST = 0.45
MAX_ROUGH_HALFLIFE = 7.0
SLOT_COUNT = 3

# 本地数据回看窗口（天）：与 qmt_daily_sync.py 保持一致
LOCAL_LOOKBACK_DAYS = 250  # 本地已有（8-10月日线）


def send_webhook(title: str, message: str) -> None:
    """N8N 实时通知（失败静默，不阻断主逻辑）"""
    if not N8N_WEBHOOK_URL or not _HAS_REQUESTS:
        return
    try:
        requests.post(
            N8N_WEBHOOK_URL,
            json={
                "title":     title,
                "message":   message,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            timeout=5,
        )
    except Exception as e:
        print(f"⚠️ Webhook 发送失败: {e}")


def _last_trading_day_str() -> str:
    """推算最近一个交易日（自然日->跳过周末）。
    盘前运行：最近交易日 = 昨日（如果今日天 < 15:30）或今日。
    盘后运行：最近交易日 = 今日。
    注意：此处不过节假日，仅用于生成起始日期参数和数据新鲜度告警。
    """
    now  = datetime.now()
    day  = now.date()
    # 如果是周末，往前回挎到周五
    weekday = day.weekday()        # Monday=0 ... Sunday=6
    if weekday == 5:               # Saturday -> Friday
        day -= timedelta(days=1)
    elif weekday == 6:             # Sunday   -> Friday
        day -= timedelta(days=2)
    return day.strftime("%Y%m%d")


def _validate_local_data(market_data: dict, candidates: list) -> None:
    """本地数据新鲜度时间戳核对。

    逻辑：
    1. 对每只标的取 DataFrame 最后一行的索引（已是 YYYYMMDD 字符串或 int）
    2. 与 _last_trading_day_str() 比较
    3. 有任何一只标的数据老于 3 个交易日 → 黑体警告（不逃出，仅告警）
    """
    expected_day = _last_trading_day_str()   # e.g. '20260415'
    stale_codes  = []

    for code in candidates:
        df = market_data.get(code)
        if df is None or df.empty:
            continue
        # xtdata 返回的 DataFrame 索引可能是 int timestamp(ms) 或 str
        last_idx = df.index[-1]
        if isinstance(last_idx, (int, float)):
            # Unix 毫秒 → 转为 YYYYMMDD
            last_day_str = pd.Timestamp(last_idx, unit='ms').strftime('%Y%m%d')
        else:
            last_day_str = str(last_idx)[:8].replace('-', '')

        if last_day_str < expected_day:
            stale_codes.append(f"{code}(最新:{last_day_str})")

    if stale_codes:
        stale_str = ', '.join(stale_codes[:5])  # 最多印 5 只防滢屏
        more = f" +{len(stale_codes)-5} 更多" if len(stale_codes) > 5 else ""
        print(f"\n⚠️  [本地数据老旧告警] 期望最新交易日={expected_day}，但 {len(stale_codes)} 只标的数据滤后不匹配:")
        print(f"   {stale_str}{more}")
        print(f"   建议检查 qmt_daily_sync.py 是否在 15:30 后完成了日线同步。本次解算将使用旧数据继续。\n")
    else:
        print(f"\u2705 本地数据已最新，最新交易日 = {expected_day}，数据就绪。")

def _load_t0_set() -> set:
    """加载绝对 T0 基因库，转为 Hash Set 以实现微秒级检索。
    文件格式：无表头 CSV，每行第一列为标的代码（可选选第二列为名称）
    示例：501302.SH,南方指数etf
    """
    if not os.path.exists(T0_POOL_FILE):
        print(f"⚠️ 未找到 T0 物理白名单 {T0_POOL_FILE}，将全部降级为 T+1 处理！")
        return set()
    t0_codes = set()
    with open(T0_POOL_FILE, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        for row in reader:
            if row and row[0].strip():          # 第一列是标的代码
                code = row[0].strip()
                # 自动跳过表头行（如果第一列显然不是代码格式则跳过）
                if '代码' in code or 'code' in code.lower():
                    continue
                t0_codes.add(code)
    print(f"🔒 T0 物理白名单装载：{len(t0_codes)} 只")
    return t0_codes

def run_global_grid_master():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🛸 启动全局 ETF_OU_Grid 大脑 (绝对基因白名单版)...")
    
    # ── 加载精英候选池（带降级兜底）──────────────────────────────
    # 正常路径：读 oracle_v2_universe.json（由 refine_core_universe.py 周末刷新）
    # 降级路径：文件缺失时自动从 top100_liquidity.csv 重建，保证席位解算不因文件误删中断
    if os.path.exists(UNIVERSE_FILE):
        with open(UNIVERSE_FILE, 'r', encoding='utf-8') as f:
            pool = json.load(f).get('universe', [])
        print(f"✅ [Universe] 读取 oracle_v2_universe.json，共 {len(pool)} 只候选标的")
    else:
        _TOP100_CSV = os.path.join(_DIR, '.state', 'top100_liquidity.csv')
        print(f"⚠️ [Universe] oracle_v2_universe.json 缺失，降级读取 top100_liquidity.csv 重建候选池...")
        if not os.path.exists(_TOP100_CSV):
            print(f"❌ [Universe] top100_liquidity.csv 也不存在，无法重建候选池，退出。")
            return
        import csv as _csv
        pool = []
        with open(_TOP100_CSV, 'r', encoding='utf-8-sig') as _f:
            for row in _csv.DictReader(_f):
                code = row.get('代码', '').strip()
                if code:
                    pool.append({'code': code, 'avg_amount_5d': float(row.get('5日均成交额（亿）', 0) or 0)})
        # 同步写回 oracle_v2_universe.json 防止下次再降级
        _rebuilt = {'meta': {'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
                             'note': 'Auto-rebuilt from top100_liquidity.csv'},
                    'universe': pool}
        os.makedirs(os.path.dirname(UNIVERSE_FILE), exist_ok=True)
        with open(UNIVERSE_FILE, 'w', encoding='utf-8') as _wf:
            json.dump(_rebuilt, _wf, indent=4, ensure_ascii=False)
        print(f"✅ [Universe] 降级重建完成，共 {len(pool)} 只候选，已写回 oracle_v2_universe.json")

        
    candidates = [item['code'] for item in pool]

    # 挂载 T0 绝对真理库
    t0_set = _load_t0_set()

    # ── 从本地 QMT 数据目录读取（无网络拉取）──────────────────────────────
    # qmt_daily_sync.py 已在 15:30 将全宇宙日线落盘至本地。
    # 直接读本地缓存：无网络延迟，无并发抢占，结果完全一致。
    start_time = (pd.Timestamp.now() - pd.Timedelta(days=LOCAL_LOOKBACK_DAYS)).strftime("%Y%m%d")
    end_time   = _last_trading_day_str()    # 不包含未来日期，跳过非交易日

    print(f"[本地读取] 数据区间：{start_time} ~ {end_time}，标的数：{len(candidates)}")
    market_data = xtdata.get_market_data_ex(
        field_list=['close', 'open', 'high', 'low', 'volume'],
        stock_list=candidates,
        period='1d',
        start_time=start_time,
        end_time=end_time,
    )

    # ✅ 时间戳新鲜度核对（警告但不阻断）
    _validate_local_data(market_data, candidates)
    
    qualified_assets = []
    
    for item in pool:
        code = item['code']
        if code not in market_data or market_data[code].empty: continue
        
        df = market_data[code]
        if len(df) < 60: continue

        # get_instrument_detail 逐只调用（无 is_dict 参数，CRITICAL-5 修复）
        try:
            detail = xtdata.get_instrument_detail(code)
            name   = (detail or {}).get('InstrumentName', '') if isinstance(detail, dict) else ''
        except Exception:
            name = item.get('name', '')   # 降级：从 universe.json 里读名称

        # 🧬 绝对基因判定：在池子里就是 T0，不在就是 T1，没有任何废话
        is_t0      = code in t0_set
        trade_rule = "T+0" if is_t0 else "T+1"
        
        df['ma20'] = df['close'].rolling(20).mean()
        current_ma20 = df['ma20'].iloc[-1]
        spread = (df['close'] / df['ma20'] - 1).dropna()
        
        hurst = calculate_hurst_variance_scaling(spread)
        if hurst > MAX_HURST: continue
            
        cir_params = calibrate_rough_cir(spread, hurst=hurst)
        if cir_params['rough_halflife_days'] > MAX_ROUGH_HALFLIFE: continue
            
        base_efficiency = cir_params['sigma'] / cir_params['rough_halflife_days']
        
        # T0 流动性溢价
        final_efficiency = base_efficiency * 1.2 if is_t0 else base_efficiency
        
        qualified_assets.append({
            "code":         code,
            "name":         name,
            "trade_rule":   trade_rule,
            "efficiency":   round(final_efficiency, 4),
            "dynamic_step": round(cir_params['sigma'] / 2.5, 4),
            "ma20_baseline": round(current_ma20, 4),
            # ⏳ 时间止损基准：Executor 用 halflife_days × TIME_STOP_MULTIPLIER 判定过期仓位
            "halflife_days": round(cir_params['rough_halflife_days'], 2),
        })


    qualified_assets.sort(key=lambda x: x['efficiency'], reverse=True)

    # ── 🛡️ 同质化互斥甄别发牌机制（Homogeneity Mutex Lock）────────────────────
    # 防止纳指1/2/3、标普油气A/B 等高相关标的占满所有席位，确保持仓多样性
    def _extract_core_class(name: str) -> str:
        """提取资产核心基因关键词，用于同质化判定。
        匹配不到则返回全名（保证两只名字完全不同的标的不互斥）。
        """
        mutex_keywords = [
            '油气', '纳指', '标普', '恒生', '红利', '黄金',
            '医疗', '医药', '创新药', '半导体', '芯片',
            '日经', '德国', '法国', '国债', '科技', '消费',
            '能源', '新能源', '地产', '银行', '军工',
        ]
        for kw in mutex_keywords:
            if kw in name:
                return kw
        return name  # 无命中则以全名为唯一键，互不影响

    top_slots    = []
    seen_classes = set()

    print("\n🛡️  同质化互斥甄别启动...")
    for asset in qualified_assets:
        if len(top_slots) >= SLOT_COUNT:
            break  # 席位已满

        core_class = _extract_core_class(asset['name'])

        if core_class in seen_classes:
            print(f"  🚫 [拦截] {asset['code']} {asset['name']} "
                  f"(效率:{asset['efficiency']}) → 同类【{core_class}】已有代表，踢出！")
            continue

        seen_classes.add(core_class)
        top_slots.append(asset)
        print(f"  ✅ [放行] {asset['code']} {asset['name']} "
              f"(效率:{asset['efficiency']}) → 基因:【{core_class}】")
    # ── 互斥甄别结束 ──────────────────────────────────────────────────────────

    
    os.makedirs(os.path.dirname(OUTPUT_SLOTS), exist_ok=True)
    with open(OUTPUT_SLOTS, 'w', encoding='utf-8') as f:
        json.dump({"slots": top_slots}, f, indent=4, ensure_ascii=False)

    print(f"\u2705 解算完毕！获取 {len(top_slots)} 个席位：")
    lines = []
    for s in top_slots:
        line = f"  [{s['trade_rule']}] {s['code']} {s['name']} | 效率: {s['efficiency']} | 步长: {s['dynamic_step']*100:.2f}%"
        print(line)
        lines.append(line)

    # ── N8N 实时推送：席位解算结果 ───────────────────────────────
    slot_summary = "\n".join(lines) if lines else "❗ 无席位入选，请检查宇宙筛选参数！"
    webhook_msg = (
        f"当日席位数：{len(top_slots)}/{SLOT_COUNT}\n"
        f"候选库大小：{len(top_slots)} 小（经同质化甄别 +T+1筛选）\n"
        f"---\n{slot_summary}"
    )
    if not top_slots:
        send_webhook("🚨 ETF_OU_Grid 席位解算失败", webhook_msg)
    else:
        send_webhook("🛸 ETF_OU_Grid 席位已解算", webhook_msg)

if __name__ == "__main__":
    run_global_grid_master()