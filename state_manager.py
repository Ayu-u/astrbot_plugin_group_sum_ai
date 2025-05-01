"""
群聊消息总结插件的状态管理模块
提供状态保存、加载和清理功能
"""

import os
import sys
import json
import time
import glob
import asyncio
import traceback
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta

# 确保当前目录在sys.path中以便正确导入本地模块
current_dir = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)
    # 避免循环导入
    from astrbot import logger
    logger.info(f"[StateManager] 已将插件目录添加到sys.path: {current_dir}")

from astrbot import logger

class StateManager:
    """状态管理类，提供状态保存、加载和清理功能"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化状态管理器
        
        Args:
            config: 配置信息
        """
        self.config = config
        
        # 状态相关的目录和文件
        self.plugin_data_dir = os.path.join("data", "group_summarizer")
        self.summary_dir = os.path.join(self.plugin_data_dir, "summaries")
        self.state_file = os.path.join(self.plugin_data_dir, "state.json")
        self.ensure_dirs()
        
        # 状态变量
        self.message_buffers = {}  # 每个群组的消息缓冲区
        self.message_counts = {}   # 每个群组的消息计数
        self.last_summary_times = {}  # 每个群组上次总结的时间
        self.summarizing_groups = set()  # 正在进行总结的群组
        
        # 定期保存和清理的任务
        self.save_task = None
        self.clean_task = None
    
    def ensure_dirs(self):
        """确保必要的目录存在"""
        try:
            os.makedirs(self.plugin_data_dir, exist_ok=True)
            os.makedirs(self.summary_dir, exist_ok=True)
            logger.info(f"已确保数据目录存在: {self.plugin_data_dir} 和 {self.summary_dir}")
        except Exception as e:
            logger.error(f"创建数据目录失败: {e}")
    
    def load_state(self) -> bool:
        """加载状态
        
        Returns:
            bool: 是否成功加载状态
        """
        try:
            if not os.path.exists(self.state_file):
                logger.info("状态文件不存在，将使用空状态")
                return False
                
            with open(self.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
                
            # 加载状态变量
            self.message_buffers = state.get("message_buffers", {})
            self.message_counts = state.get("message_counts", {})
            self.last_summary_times = state.get("last_summary_times", {})
            
            # 兼容性处理：确保last_summary_times中的时间是数字
            for group_id, time_str in list(self.last_summary_times.items()):
                if isinstance(time_str, str):
                    try:
                        self.last_summary_times[group_id] = float(time_str)
                    except (ValueError, TypeError):
                        # 如果无法转换，使用当前时间
                        self.last_summary_times[group_id] = time.time()
            
            logger.info(f"已加载插件状态: {len(self.message_buffers)} 群组的消息, {len(self.message_counts)} 群组的计数")
            return True
        except Exception as e:
            logger.error(f"加载状态失败: {e}")
            logger.error(f"错误详情: {traceback.format_exc()}")
            # 初始化为空状态
            self.message_buffers = {}
            self.message_counts = {}
            self.last_summary_times = {}
            return False
    
    def save_state(self) -> bool:
        """保存状态
        
        Returns:
            bool: 是否成功保存状态
        """
        try:
            state = {
                "message_buffers": self.message_buffers,
                "message_counts": self.message_counts,
                "last_summary_times": self.last_summary_times
            }
            
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                
            logger.info(f"已保存插件状态: {len(self.message_buffers)} 群组的消息, {len(self.message_counts)} 群组的计数")
            return True
        except Exception as e:
            logger.error(f"保存状态失败: {e}")
            logger.error(f"错误详情: {traceback.format_exc()}")
            return False
    
    async def periodic_save(self):
        """定期保存状态的任务"""
        try:
            while True:
                await asyncio.sleep(300)  # 5分钟保存一次
                self.save_state()
        except asyncio.CancelledError:
            # 任务被取消时保存一次
            self.save_state()
            logger.info("定期保存任务已取消")
            raise  # 重新抛出异常，允许正常取消
    
    async def periodic_clean(self):
        """定期清理旧总结文件的任务"""
        cleaning_config = self.config.get("data_cleaning", {})
        if not cleaning_config.get("enabled", True):
            logger.info("数据清理功能未启用")
            return
            
        check_interval = cleaning_config.get("check_interval", 86400)  # 默认1天检查一次
        
        try:
            while True:
                await asyncio.sleep(check_interval)
                self.clean_old_summaries()
        except asyncio.CancelledError:
            logger.info("定期清理任务已取消")
            raise  # 重新抛出异常，允许正常取消
    
    def clean_old_summaries(self) -> int:
        """清理旧的总结文件
        
        Returns:
            int: 清理的文件数量
        """
        try:
            cleaning_config = self.config.get("data_cleaning", {})
            if not cleaning_config.get("enabled", True):
                logger.info("数据清理功能未启用，跳过清理")
                return 0
                
            max_files = cleaning_config.get("max_files_per_group", 30)
            max_days = cleaning_config.get("max_retention_days", 30)
            
            # 获取所有总结文件
            all_files = glob.glob(os.path.join(self.summary_dir, "*.txt"))
            
            # 按群组分组
            group_files = {}
            for file_path in all_files:
                try:
                    # 从文件名提取群组ID：格式为 group_id_date_time.txt
                    filename = os.path.basename(file_path)
                    parts = filename.split("_")
                    if len(parts) >= 2:  # 至少有group_id和其他部分
                        group_id = parts[0]  # 第一部分是群组ID
                        if group_id not in group_files:
                            group_files[group_id] = []
                        group_files[group_id].append((file_path, os.path.getmtime(file_path)))
                except Exception as e:
                    logger.error(f"处理文件 {file_path} 时出错: {e}")
            
            # 清理每个群组的文件
            cleaned_count = 0
            current_time = time.time()
            max_age = max_days * 86400  # 转换为秒
            
            for group_id, files in group_files.items():
                # 按修改时间排序（从新到旧）
                files.sort(key=lambda x: x[1], reverse=True)
                
                # 删除超过最大数量的文件
                if len(files) > max_files:
                    for file_path, mtime in files[max_files:]:
                        try:
                            os.remove(file_path)
                            cleaned_count += 1
                            logger.info(f"已删除超过数量限制的文件: {file_path}")
                        except Exception as e:
                            logger.error(f"删除文件 {file_path} 失败: {e}")
                
                # 删除超过最大保留天数的文件
                for file_path, mtime in files:
                    if current_time - mtime > max_age:
                        try:
                            os.remove(file_path)
                            cleaned_count += 1
                            logger.info(f"已删除超过时间限制的文件: {file_path}")
                        except Exception as e:
                            logger.error(f"删除文件 {file_path} 失败: {e}")
            
            logger.info(f"数据清理完成，共删除 {cleaned_count} 个文件")
            return cleaned_count
            
        except Exception as e:
            logger.error(f"清理旧总结文件失败: {e}")
            logger.error(f"错误详情: {traceback.format_exc()}")
            return 0
            
    async def start_tasks(self):
        """启动定期任务"""
        # 启动定期保存任务
        self.save_task = asyncio.create_task(self.periodic_save())
        
        # 启动定期清理任务
        self.clean_task = asyncio.create_task(self.periodic_clean())
        
        logger.info("已启动状态管理定期任务")
        
    async def stop_tasks(self):
        """停止定期任务"""
        # 取消定期保存任务
        if hasattr(self, 'save_task') and self.save_task and not self.save_task.done():
            self.save_task.cancel()
            try:
                await self.save_task
            except asyncio.CancelledError:
                pass
                
        # 取消定期清理任务
        if hasattr(self, 'clean_task') and self.clean_task and not self.clean_task.done():
            self.clean_task.cancel()
            try:
                await self.clean_task
            except asyncio.CancelledError:
                pass
        
        logger.info("已停止状态管理定期任务")
        
        # 保存一次状态
        self.save_state() 
