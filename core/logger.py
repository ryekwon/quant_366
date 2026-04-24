#!/usr/bin/env -S uv run
# -*- coding: utf-8 -*-
"""
日志管理模块
提供统一的日志接口，支持控制台和文件输出
"""

import logging
import os
import sys
import io
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# 尝试重新配置标准输出编码为 UTF-8，解决 Windows 控制台乱码问题
try:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding='utf-8')
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(encoding='utf-8')
except (AttributeError, io.UnsupportedOperation):
    pass


def setup_logger(
    name: str,
    log_file: str = "logs/pilot.log",
    level: str = "INFO",
    log_format: str = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    date_format: str = "%Y-%m-%d %H:%M:%S",
    max_bytes: int = 10485760,  # 10MB
    backup_count: int = 5,
) -> logging.Logger:
    """
    创建并配置日志记录器
    
    Args:
        name: 日志记录器名称（通常使用模块名）
        log_file: 日志文件路径
        level: 日志级别（DEBUG, INFO, WARNING, ERROR, CRITICAL）
        log_format: 日志格式字符串
        date_format: 时间格式字符串
        max_bytes: 单个日志文件最大字节数（超过后轮转）
        backup_count: 保留的备份日志文件数量
    
    Returns:
        配置好的 Logger 对象
    """
    # 确保日志目录存在
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 创建日志记录器
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper()))
    
    # 避免重复添加处理器
    if logger.handlers:
        return logger
    
    # 创建格式化器
    formatter = logging.Formatter(log_format, datefmt=date_format)
    
    # 文件处理器（带日志轮转）
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(getattr(logging, level.upper()))
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, level.upper()))
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger


def get_logger(name: str, config: Optional[dict] = None) -> logging.Logger:
    """
    获取日志记录器（简化接口）
    
    Args:
        name: 日志记录器名称
        config: 可选的配置字典（来自 config.yaml）
    
    Returns:
        Logger 对象
    """
    if config is None:
        # 使用默认配置
        return setup_logger(name)
    
    # 从配置文件读取参数
    logging_config = config.get("logging", {})
    paths_config = config.get("paths", {})
    
    return setup_logger(
        name=name,
        log_file=paths_config.get("log_file", "logs/pilot.log"),
        level=logging_config.get("level", "INFO"),
        log_format=logging_config.get(
            "format", "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
        ),
        date_format=logging_config.get("date_format", "%Y-%m-%d %H:%M:%S"),
        max_bytes=logging_config.get("max_bytes", 10485760),
        backup_count=logging_config.get("backup_count", 5),
    )


if __name__ == "__main__":
    # 测试日志系统
    test_logger = setup_logger("test_logger")
    
    test_logger.debug("这是一条 DEBUG 日志")
    test_logger.info("这是一条 INFO 日志")
    test_logger.warning("这是一条 WARNING 日志")
    test_logger.error("这是一条 ERROR 日志")
    test_logger.critical("这是一条 CRITICAL 日志")
    
    print("\n日志测试完成，请查看 logs/pilot.log 文件")
