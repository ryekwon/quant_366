#!/usr/bin/env -S uv run
# -*- coding: utf-8 -*-
"""
进程守护模块
负责监控 MiniQMT (xtquant.exe) 进程状态，如果进程挂掉则自动重启
"""

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

import psutil

from .logger import get_logger


class ProcessGuardian:
    """进程守护器类"""
    
    def __init__(self, config: dict):
        """
        初始化进程守护器
        
        Args:
            config: 配置字典（来自 config.yaml）
        """
        self.config = config
        self.logger = get_logger("Guardian", config)
        
        # 从配置读取参数
        paths = config.get("paths", {})
        self.executable_path = paths.get("miniQMT_executable", "")
        self.process_name = "xtquant.exe"
        
        # 检查间隔（分钟）
        schedule_config = config.get("schedule", {})
        self.check_interval = schedule_config.get("guardian_check_interval", 5)
        
        self.logger.info(f"进程守护器初始化完成，监控进程：{self.process_name}")
        self.logger.info(f"可执行文件路径：{self.executable_path}")
        self.logger.info(f"检查间隔：{self.check_interval} 分钟")
    
    def is_process_running(self) -> bool:
        """
        检查目标进程是否正在运行
        
        Returns:
            True 表示进程存在，False 表示进程不存在
        """
        try:
            for proc in psutil.process_iter(["name"]):
                if proc.info["name"] == self.process_name:
                    self.logger.debug(f"进程 {self.process_name} 正在运行，PID: {proc.pid}")
                    return True
            return False
        except Exception as e:
            self.logger.error(f"检查进程状态时出错: {e}")
            return False
    
    def start_process(self) -> bool:
        """
        启动 MiniQMT 进程
        
        Returns:
            True 表示启动成功，False 表示启动失败
        """
        try:
            if not Path(self.executable_path).exists():
                self.logger.error(f"可执行文件不存在: {self.executable_path}")
                return False
            
            self.logger.info(f"正在启动进程: {self.executable_path}")
            
            # 使用 subprocess.Popen 启动进程（后台运行）
            # Windows 系统使用 CREATE_NEW_CONSOLE 标志独立启动
            if os.name == "nt":  # Windows 系统
                subprocess.Popen(
                    [self.executable_path],
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                    close_fds=True,
                )
            else:  # Linux/Mac 系统（虽然本项目主要针对 Windows）
                subprocess.Popen(
                    [self.executable_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
            
            # 等待 5 秒确认进程启动
            time.sleep(5)
            
            if self.is_process_running():
                self.logger.info(f"进程 {self.process_name} 启动成功")
                return True
            else:
                self.logger.warning(f"进程 {self.process_name} 启动后未检测到运行状态")
                return False
                
        except Exception as e:
            self.logger.error(f"启动进程时出错: {e}")
            return False
    
    def check_and_restart(self) -> None:
        """
        检查进程状态，如果挂掉则重启
        这是供 schedule 调用的主函数
        """
        self.logger.info(f"执行守护检查：{self.process_name}")
        
        if self.is_process_running():
            self.logger.info(f"进程 {self.process_name} 运行正常")
        else:
            self.logger.warning(f"进程 {self.process_name} 未运行，尝试重启...")
            if self.start_process():
                self.logger.info(f"进程 {self.process_name} 重启成功")
            else:
                self.logger.error(f"进程 {self.process_name} 重启失败，将在下次检查时重试")


def create_guardian(config: dict) -> ProcessGuardian:
    """
    工厂函数：创建进程守护器实例
    
    Args:
        config: 配置字典
    
    Returns:
        ProcessGuardian 实例
    """
    return ProcessGuardian(config)


if __name__ == "__main__":
    # 测试进程守护器
    print("进程守护器测试模式")
    print("注意：此模块主要在 Windows 系统上运行")
    
    # 模拟配置
    test_config = {
        "paths": {
            "miniQMT_executable": "C:\\国金证券QMT交易端\\userdata_mini\\bin\\xtquant.exe"
        },
        "schedule": {"guardian_check_interval": 5},
        "logging": {"level": "INFO"},
    }
    
    guardian = ProcessGuardian(test_config)
    guardian.check_and_restart()
