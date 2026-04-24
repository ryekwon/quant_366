#!/usr/bin/env -S uv run
# -*- coding: utf-8 -*-
"""
T+0 标的自动筛选模块 (健壮版)
负责从全市场扫描 QDII、黄金、商品等 T+0 品种，支持板块定义缺失时的自动降级扫描
"""

import re
import time
from typing import List, Dict

try:
    from xtquant import xtdata
    XTDATA_AVAILABLE = True
except ImportError:
    XTDATA_AVAILABLE = False
    print("警告：xtdata 库未安装，自动选股功能将不可用")

import pandas as pd


def get_dynamic_t0_pool(top_n: int = 10) -> List[str]:
    """
    进行全市场 T+0 标的扫描
    逻辑：1. 强制更新板块数据；2. 优先扫描 QDII/黄金/商品板块；3. 兜底扫描全量 ETF 并筛选 T+0 特征。
    
    Args:
        top_n: 返回流动性最好的前 N 个标的
        
    Returns:
        股票代码列表
    """
    if not XTDATA_AVAILABLE:
        print("xtdata 不可用，无法执行筛选")
        return []

    print(">>> 正在初始化 T+0 标的池 (健壮扫描模式)...")

    # --- 步骤 1: 尝试下载板块数据 (防止本地数据缺失) ---
    try:
        print("正在同步板块分类数据...")
        xtdata.download_sector_data()
    except Exception as e:
        print(f"⚠️ 板块数据更新失败 (不影响后续运行): {e}")

    # --- 步骤 2: 获取基础池 (双重保险) ---
    target_pool = []
    preferred_sectors = ['QDII基金', '黄金ETF', '商品ETF']
    
    for sector in preferred_sectors:
        stocks = xtdata.get_stock_list_in_sector(sector)
        if stocks:
            print(f"   - 板块 [{sector}] 获取到 {len(stocks)} 只标的")
            target_pool.extend(stocks)
    
    # 兜底策略：如果精准板块没数据，扫描全量 ETF
    if len(target_pool) < 5:
        print("⚠️ 精准板块数据不足，启用全量 ETF 扫描模式...")
        all_etfs = xtdata.get_stock_list_in_sector('沪深ETF')
        
        if not all_etfs:
            print("❌ 致命错误：无法获取 '沪深ETF' 列表。请检查 MiniQMT 是否登录。")
            return []

        # 智能筛选 T+0 特征 (基于代码段和名称关键词)
        for code in all_etfs:
            # 1. 明确的代码段 (513/518 开头通常是跨境/黄金)
            if code.startswith('513') or code.startswith('518'):
                target_pool.append(code)
                continue
            
            # 2. 名字特征筛选 (深市 159 开头的 T+0 品种)
            if code.startswith('159'):
                detail = xtdata.get_instrument_detail(code)
                if detail and 'InstrumentName' in detail:
                    name = detail['InstrumentName']
                    # 覆盖：美股、港股、日本、德国、法国、商品、黄金
                    keywords = ['纳指', '标普', '日经', '豆粕', '黄金', '恒生', '港股', '教育', '石油', '有色', '德国', '法国']
                    if any(k in name for k in keywords):
                        target_pool.append(code)

    # 去重
    target_pool = list(set(target_pool))
    print(f"--- 最终锁定 T+0 候选标的: {len(target_pool)} 只 ---")

    if not target_pool:
        return []

    # --- 步骤 3: 下载行情并分析流动性 ---
    print("--- 正在下载今日流动性数据 (成交额)...")
    
    # 注意：此处维持循环单只下载模式，以兼容旧版 xtquant 参数限制，确保由于参数类型导致的失败最小化
    cnt = 0
    for code in target_pool:
        try:
            xtdata.download_history_data(stock_code=code, period='1d', start_time='', end_time='')
        except Exception:
            pass
        cnt += 1
        if cnt % 50 == 0: 
            print(f"   已下载 {cnt}/{len(target_pool)}")

    # 获取数据
    market_data = xtdata.get_market_data_ex(stock_list=target_pool, period='1d', count=1)
    
    data_list = []
    for code, df in market_data.items():
        if df is None or df.empty:
            continue
        
        # 过滤流动性差的 (日成交额 < 2000万，避免大幅冲击成本)
        try:
            amt = df.iloc[-1]['amount']
            if amt < 20000000: 
                continue
            
            detail = xtdata.get_instrument_detail(code)
            name = detail['InstrumentName'] if detail and 'InstrumentName' in detail else "Unknown"
            
            data_list.append({
                'code': code, 
                'name': name, 
                'amount': amt
            })
        except (IndexError, KeyError):
            continue

    # --- 步骤 4: 智能去重 (只留同类龙头) ---
    # 逻辑：去除公司名前缀和无关后缀，按核心名分组，取成交额最大的标的
    cleaned_groups: Dict[str, dict] = {}
    
    # 常见无关词过滤模式
    remove_pattern = r'ETF|LOF|QDII|联接|A|C|\(.*?\)|（.*?）|华夏|易方达|工银|博时|国泰|南方|广发|嘉实|华安|大成|景顺|天弘|招商|鹏华|汇添富|万家'
    
    for item in data_list:
        # 提取核心资产名
        core_name = re.sub(remove_pattern, '', item['name'], flags=re.IGNORECASE).strip()
        
        if not core_name:
            core_name = item['name'] # 兜底

        if core_name not in cleaned_groups:
            cleaned_groups[core_name] = item
        else:
            # 同类资产竞争，保留成交额更大的
            if item['amount'] > cleaned_groups[core_name]['amount']:
                cleaned_groups[core_name] = item

    # 排序并截取 Top N
    final_list = sorted(cleaned_groups.values(), key=lambda x: x['amount'], reverse=True)
    
    print("\n🏆 全市场 T+0 流动性龙头 (Top 10 自动筛选结果):")
    result_codes = []
    for item in final_list[:top_n]:
        amt_billion = item['amount'] / 100000000
        print(f"  - [{item['name']}] 代码:{item['code']} 成交:{amt_billion:.2f}亿")
        result_codes.append(item['code'])
        
    return result_codes


if __name__ == "__main__":
    # 执行自动化筛选
    codes = get_dynamic_t0_pool(top_n=8)
    if codes:
        print(f"\n✅ 扫描完成。建议 Config 配置:\n{codes}")
    else:
        print("\n❌ 扫描未产生结果，请检查 QMT 连接或数据权限。")
