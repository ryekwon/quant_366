# -*- coding: utf-8 -*-
"""
T0 猎犬 V6.1 (人工验证CSV + 纯走势去重版)
架构:
  1. 读取 T0型ETF.csv（人工肉身验证的权威 T0 标的库，无需再做关键词筛选）
  2. 黑名单物理过滤（剔除已确认 T+1 标的）
  3. 同类走势去重：按族群 Tag 分组，拉取近 20 日涨幅，每类只保留最优一只
  4. 写入 t0_list.yaml 候选宇宙，由 t0_master.py 按 ATR 波动率再次选拔最终 4 只

维护说明：
  - T0型ETF.csv：每月人工更新一次即可。新上市ETF缺乏历史数据（无1m parquet），
    t0_master.py 的 MIN_BARS=12000 护城河会自动过滤，约50个交易日后才能参与竞争。
  - MANUAL_BLACKLIST：发现确认 T+1 的标的立即加入，永久封杀。
  - 货币/债券ETF无需手动过滤，ATR≈0 自然淘汰。
"""
import pandas as pd
import yaml
import sys
import os
import re
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# ================= 路径配置 =================
T0_CSV   = r"Z:\QuantpC_Workspace\Quant_Pilot\.state\T0型ETF.csv"
OUT_YAML = os.getenv("T0_LIST_FILE", r"Z:\QuantpC_Workspace\Quant_Pilot\.state\t0_list.yaml")
TEST_YAML = r"Z:\QuantpC_Workspace\Quant_Pilot\.state\t0_list_test.yaml"
# ===========================================

# ─── 人工黑名单：肉身验证为 T+1，永久封杀 ─────────────────────────────
MANUAL_BLACKLIST = {
    "159309.SZ": "油气ETF汇添富 (确认T+1, 人工封杀)",
    # ↑ 继续添加确认为 T+1 的标的
}

# ─── 族群 Tag 分类器（细化，每个地理/资产类独立保留最优一只） ─────────────
TAG_RULES = [
    # 商品
    ('黄金',         r'黄金|金etf|上海金|贵金属'),
    ('原油油气',      r'原油|油气|石油'),
    ('豆粕农产品',    r'豆粕|大豆|农产品'),
    ('有色商品',      r'有色|铜|铝|锌|能化'),
    # 美国
    ('纳斯达克',      r'纳指|纳斯达克'),
    ('标普500',       r'标普500|msci美国|美国50'),
    ('道琼斯',        r'道指|道琼斯'),
    ('标普生物科技',  r'标普生物|生物科技.*标普|标普.*生物'),
    ('标普消费',      r'标普消费'),
    ('标普石油',      r'标普石油|标普.*石油'),
    ('标普其他',      r'标普'),
    # 港股/中国海外
    ('恒生科技',      r'恒生科技|恒科'),
    ('恒生医药',      r'恒生医药|恒生生物|恒生.*医|港股.*医'),
    ('恒生消费',      r'恒生消费|港股.*消费'),
    ('恒生红利',      r'恒生.*红利|恒生.*低波|港股.*红利|港股.*高股息'),
    ('恒生国企央企',  r'恒生国企|恒生中国企业|恒生.*央企|港股.*央企|国新.*港股'),
    ('恒生指数',      r'恒生指数|恒生etf|恒生50|h股指数'),
    ('港股通科技',    r'港股通.*科技|港股.*科技'),
    ('港股通汽车',    r'港股.*汽车'),
    ('港股通医疗',    r'港股.*医疗|港股.*创新药'),
    ('港股通消费',    r'港股通.*消费'),
    ('港股通金融',    r'港股.*金融|港股.*非银'),
    ('港股通红利',    r'港股通.*红利|港股通.*高股息|港股通.*低波'),
    ('港股通50',      r'港股通50|港股通100'),
    ('中概互联',      r'中概|海外中国互联网|全球中国互联网'),
    ('港股其他',      r'港股|新经济'),
    # 亚洲单国
    ('日经',          r'日经|topix'),
    ('韩国',          r'韩国|韩交所|中韩'),
    ('印度',          r'印度'),
    ('沙特',          r'沙特'),
    ('越南',          r'越南'),
    ('东南亚',        r'东南亚|亚太低碳'),
    # 欧洲
    ('德国',          r'德国|dax'),
    ('法国',          r'法国|cac'),
    ('英国',          r'英国|富时'),
    ('欧洲综合',      r'欧洲|欧盟'),
    # 其他
    ('巴西',          r'巴西|ibovespa'),
    ('俄罗斯',        r'俄罗斯'),
    ('亚太综合',      r'亚太|全球|新兴亚洲'),
]

NOISE_CLEANER = re.compile(
    r'etf|华夏|易方达|博时|南方|广发|富国|汇添富|国泰|华宝|天弘|'
    r'联接|发起式|嘉实|鹏华|工银|建信|交银|银华|lof|'
    r'景顺|招商|万家|前海开源|摩根|海富通|大成|永赢|平安|国联安|'
    r'兴业|华安|中银|招商利安|野村|顶峰|融通|财通|\(qdii\)|\(qdii-etf\)|'
    r'\(qdii-lof\)|\(qdii-fof-lof\)'
)


def _get_tag(name: str) -> str:
    name_lower = name.lower()
    clean = NOISE_CLEANER.sub('', name_lower).strip()
    for tag, pattern in TAG_RULES:
        if re.search(pattern, clean) or re.search(pattern, name_lower):
            return tag
    return clean or name_lower


def _get_20d_return(codes: list, xtdata) -> dict:
    returns = {}
    end_date   = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=40)).strftime('%Y%m%d')
    try:
        data = xtdata.get_market_data(
            field_list=['close'], stock_list=codes, period='1d',
            start_time=start_date, end_time=end_date, count=-1
        )
        close_df = data.get('close')
        if close_df is not None and not close_df.empty:
            for code in codes:
                if code in close_df.columns:
                    series = close_df[code].dropna()
                    if len(series) >= 2:
                        returns[code] = round((series.iloc[-1] / series.iloc[0] - 1) * 100, 2)
    except Exception as e:
        print(f"   ⚠️ 走势数据拉取失败: {e}")
    return returns


def get_t0_universe():
    print("[SCAN] T0 猎犬 V6.1 — 人工验证CSV权威来源 + 同类走势去重")

    if not os.path.exists(T0_CSV):
        print(f"❌ 找不到权威来源: {T0_CSV}")
        return []

    try:
        df = pd.read_csv(T0_CSV, header=None, names=['code', 'name'],
                         encoding='utf-8', dtype=str)
    except UnicodeDecodeError:
        df = pd.read_csv(T0_CSV, header=None, names=['code', 'name'],
                         encoding='gbk', dtype=str)

    df = df.dropna(subset=['code', 'name'])
    df['code'] = df['code'].str.strip()
    df['name'] = df['name'].str.strip()
    print(f"   📋 CSV 加载: {len(df)} 只人工验证T+0标的")

    # 黑名单过滤
    raw = []
    for _, row in df.iterrows():
        code, name = row['code'], row['name']
        if code in MANUAL_BLACKLIST:
            print(f"   🚫 黑名单剔除: {code} {name}")
            continue
        raw.append({'code': code, 'name': name, 'tag': _get_tag(name)})

    print(f"   ✅ 黑名单过滤后: {len(raw)} 只")

    # xtdata 走势去重
    try:
        from xtquant import xtdata
        _has_xtdata = True
        print("   ✅ xtdata 可用，启用同类走势去重")
    except ImportError:
        _has_xtdata = False
        xtdata = None
        print("   ⚠️ xtdata 不可用，跳过走势去重")

    if _has_xtdata and raw:
        groups = defaultdict(list)
        for c in raw:
            groups[c['tag']].append(c)

        deduped = []
        for tag, group in groups.items():
            if len(group) == 1:
                deduped.append(group[0])
                continue
            codes = [c['code'] for c in group]
            returns = _get_20d_return(codes, xtdata)
            if returns:
                best = max(group, key=lambda c: returns.get(c['code'], -999))
                ret_val = returns.get(best['code'])
                ret_str = f"{ret_val:.2f}%" if isinstance(ret_val, float) else '?'
                print(f"   [{tag}] {len(group)} 只 → 保留: {best['code']} {best['name'][:16]} (20d: {ret_str})")
                deduped.append(best)
            else:
                deduped.append(group[0])
        raw = deduped

    print(f"\n🎯 去重后候选: {len(raw)} 只（供人工挑选最终 4 只网格标的）")
    return raw


def save_t0_list(candidates, out_path=OUT_YAML):
    if not candidates:
        print("⚠️ 候选列表为空，跳过写盘。")
        return
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    lines = [
        "# t0_list.yaml - T0 候选宇宙 (由 t0_hunter.py V6.1 生成)",
        f"# 来源: T0型ETF.csv (人工肉身验证权威库) + ATR走势去重",
        f"# 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "t0_targets:",
    ]
    for item in candidates:
        lines.append(f"  - '{item['code']}'  # {item['name']}")
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"✅ T0 候选列表已落盘 ({len(candidates)} 只) -> {out_path}")


if __name__ == "__main__":
    candidates = get_t0_universe()
    save_t0_list(candidates)   # 正式写入 t0_list.yaml，货币/债券 ATR≈0 自然被 t0_master.py 淘汰

