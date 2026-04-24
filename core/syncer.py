#!/usr/bin/env -S uv run
# -*- coding: utf-8 -*-
"""
数据同步与备份模块
负责下载历史数据并将热数据备份到冷存储
"""

import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .logger import get_logger

# xtquant 库导入（仅在 Windows 环境可用）
try:
    import xtquant.xtdata as xtdata
    XTDATA_AVAILABLE = True
except ImportError:
    XTDATA_AVAILABLE = False
    print("警告：xtdata 库未安装，历史数据下载功能将不可用")


class DataSyncer:
    """数据同步与备份管理器"""
    
    def __init__(self, config: dict):
        """
        初始化数据同步管理器
        
        Args:
            config: 配置字典（来自 config.yaml）
        """
        self.config = config
        self.logger = get_logger("DataSyncer", config)
        
        # 从配置读取参数
        paths = config.get("paths", {})
        self.realtime_data_path = Path(paths.get("realtime_data", "data/realtime"))
        self.history_data_path = Path(paths.get("history_data", "data/history"))
        
        stock_pool = config.get("stock_pool", {})
        self.stock_codes = stock_pool.get("codes", [])
        
        download_config = config.get("data_download", {})
        self.download_period = download_config.get("period", "1d")
        self.retry_times = download_config.get("retry_times", 3)
        self.retry_interval = download_config.get("retry_interval", 10)
        
        schedule = config.get("schedule", {}).get("data_sync", {})
        self.cleanup_days = schedule.get("cleanup_days", 7)
        
        # 确保目录存在
        self.history_data_path.mkdir(parents=True, exist_ok=True)
        
        self.logger.info("数据同步管理器初始化完成")
        self.logger.info(f"实时数据路径: {self.realtime_data_path}")
        self.logger.info(f"历史数据路径: {self.history_data_path}")
        self.logger.info(f"下载周期: {self.download_period}")
        self.logger.info(f"股票池: {self.stock_codes}")
        
        # 默认全量下载起始日期
        self.full_history_start = config.get("data_download", {}).get("full_history_start", "20120101")
    
    def download_history_data(self, start_time: Optional[str] = None, end_time: Optional[str] = None) -> None:
        """
        下载历史数据
        
        Args:
            start_time: 起始时间 (YYYYMMDD)，如果为 None 则使用今天
            end_time: 结束时间 (YYYYMMDD)，如果为 None 则使用今天
        """
        if not XTDATA_AVAILABLE:
            self.logger.error("xtdata 库不可用，无法下载历史数据")
            return
        
        try:
            self.logger.info("========== 开始下载历史数据 ==========")
            
            if not self.stock_codes:
                self.logger.warning("股票池为空，跳过下载")
                return
            
            # 获取日期范围
            target_start = start_time if start_time else datetime.now().strftime("%Y%m%d")
            target_end = end_time if end_time else datetime.now().strftime("%Y%m%d")
            
            self.logger.info(f"下载范围: {target_start} -> {target_end}")
            
            # 下载每只股票的历史数据
            for code in self.stock_codes:
                success = False
                
                for attempt in range(1, self.retry_times + 1):
                    try:
                        self.logger.info(f"下载 {code} 的 {self.download_period} 数据（尝试 {attempt}/{self.retry_times}）")
                        
                        # 调用 xtdata 下载当日数据
                        # 参数：股票代码列表、周期、起始日期
                        # 调用 xtdata 下载指定范围数据
                        xtdata.download_history_data(
                            stock_code=code,
                            period=self.download_period,
                            start_time=target_start,
                            end_time=target_end,
                        )
                        
                        self.logger.info(f"{code} 下载成功")
                        success = True
                        break
                        
                    except Exception as e:
                        self.logger.warning(f"{code} 下载失败（尝试 {attempt}/{self.retry_times}）: {e}")
                        if attempt < self.retry_times:
                            self.logger.info(f"等待 {self.retry_interval} 秒后重试...")
                            time.sleep(self.retry_interval)
                
                if not success:
                    self.logger.error(f"{code} 下载失败，已达到最大重试次数")
            
            self.logger.info("历史数据下载任务完成")
            
        except Exception as e:
            self.logger.error(f"下载历史数据失败: {e}")

    def download_all_history(self) -> None:
        """
        下载从 2012 年开始的所有历史数据
        """
        self.logger.info(f"========== 开始下载全量历史数据 (从 {self.full_history_start} 开始) ==========")
        self.download_history_data(start_time=self.full_history_start)
    
    def backup_realtime_data(self) -> None:
        """
        备份实时热数据到冷存储
        由 schedule 在每天 16:30 调用
        """
        try:
            self.logger.info("========== 开始备份实时数据 ==========")
            
            if not self.realtime_data_path.exists():
                self.logger.warning(f"实时数据路径不存在: {self.realtime_data_path}")
                return
            
            # 创建今天的备份目录
            today = datetime.now().strftime("%Y%m%d")
            backup_dir = self.history_data_path / today
            backup_dir.mkdir(parents=True, exist_ok=True)
            
            # 遍历实时数据目录中的所有文件
            file_count = 0
            for file_path in self.realtime_data_path.glob(f"tick_{today}_*.parquet"):
                try:
                    dest_path = backup_dir / file_path.name
                    
                    # 复制文件（保留元数据）
                    shutil.copy2(file_path, dest_path)
                    
                    self.logger.info(f"备份文件: {file_path.name}")
                    file_count += 1
                    
                except Exception as e:
                    self.logger.error(f"备份文件 {file_path.name} 失败: {e}")
            
            if file_count > 0:
                self.logger.info(f"备份完成，共 {file_count} 个文件")
            else:
                self.logger.warning("没有找到需要备份的文件")
            
        except Exception as e:
            self.logger.error(f"备份实时数据失败: {e}")
    
    def cleanup_old_data(self) -> None:
        """
        清理热数据中超过指定天数的旧数据
        可选功能，避免 NVMe 盘空间不足
        """
        try:
            self.logger.info(f"========== 清理超过 {self.cleanup_days} 天的旧数据 ==========")
            
            if not self.realtime_data_path.exists():
                return
            
            # 计算截止日期
            from datetime import timedelta
            cutoff_date = datetime.now() - timedelta(days=self.cleanup_days)
            cutoff_str = cutoff_date.strftime("%Y%m%d")
            
            # 遍历文件并删除旧数据
            deleted_count = 0
            for file_path in self.realtime_data_path.glob("tick_*.parquet"):
                try:
                    # 从文件名提取日期: tick_20260208_510300_SH.parquet
                    parts = file_path.stem.split("_")
                    if len(parts) >= 2:
                        file_date = parts[1]
                        
                        if file_date < cutoff_str:
                            file_path.unlink()
                            self.logger.info(f"删除旧文件: {file_path.name}")
                            deleted_count += 1
                            
                except Exception as e:
                    self.logger.warning(f"删除文件 {file_path.name} 失败: {e}")
            
            if deleted_count > 0:
                self.logger.info(f"清理完成，删除 {deleted_count} 个旧文件")
            else:
                self.logger.info("没有需要清理的旧文件")
                
        except Exception as e:
            self.logger.error(f"清理旧数据失败: {e}")
    
    def sync_data(self) -> None:
        """
        执行完整的数据同步流程
        包括：下载历史数据 -> 备份实时数据 -> 清理旧数据
        由 schedule 在每天 16:00 调用
        """
        self.logger.info("========================================")
        self.logger.info("开始执行数据同步流程")
        self.logger.info("========================================")
        
        # 1. 下载历史数据
        self.download_history_data()
        
        # 2. 备份实时数据
        self.backup_realtime_data()
        
        # 3. 清理旧数据（可选）
        if self.cleanup_days > 0:
            self.cleanup_old_data()
        
        self.logger.info("========================================")
        self.logger.info("数据同步流程完成")
        self.logger.info("========================================")


def create_syncer(config: dict) -> DataSyncer:
    """
    工厂函数：创建数据同步管理器实例
    
    Args:
        config: 配置字典
    
    Returns:
        DataSyncer 实例
    """
    return DataSyncer(config)


if __name__ == "__main__":
    # 测试数据同步管理器
    print("数据同步管理器测试模式")
    print("注意：此模块需要 xtdata 库（仅在 Windows 量化服务器上可用）")
    
    # 尝试加载配置文件
    import yaml
    config_path = Path("config.yaml")
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        print(f"已加载配置文件: {config_path.absolute()}")
    else:
        print("未找到配置文件，使用默认测试配置")
        config = {
            "paths": {
                "realtime_data": "data/realtime",
                "history_data": "data/history",
            },
            "stock_pool": {
                "codes": ["510300.SH", "510500.SH"],
            },
            "data_download": {
                "period": "1d",
                "retry_times": 3,
                "retry_interval": 10,
            },
            "schedule": {
                "data_sync": {"cleanup_days": 7}
            },
            "logging": {"level": "INFO"},
        }
    
    syncer = DataSyncer(config)
    print(f"实时数据路径: {syncer.realtime_data_path}")
    print(f"历史数据路径: {syncer.history_data_path}")
    
    # 命令行参数支持
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--full":
        print("检测到 --full 参数，将下载全量历史数据...")
        try:
            syncer.download_all_history()
        except Exception as e:
            print(f"任务执行失败: {e}")
    else:
        try:
            syncer.sync_data()
        except KeyboardInterrupt:
            print("用户中断")
        except Exception as e:
            print(f"执行失败: {e}")
