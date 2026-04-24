#!/usr/bin/env -S uv run
# -*- coding: utf-8 -*-
"""
数据订阅与落盘模块
负责订阅实时 Tick 数据，并高效落盘到 Parquet 文件
"""

import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from .logger import get_logger

# 注意：xtquant 库为私有库，需在 Windows 环境安装
# 在非 Windows 环境下运行时，导入会失败（仅用于开发测试）
try:
    import xtquant.xtdata as xtdata
    XTDATA_AVAILABLE = True
except ImportError:
    XTDATA_AVAILABLE = False
    print("警告：xtdata 库未安装，数据订阅功能将不可用（仅在 Windows 量化服务器上可用）")


class DataFeed:
    """数据订阅与落盘管理器"""
    
    def __init__(self, config: dict):
        """
        初始化数据订阅管理器
        
        Args:
            config: 配置字典（来自 config.yaml）
        """
        self.config = config
        self.logger = get_logger("DataFeed", config)
        
        # 从配置读取参数
        paths = config.get("paths", {})
        self.realtime_data_path = Path(paths.get("realtime_data", "data/realtime"))
        
        stock_pool = config.get("stock_pool", {})
        self.subscription_mode = stock_pool.get("mode", "custom")
        self.stock_codes = stock_pool.get("codes", [])
        
        schedule = config.get("schedule", {}).get("data_feed", {})
        self.snapshot_interval = schedule.get("snapshot_interval", 3)
        self.flush_interval = schedule.get("flush_interval", 60)
        
        parquet_config = config.get("data_subscription", {}).get("parquet", {})
        self.compression = parquet_config.get("compression", "snappy")
        
        # 内存缓存：{股票代码: [tick数据列表]}
        self.data_buffer: Dict[str, List[dict]] = defaultdict(list)
        
        # 订阅状态标志
        self.sub_handles: Dict[str, int] = {}  # {股票代码: 订阅句柄}
        self.is_running = False
        
        # 确保数据目录存在
        self.realtime_data_path.mkdir(parents=True, exist_ok=True)
        
        self.logger.info("数据订阅管理器初始化完成")
        self.logger.info(f"实时数据路径: {self.realtime_data_path}")
        self.logger.info(f"订阅模式: {self.subscription_mode}")
        self.logger.info(f"股票池: {self.stock_codes if self.subscription_mode == 'custom' else '全市场'}")
        self.logger.info(f"快照间隔: {self.snapshot_interval} 秒")
        self.logger.info(f"落盘间隔: {self.flush_interval} 秒")
    
    def start_subscription(self) -> None:
        """
        开始订阅行情数据
        由 schedule 在每天 09:25 调用
        """
        if not XTDATA_AVAILABLE:
            self.logger.error("xtdata 库不可用，无法订阅数据")
            return
        
        try:
            self.logger.info("========== 开始订阅行情数据 ==========")
            
            # 根据订阅模式选择股票池
            if self.subscription_mode == "all_market":
                # 全市场订阅（可以通过 xtdata 获取所有股票代码）
                self.logger.info("订阅全市场行情")
                # codes = xtdata.get_stock_list_in_sector("沪深A股")  # 示例
                codes = self.stock_codes  # 暂时使用配置中的代码
            else:
                codes = self.stock_codes
            
            if not codes:
                self.logger.warning("股票池为空，跳过订阅")
                return
            
            # 订阅 Tick 数据
            self.logger.info(f"订阅 {len(codes)} 只股票的 Tick 数据")
            
            for code in codes:
                try:
                    # 获取订阅句柄，用于后期取消注册
                    handle = xtdata.subscribe_quote(
                        stock_code=code,
                        period="tick",
                        count=-1,
                    )
                    self.sub_handles[code] = handle
                    self.logger.info(f"订阅成功: {code} (句柄: {handle})")
                except Exception as e:
                    self.logger.error(f"订阅失败 {code}: {e}")
            
            self.logger.info(f"行情订阅完成，共成功订阅 {len(self.sub_handles)} 只")
            
        except Exception as e:
            self.logger.error(f"订阅行情失败: {e}")
    
    def stop_subscription(self) -> None:
        """
        停止订阅行情数据
        由 schedule 在每天 15:00 调用
        """
        if not XTDATA_AVAILABLE:
            return
        
        try:
            self.logger.info("========== 停止订阅行情数据 ==========")
            
            # 取消订阅
            if self.sub_handles:
                self.logger.info(f"正在取消 {len(self.sub_handles)} 个订阅会话...")
                for code, handle in self.sub_handles.items():
                    try:
                        xtdata.unsubscribe(handle)
                    except Exception:
                        pass
                self.sub_handles.clear()
            
            # 最后一次落盘
            self.flush_to_disk()
            
            # 清空缓存
            self.data_buffer.clear()
            
            self.logger.info("行情订阅已完全停止并注销")
            
        except Exception as e:
            self.logger.error(f"停止订阅行情失败: {e}")
    
    def collect_snapshot(self) -> None:
        """
        采集一次快照数据并缓存到内存
        由 schedule 每隔 snapshot_interval 秒调用
        """
        if not XTDATA_AVAILABLE or not self.sub_handles:
            return
        
        try:
            # 获取全部 Tick 数据
            codes = self.stock_codes if self.subscription_mode == "custom" else []
            
            if not codes:
                return
            
            # 逐个股票获取快照
            for code in codes:
                tick_data = xtdata.get_full_tick([code])
                
                if tick_data and code in tick_data:
                    # 将数据加入缓存
                    self.data_buffer[code].append({
                        "timestamp": datetime.now(),
                        "data": tick_data[code],
                    })
            
            self.logger.debug(f"快照采集完成，当前缓存大小: {sum(len(v) for v in self.data_buffer.values())} 条")
            
        except Exception as e:
            self.logger.error(f"采集快照失败: {e}")
    
    def flush_to_disk(self) -> None:
        """
        将内存中的数据落盘到 Parquet 文件
        由 schedule 每隔 flush_interval 秒调用
        """
        if not self.data_buffer:
            self.logger.debug("内存缓存为空，跳过落盘")
            return
        
        try:
            self.logger.info(f"开始落盘数据到 {self.realtime_data_path}")
            date_str = datetime.now().strftime("%Y%m%d")
            
            for code, tick_list in self.data_buffer.items():
                if not tick_list:
                    continue
                
                # 构造文件名: tick_20260208_510300.SH.parquet
                safe_code = code.replace(".", "_")  # 避免文件名中的点号
                file_name = f"tick_{date_str}_{safe_code}.parquet"
                file_path = self.realtime_data_path / file_name
                
                # 转换为 DataFrame
                df = pd.DataFrame([item["data"] for item in tick_list])
                df.insert(0, "collect_time", [item["timestamp"] for item in tick_list])
                
                # Append 模式写入
                if file_path.exists():
                    existing_df = pd.read_parquet(file_path)
                    df = pd.concat([existing_df, df], ignore_index=True)
                
                # 写入 Parquet
                df.to_parquet(
                    file_path,
                    engine="pyarrow",
                    compression=self.compression,
                    index=False,
                )
                
                self.logger.info(f"落盘完成: {file_name} ({len(df)} 条记录)")
            
            # 清空内存缓存
            self.data_buffer.clear()
            self.logger.info("内存缓存已清空")
            
        except Exception as e:
            self.logger.error(f"落盘失败: {e}")


def create_data_feed(config: dict) -> DataFeed:
    """
    工厂函数：创建数据订阅管理器实例
    
    Args:
        config: 配置字典
    
    Returns:
        DataFeed 实例
    """
    return DataFeed(config)


if __name__ == "__main__":
    # 测试数据订阅管理器
    print("数据订阅管理器测试模式")
    print("注意：此模块需要 xtdata 库（仅在 Windows 量化服务器上可用）")
    
    test_config = {
        "paths": {"realtime_data": "data/realtime"},
        "stock_pool": {
            "mode": "custom",
            "codes": ["510300.SH", "510500.SH"],
        },
        "schedule": {
            "data_feed": {
                "snapshot_interval": 3,
                "flush_interval": 60,
            }
        },
        "data_subscription": {
            "parquet": {"compression": "snappy"}
        },
        "logging": {"level": "INFO"},
    }
    
    feed = DataFeed(test_config)
    print(f"数据路径: {feed.realtime_data_path}")
    print(f"股票池: {feed.stock_codes}")
