# -*- coding: utf-8 -*-
import os
import yaml
import pandas as pd
import numpy as np
from datetime import datetime

# 配置路径
DATA_DIR = r'Z:\QuantpC_Workspace\Data'
MARKET_DAILY_DIR = os.path.join(DATA_DIR, 'Market_Daily')
INPUT_YAML = os.path.join(DATA_DIR, 'ETF_list.yaml')
OUTPUT_YAML = os.path.join(DATA_DIR, 'refined_etf_list.yaml')

# 关键词定义 (分优先级和分类)
TAG_KEYWORDS = {
    '指数_宽基': ['沪深300', '中证500', '中证1000', '上证50', '创业板', '科创50', '科创100', '中证A50', '中证A500'],
    '指数_海外': ['纳指', '标普500', '恒生', '德国DAX', '日经225', '纳斯达克'],
    '行业_科技': ['芯片', '半导体', '人工智能', 'AI', '互联网', '云计算', '通信', '大数据', '游戏', '软件'],
    '行業_金融': ['银行', '证券', '保险', '金融', '非银'],
    '行业_消费': ['消费', '白酒', '食品', '家电', '医药', '医疗', '生物'],
    '行业_制造': ['新能源', '光伏', '电池', '汽车', '军工', '高端装备', '机械'],
    '行业_周期': ['煤炭', '有色', '钢铁', '化工', '石油', '基建', '房地产', '地产', '资源'],
    '策略_主题': ['红利', '价值', '成长', '质量', '低波', '红利低波', '黄金', '豆粕', '原油'],
}

# 货币基金排除词
MONEY_FUND_KEYWORDS = ['货币', '保证金', '添利', '快钱', '金元宝', '即时化', '日日', '月月', '理财', '理财金']

def load_etf_list():
    if not os.path.exists(INPUT_YAML):
        print(f"❌ 找不到输入文件: {INPUT_YAML}")
        return []
    with open(INPUT_YAML, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def is_money_fund(name):
    for kw in MONEY_FUND_KEYWORDS:
        if kw in name:
            return True
    return False

def get_parquet_path(code):
    # 处理代码格式，假设文件名类似 510050_SH.parquet
    filename = code.replace('.', '_') + '.parquet'
    path = os.path.join(MARKET_DAILY_DIR, filename)
    return path if os.path.exists(path) else None

def analyze_correlation_and_volume(etf_items):
    """
    对一组 ETF 进行相关性分析，并保留成交量最大的一只。
    如果相关性极高 (>99%)，则视为重复标的，仅保留流动性最好的一只。
    """
    if not etf_items:
        return None
    
    # 预加载数据并计算成交量
    valid_data = []
    for item in etf_items:
        path = get_parquet_path(item['code'])
        if not path: continue
        
        try:
            df = pd.read_parquet(path)
            if len(df) < 20: continue # 样本太少不具统计意义
            
            # 计算最近 60 交易日的平均成交额（amount 比 volume 更能反映流动性质量）
            df_recent = df.sort_index().tail(60)
            avg_amount = df_recent['amount'].mean()
            returns = df_recent['close'].pct_change().dropna()
            
            valid_data.append({
                'code': item['code'],
                'name': item['name'],
                'amount': avg_amount,
                'returns': returns
            })
        except Exception as e:
            print(f"⚠️ 处理 {item['code']} 时出错: {e}")

    if not valid_data:
        # 如果都没有历史数据，保底按代码排序取第一个
        return sorted(etf_items, key=lambda x: x['code'])[0]

    if len(valid_data) == 1:
        return valid_data[0]

    # 构建相关性矩阵
    # 找出共同的时间索引
    common_idx = None
    for d in valid_data:
        if common_idx is None:
            common_idx = d['returns'].index
        else:
            common_idx = common_idx.intersection(d['returns'].index)
    
    if len(common_idx) < 10: # 共同交易日太少，无法判断
        return max(valid_data, key=lambda x: x['amount'])

    returns_df = pd.DataFrame({d['code']: d['returns'].loc[common_idx] for d in valid_data})
    corr_matrix = returns_df.corr()
    
    # 实施 99% 走势去重逻辑
    to_exclude = set()
    codes = [d['code'] for d in valid_data]
    
    for i in range(len(codes)):
        if codes[i] in to_exclude: continue
        for j in range(i + 1, len(codes)):
            if codes[j] in to_exclude: continue
            
            corr = corr_matrix.loc[codes[i], codes[j]]
            if corr > 0.99:
                # 发现极其相似的标的，干掉成交额小的那一个
                amount_i = next(d['amount'] for d in valid_data if d['code'] == codes[i])
                amount_j = next(d['amount'] for d in valid_data if d['code'] == codes[j])
                
                if amount_i >= amount_j:
                    to_exclude.add(codes[j])
                    print(f"✂️ 去重: {codes[j]}({next(d['name'] for d in valid_data if d['code']==codes[j])}) 与 {codes[i]} 走势 99% 相似，保留成交额大的。")
                else:
                    to_exclude.add(codes[i])
                    print(f"✂️ 去重: {codes[i]}({next(d['name'] for d in valid_data if d['code']==codes[i])}) 与 {codes[j]} 走势 99% 相似，保留成交额大的。")
                    break # i 已被踢出，跳出内层循环

    # 在幸存者中，按成交额降序排列，取最强的一个（针对该关键词）
    survivors = [d for d in valid_data if d['code'] not in to_exclude]
    survivors.sort(key=lambda x: x['amount'], reverse=True)
    
    return survivors[0] if survivors else None

def main():
    print("🚀 开始构建 ETF 标签池...")
    data = load_etf_list()
    if not data or 'etf_list' not in data:
        print("❌ YAML 格式不符合预期（缺少 etf_list 键）")
        return

    raw_list = data['etf_list']
    
    # 1. 初始过滤：去除货币基金
    filtered_list = [item for item in raw_list if not is_money_fund(item['name'])]
    print(f"📊 原始标的: {len(raw_list)}, 排除货币后剩余: {len(filtered_list)}")

    # 2. 分类归纳
    categorized = {} # tag -> {kw -> [items]}
    
    for item in filtered_list:
        name = item['name']
        for cat_name, kws in TAG_KEYWORDS.items():
            for kw in kws:
                if kw in name:
                    if cat_name not in categorized:
                        categorized[cat_name] = {}
                    if kw not in categorized[cat_name]:
                        categorized[cat_name][kw] = []
                    categorized[cat_name][kw].append(item)
                    # 注意：一只标的可以属于多个大类或关键词，但这里我们取第一个匹配的关键词以简化分类
                    break 
            else: continue
            break

    print(f"🔍 已分类大类: {len(categorized)}")
    
    # 3. 对每个具体关键词（子类）进行筛选
    categories = {}
    total_count = 0
    
    for cat, sub_cats in categorized.items():
        print(f"处理分类: {cat}...")
        cat_results = []
        for kw, items in sub_cats.items():
            # 对同一个关键词（如 "红利"）下的多个 ETF 进行筛选
            best_pick = analyze_correlation_and_volume(items)
            if best_pick:
                cat_results.append({
                    'code': best_pick['code'],
                    'name': best_pick['name']
                })
                total_count += 1
        
        if cat_results:
            categories[cat] = cat_results
    
    # 4. 输出结果
    output_data = {
        'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'count': total_count,
        'categories': categories
    }
    
    with open(OUTPUT_YAML, 'w', encoding='utf-8') as f:
        yaml.dump(output_data, f, allow_unicode=True, sort_keys=False)
    
    print(f"✅ 精选聚类列表已生成: {OUTPUT_YAML}")
    print(f"✨ 最终入选标的数量: {total_count}, 分类总数: {len(categories)}")

if __name__ == '__main__':
    main()
