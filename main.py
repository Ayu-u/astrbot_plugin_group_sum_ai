import os
import json
import time
import yaml
import glob
import asyncio
import traceback
import secrets
import uuid
import base64
from io import BytesIO
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from multiprocessing import Process, Queue

import aiohttp
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api.platform import MessageType
from astrbot.core.star.filter.event_message_type import EventMessageType
from astrbot.core.star.filter.permission import PermissionType
from astrbot.core.star.register import register_event_message_type
from astrbot import logger

# 导入提示词
try:
    from .prompts import SYSTEM_PROMPT, SUMMARY_PROMPT_TEMPLATE, MESSAGE_FORMAT, FEW_MESSAGES_PROMPT
    
    # 验证导入的提示词
    if not isinstance(SYSTEM_PROMPT, str) or not SYSTEM_PROMPT.strip():
        raise ValueError("SYSTEM_PROMPT 必须是非空字符串")
    if not isinstance(SUMMARY_PROMPT_TEMPLATE, str) or not all(x in SUMMARY_PROMPT_TEMPLATE for x in ["{group_name}", "{msg_count}", "{current_time}", "{messages}"]):
        raise ValueError("SUMMARY_PROMPT_TEMPLATE 格式不正确，缺少必要的占位符")
    if not isinstance(MESSAGE_FORMAT, str) or not all(x in MESSAGE_FORMAT for x in ["{time_str}", "{sender_name}", "{content}"]):
        raise ValueError("MESSAGE_FORMAT 格式不正确，缺少必要的占位符")
    
    logger.info("成功从提示词文件加载提示词")
except (ImportError, ValueError) as e:
    logger.error(f"提示词加载失败: {str(e)}，插件无法正常运行")
    raise ImportError(f"无法加载必要的提示词: {str(e)}")

@register("group-summarizer", "Ayu-u", "自动总结群聊消息", "1.0.0")
class GroupSummarizer(Star):
    """
    群消息自动总结插件
    
    自动收集群聊消息，定期进行总结，总结结果可保存在本地或发送到群聊
    """
    
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        # 使用新的配置系统
        self.config = config
        
        # 如果没有配置，使用默认配置
        if not self.config:
            logger.warning("未找到插件配置，使用默认配置")
            self.config = {
                "summary_threshold": 300,
                "max_buffer_size": 500,
                "min_interval": 3600,
                "data_cleaning": {
                    "enabled": True,
                    "check_interval": 86400,
                    "max_files_per_group": 30,
                    "max_retention_days": 30
                },
                "sending": {
                    "send_to_chat": False,
                    "target": {
                        "type": "current",
                        "custom_group_id": "",
                        "add_group_name_prefix": True
                    },
                    "api": {
                        "enabled": False,
                        "host": "0.0.0.0",
                        "port": 9966,
                        "token": ""
                    }
                }
            }
        
        # 创建存储目录
        self.summary_dir = "data/group_summaries"
        os.makedirs(self.summary_dir, exist_ok=True)
        
        # 初始化消息存储
        self.message_counters = {}  # 群ID -> 消息计数
        self.message_buffers = {}   # 群ID -> 消息列表
        self.last_summary_time = {} # 群ID -> 上次总结时间
        self.last_summarized_positions = {} # 群ID -> 上次总结的位置
        self.summarizing_groups = set()  # 正在进行总结的群ID集合，防止并发触发总结
        
        # 从文件加载状态
        self._load_state()
        
        # 定时保存状态的任务
        self.save_task = asyncio.create_task(self._periodic_save())
        
        # 如果启用了数据清理，创建定时清理任务
        if self.config.get("data_cleaning", {}).get("enabled", False):
            self.clean_task = asyncio.create_task(self._periodic_clean())
        
        # API相关配置
        self.api_config = self.config.get("sending", {}).get("api", {})
        self.api_enabled = self.api_config.get("enabled", False)
        self.api_in_queue = None
        self.api_process = None
        self.api_running = False

        # 如果启用了API，则初始化相关资源
        if self.api_enabled:
            # 如果没有配置令牌，则生成一个
            if not self.api_config.get("token"):
                self.api_config["token"] = secrets.token_urlsafe(32)
                # 如果有save_config方法，则保存配置
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
        
        logger.info("群消息总结插件已初始化")
        logger.info(f"群聊总结配置: summary_threshold={self.config['summary_threshold']}, "
                   f"min_interval={self.config['min_interval']}秒, "
                   f"send_to_chat={self.config.get('sending', {}).get('send_to_chat', False)}, "
                   f"api_enabled={self.api_enabled}")
    
    def _load_state(self):
        """从文件加载状态"""
        state_file = f"{self.summary_dir}/state.json"
        try:
            if os.path.exists(state_file):
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                    self.message_counters = state.get("counters", {})
                    self.last_summary_time = state.get("last_summary_time", {})
                    self.last_summarized_positions = state.get("last_summarized_positions", {})
                    logger.info("群消息总结状态加载成功")
        except Exception as e:
            logger.error(f"加载群消息总结状态失败: {e}")
    
    def _save_state(self):
        """保存状态到文件"""
        state_file = f"{self.summary_dir}/state.json"
        try:
            state = {
                "counters": self.message_counters,
                "last_summary_time": self.last_summary_time,
                "last_summarized_positions": self.last_summarized_positions,
                "last_update": int(time.time())
            }
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存群消息总结状态失败: {e}")
    
    async def _periodic_save(self):
        """定期保存状态"""
        while True:
            await asyncio.sleep(300)  # 每5分钟保存一次
            self._save_state()
    
    async def _periodic_clean(self):
        """定期清理旧的总结文件"""
        cleaning_config = self.config.get("data_cleaning", {})
        check_interval = cleaning_config.get("check_interval", 86400)  # 默认24小时
        
        while True:
            await asyncio.sleep(check_interval)
            try:
                self._clean_old_summaries()
                logger.info("已完成旧总结文件的清理")
            except Exception as e:
                logger.error(f"清理旧总结文件失败: {e}")
    
    def _clean_old_summaries(self):
        """清理旧的总结文件"""
        cleaning_config = self.config.get("data_cleaning", {})
        max_files_per_group = cleaning_config.get("max_files_per_group", 30)
        max_retention_days = cleaning_config.get("max_retention_days", 30)
        
        if not max_files_per_group and not max_retention_days:
            return  # 如果两个条件都没设置，则不执行清理
        
        # 按群ID分组收集文件
        group_files = {}
        for file_path in glob.glob(f"{self.summary_dir}/*.txt"):
            file_name = os.path.basename(file_path)
            # 文件名格式：群ID_日期_时间.txt
            parts = file_name.split('_', 1)
            if len(parts) >= 2:
                group_id = parts[0]
                if group_id not in group_files:
                    group_files[group_id] = []
                
                # 获取文件修改时间和创建时间
                mtime = os.path.getmtime(file_path)
                group_files[group_id].append((file_path, mtime))
        
        # 为每个群执行清理
        for group_id, files in group_files.items():
            # 按时间排序，最新的在前面
            files.sort(key=lambda x: x[1], reverse=True)
            
            # 删除超过数量限制的文件
            if max_files_per_group and len(files) > max_files_per_group:
                for file_path, _ in files[max_files_per_group:]:
                    try:
                        os.remove(file_path)
                        logger.debug(f"已删除超过数量限制的总结文件: {file_path}")
                    except Exception as e:
                        logger.error(f"删除文件失败: {file_path}, 错误: {e}")
            
            # 删除超过保留天数的文件
            if max_retention_days:
                cutoff_time = time.time() - (max_retention_days * 86400)  # 天数转秒
                for file_path, mtime in files:
                    if mtime < cutoff_time:
                        try:
                            os.remove(file_path)
                            logger.debug(f"已删除过期的总结文件: {file_path}")
                        except Exception as e:
                            logger.error(f"删除文件失败: {file_path}, 错误: {e}")
    
    # 使用register_event_message_type注册群消息处理函数
    @register_event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """处理群消息"""
        # 获取群ID
        group_id = event.get_group_id()
        if not group_id:
            return
        
        # 获取完整会话ID
        platform_name = event.get_platform_name() or "unknown"
        full_session_id = event.unified_msg_origin or f"{platform_name}:GroupMessage:{group_id}"
        
        # 初始化群消息缓存
        if group_id not in self.message_counters:
            logger.info(f"初始化群 {group_id} 的消息缓存 (完整ID: {full_session_id})")
            logger.info(f"完整会话ID: {full_session_id}")
            self.message_counters[group_id] = 0
            self.message_buffers[group_id] = []
            self.last_summary_time[group_id] = 0
            self.last_summarized_positions[group_id] = 0
        
        # 收集消息
        sender_name = event.get_sender_name() or "未知用户"
        sender_id = event.get_sender_id()
        content = event.get_message_str()
        timestamp = int(time.time())
        
        # 跳过空消息
        if not content.strip():
            logger.debug(f"跳过群 {group_id} (完整ID: {full_session_id}) 的空消息")
            return
        
        # 消息缓存，保持在最大容量以内
        logger.debug(f"添加消息到群 {group_id} (完整ID: {full_session_id}) 的缓存: {sender_name}: {content[:30]}...")
        self.message_buffers.setdefault(group_id, []).append({
            "sender_name": sender_name,
            "sender_id": sender_id,
            "content": content,
            "timestamp": timestamp
        })
        
        # 限制缓冲区大小
        max_size = self.config["max_buffer_size"]
        buffer_size = len(self.message_buffers[group_id])
        if buffer_size > max_size:
            logger.debug(f"群 {group_id} (完整ID: {full_session_id}) 缓冲区超过最大大小 {max_size}，清理旧消息")
            self.message_buffers[group_id] = self.message_buffers[group_id][-max_size:]
            logger.debug(f"群 {group_id} (完整ID: {full_session_id}) 缓冲区清理完成，当前大小: {len(self.message_buffers[group_id])}")
        
        # 增加计数
        self.message_counters[group_id] += 1
        logger.info(f"群 {group_id} (完整ID: {full_session_id}) 当前消息计数: {self.message_counters[group_id]}/{self.config['summary_threshold']}, 缓冲区大小: {buffer_size}")
        
        # 如果该群正在进行总结，跳过总结检查
        if group_id in self.summarizing_groups:
            logger.info(f"群 {group_id} (完整ID: {full_session_id}) 正在进行总结，跳过本次总结检查")
            return
            
        # 检查是否达到总结条件
        threshold = self.config["summary_threshold"]
        min_interval = self.config["min_interval"]
        current_time = int(time.time())
        last_time = self.last_summary_time.get(group_id, 0)
        time_diff = current_time - last_time
        
        # 记录触发条件的判断过程
        if self.message_counters[group_id] >= threshold:
            logger.info(f"群 {group_id} (完整ID: {full_session_id}) 消息数量已达到阈值: {self.message_counters[group_id]}/{threshold}")
            if time_diff >= min_interval or last_time == 0:
                logger.info(f"群 {group_id} (完整ID: {full_session_id}) 时间间隔满足条件: 距上次总结 {time_diff} 秒 >= {min_interval} 秒或从未总结")
                logger.info(f"群 {group_id} (完整ID: {full_session_id}) 触发自动总结")
                
                # 标记该群正在进行总结
                self.summarizing_groups.add(group_id)
                logger.info(f"已将群 {group_id} (完整ID: {full_session_id}) 标记为正在总结状态")
                
                try:
                    # 符合总结条件，进行总结
                    logger.info(f"开始为群 {group_id} (完整ID: {full_session_id}) 执行总结")
                    summary = await self.summarize_messages(group_id, event)
                    
                    # 重置计数
                    old_count = self.message_counters[group_id]
                    self.message_counters[group_id] = 0
                    logger.info(f"群 {group_id} (完整ID: {full_session_id}) 消息计数已重置: {old_count} -> 0")
                    
                    # 更新总结时间
                    old_time = self.last_summary_time[group_id]
                    self.last_summary_time[group_id] = current_time
                    logger.info(f"群 {group_id} (完整ID: {full_session_id}) 上次总结时间已更新: {old_time} -> {current_time}")
                    
                    # 保存状态
                    logger.info(f"保存群 {group_id} (完整ID: {full_session_id}) 的状态")
                    self._save_state()
                    
                    if summary:
                        logger.info(f"群 {group_id} (完整ID: {full_session_id}) 总结成功完成，总结内容长度: {len(summary)}")
                    else:
                        logger.warning(f"群 {group_id} (完整ID: {full_session_id}) 总结操作未返回有效内容")
                finally:
                    # 无论总结是否成功，都移除标记
                    self.summarizing_groups.discard(group_id)
                    logger.info(f"已移除群 {group_id} (完整ID: {full_session_id}) 的正在总结标记")
            else:
                logger.info(f"群 {group_id} (完整ID: {full_session_id}) 时间间隔不满足条件: 距上次总结 {time_diff} 秒 < {min_interval} 秒")
    
    async def summarize_messages(self, group_id: str, event: AstrMessageEvent) -> Optional[str]:
        """总结群消息
        
        Args:
            group_id: 群ID
            event: 消息事件
            
        Returns:
            str: 总结内容，如果总结失败则返回None
        """
        # 获取完整会话ID
        platform_name = event.get_platform_name() or "unknown"
        full_session_id = event.unified_msg_origin or f"{platform_name}:GroupMessage:{group_id}"
        
        messages = self.message_buffers.get(group_id, [])
        if not messages:
            logger.info(f"群 {group_id} (完整ID: {full_session_id}) 没有可总结的消息")
            return None
        
        # 获取上次总结的位置
        last_position = self.last_summarized_positions.get(group_id, 0)
        logger.info(f"群 {group_id} (完整ID: {full_session_id}) 上次总结位置: {last_position}, 当前消息数: {len(messages)}")
        
        # 如果位置超出范围（例如消息缓冲区被清理），重置为0
        if last_position >= len(messages):
            last_position = 0
            logger.info(f"群 {group_id} (完整ID: {full_session_id}) 上次总结位置超出范围，重置为0")
        
        # 只获取新消息
        new_messages = messages[last_position:]
        logger.info(f"群 {group_id} (完整ID: {full_session_id}) 有 {len(new_messages)} 条新消息需要总结")
        
        if not new_messages:
            logger.info(f"群 {group_id} (完整ID: {full_session_id}) 没有新消息需要总结")
            return None
            
        # 更新上次总结位置
        old_position = self.last_summarized_positions.get(group_id, 0)
        self.last_summarized_positions[group_id] = len(messages)
        logger.info(f"群 {group_id} (完整ID: {full_session_id}) 更新总结位置: {old_position} -> {len(messages)}")
        
        # 获取群信息
        group = await event.get_group()
        group_name = group.group_name if group else "未知群组"
        
        logger.info(f"开始总结群 {group_id} (完整ID: {full_session_id}) ({group_name}) 的 {len(new_messages)} 条新消息")
        
        # 准备提示词
        current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        msg_count = len(new_messages)
        
        # 构建消息文本
        message_texts = []
        # 最多使用最近200条消息，防止token过多
        recent_messages = new_messages[-200:]
        logger.info(f"使用最近 {len(recent_messages)}/{len(new_messages)} 条消息构建提示词")
        
        for msg in recent_messages:
            time_str = time.strftime("%H:%M", time.localtime(msg["timestamp"]))
            message_texts.append(MESSAGE_FORMAT.format(
                time_str=time_str,
                sender_name=msg['sender_name'],
                content=msg['content']
            ))
        
        # 构建提示词
        messages_str = "\n".join(message_texts)
        
        # 如果消息数量很少，添加额外提示
        additional_prompt = ""
        if msg_count < 10 and FEW_MESSAGES_PROMPT:
            logger.info(f"消息数量较少 ({msg_count} < 10)，添加额外提示")
            additional_prompt = f"\n\n{FEW_MESSAGES_PROMPT}"
        
        # 构建用户提示词
        user_prompt = SUMMARY_PROMPT_TEMPLATE.format(
            group_name=group_name,
            msg_count=msg_count,
            current_time=current_time,
            messages=messages_str
        )
        
        # 添加额外提示词（如果有）
        if additional_prompt:
            user_prompt += additional_prompt
        
        # 记录完整提示词
        logger.info(f"系统提示词长度: {len(SYSTEM_PROMPT)}")
        logger.info(f"用户提示词长度: {len(user_prompt)}")
        if additional_prompt:
            logger.info(f"附加提示词长度: {len(additional_prompt)}")
        logger.info(f"最终完整提示词内容:\n--- 系统提示词 ---\n{SYSTEM_PROMPT}\n\n--- 用户提示词 ---\n{user_prompt}")
        
        # 调用LLM进行总结
        try:
            logger.info(f"调用LLM为群 {group_id} (完整ID: {full_session_id}) ({group_name}) 生成总结")
            provider = self.context.get_using_provider()
            if not provider:
                logger.error("未找到默认LLM提供商")
                return None
            
            logger.info(f"已获取LLM提供商: {provider.__class__.__name__}")
            
            # 调用大模型生成总结，使用简化的接口调用方式
            try:
                # 尝试使用完整的参数列表
                logger.info(f"使用完整参数调用LLM生成总结")
                response = await provider.text_chat(
                    prompt=user_prompt,
                    session_id=None,  # 不关联到特定会话
                    contexts=[{"role": "system", "content": SYSTEM_PROMPT}],
                    image_urls=[]
                )
                logger.info(f"LLM调用成功，开始处理响应")
            except TypeError:
                # 如果出现参数错误，尝试使用简化版本
                logger.warning("完整参数调用失败，使用简化的LLM接口调用")
                response = await provider.text_chat(
                    prompt=user_prompt,
                    contexts=[{"role": "system", "content": SYSTEM_PROMPT}]
                )
                logger.info(f"简化版LLM调用成功，开始处理响应")
            
            summary = None
            if hasattr(response, 'role') and response.role == "assistant":
                summary = response.completion_text
                logger.info(f"从response.completion_text获取到总结内容")
            elif hasattr(response, 'content'):
                summary = response.content
                logger.info(f"从response.content获取到总结内容")
            elif isinstance(response, str):
                summary = response
                logger.info(f"从字符串响应获取到总结内容")
            
            if summary:
                logger.info(f"成功获取总结内容，长度: {len(summary)}")
                
                # 保存总结结果到文件
                timestamp = int(time.time())
                date_str = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(timestamp))
                filename = f"{self.summary_dir}/{group_id}_{date_str}.txt"
                logger.info(f"准备将总结保存到文件: {filename}")
                
                # 构建保存内容
                content = f"群组: {group_name} ({group_id})\n"
                content += f"时间: {current_time}\n"
                content += f"消息数: {len(new_messages)}\n"
                content += f"总结内容:\n\n{summary}\n"
                
                # 保存到文件
                try:
                    with open(filename, "w", encoding="utf-8") as f:
                        f.write(content)
                    logger.info(f"群 {group_id} (完整ID: {full_session_id}) ({group_name}) 的消息总结已保存到: {filename}")
                except Exception as e:
                    logger.error(f"保存总结到文件失败: {e}")
                
                # 检查是否需要发送到群聊
                sending_config = self.config.get("sending", {})
                if sending_config.get("send_to_chat", False):
                    logger.info(f"配置需要发送总结到群聊")
                    # 使用async for迭代异步生成器
                    await self._send_summary_to_chat(group_id, group_name, summary, event)
                else:
                    logger.info(f"配置不需要发送总结到群聊，仅保存到文件")
                
                return summary
            else:
                logger.error(f"群 {group_id} (完整ID: {full_session_id}) 的消息总结失败: 未获得有效总结内容")
                return None
            
        except Exception as e:
            logger.error(f"总结群 {group_id} (完整ID: {full_session_id}) 消息失败: {e}")
            logger.error(f"错误详情: {traceback.format_exc()}")
            return None
    
    async def _send_summary_to_chat(self, source_group_id: str, source_group_name: str, summary: str, event: AstrMessageEvent):
        """发送总结到群聊
        
        Args:
            source_group_id: 源群ID
            source_group_name: 源群名称
            summary: 总结内容
            event: 原始消息事件
        """
        try:
            # 获取完整会话ID
            platform_name = event.get_platform_name() or "unknown"
            full_source_id = event.unified_msg_origin or f"{platform_name}:GroupMessage:{source_group_id}"
            
            target_config = self.config.get("sending", {}).get("target", {})
            target_type = target_config.get("type", "current")
            
            logger.info(f"准备发送总结，源群ID: {source_group_id} (完整ID: {full_source_id}), 目标类型: {target_type}")
            
            # 根据目标类型选择发送方式
            if target_type == "api":
                # 通过API发送
                if not self.api_enabled:
                    logger.error("API发送功能未启用，无法发送总结")
                    return
                    
                # 构建消息内容
                if target_config.get("add_group_name_prefix", True):
                    message = f"【{source_group_name} 群聊消息总结】\n\n{summary}"
                else:
                    message = f"【群聊消息总结】\n\n{summary}"
                    
                # 获取目标会话ID
                custom_group_id = target_config.get("custom_group_id", "")
                if not custom_group_id:
                    logger.error("未配置API发送的目标群ID，无法发送总结")
                    return
                
                logger.info(f"通过API发送总结到: {custom_group_id}")
                
                # 这里假设API发送的逻辑
                # ...
                
            elif target_type == "current":
                # 发送到当前群聊
                try:
                    # 构建消息链
                    from astrbot.core.message.components import Plain
                    from astrbot.api.event import MessageChain
                    
                    if target_config.get("add_group_name_prefix", True):
                        content = f"【群聊消息总结】\n\n{summary}"
                    else:
                        content = summary
                        
                    message_chain = MessageChain(chain=[Plain(content)])
                    
                    # 获取当前平台名称和完整会话ID
                    platform_name = event.get_platform_name() or "unknown"
                    current_id = event.unified_msg_origin or f"{platform_name}:GroupMessage:{source_group_id}"
                    
                    logger.info(f"发送总结到当前群: {current_id}")
                    await self.context.send_message(current_id, message_chain)
                    logger.info(f"总结已发送到当前群: {current_id}")
                except Exception as e:
                    logger.error(f"发送总结到当前群失败: {e}")
                    logger.error(f"错误详情: {traceback.format_exc()}")
            elif target_type == "custom":
                # 发送到自定义群聊
                try:
                    # 构建消息链
                    from astrbot.core.message.components import Plain
                    from astrbot.api.event import MessageChain
                    
                    if target_config.get("add_group_name_prefix", True):
                        content = f"【{source_group_name} 群聊消息总结】\n\n{summary}"
                    else:
                        content = f"【群聊消息总结】\n\n{summary}"
                        
                    message_chain = MessageChain(chain=[Plain(content)])
                    
                    # 获取自定义群ID
                    custom_group_id = target_config.get("custom_group_id", "")
                    if not custom_group_id:
                        logger.error("未配置自定义目标群ID，无法发送总结")
                        return
                    
                    logger.info(f"自定义目标群ID: {custom_group_id}")
                    
                    # 获取平台信息
                    platform_name = event.get_platform_name() or "unknown"
                    logger.info(f"使用平台: {platform_name} 发送总结")
                    
                    # 确保使用完整格式的会话ID
                    if ":" not in custom_group_id:
                        logger.info(f"将简短群ID [{custom_group_id}] 转换为完整格式")
                        target_id = f"{platform_name}:GroupMessage:{custom_group_id}"
                    else:
                        # 已经是完整格式，直接使用
                        target_id = custom_group_id
                        logger.info(f"使用已提供的完整格式群ID: {target_id}")
                    
                    logger.info(f"尝试通过平台 {platform_name} 发送消息到群 {target_id}")
                    await self.context.send_message(target_id, message_chain)
                    logger.info(f"已将消息总结从 {source_group_id} (完整ID: {full_source_id}) 发送到自定义群 {target_id}")
                except Exception as e:
                    logger.error(f"发送总结到自定义群失败: {e}")
                    logger.error(f"错误详情: {traceback.format_exc()}")
            else:
                logger.warning(f"未知的目标类型: {target_type}")
                
        except Exception as e:
            logger.error(f"发送总结到群聊失败: {e}")
            logger.error(f"错误详情: {traceback.format_exc()}")
    
    @filter.command("summarize_now")
    @filter.permission_type(PermissionType.ADMIN)  # 仅管理员可执行
    async def force_summarize(self, event: AstrMessageEvent):
        """手动触发当前群的消息总结"""
        group_id = event.get_group_id()
        if not group_id:
            logger.info(f"命令 /summarize_now 必须在群聊中使用")
            yield event.plain_result("此命令只能在群聊中使用")
            return
        
        # 获取完整会话ID
        platform_name = event.get_platform_name() or "unknown"
        full_session_id = event.unified_msg_origin or f"{platform_name}:GroupMessage:{group_id}"
        
        if group_id not in self.message_buffers or not self.message_buffers[group_id]:
            logger.info(f"群 {group_id} (完整ID: {full_session_id}) 没有收集到消息，无法总结")
            yield event.plain_result("当前群没有收集到消息，无法总结")
            return
            
        # 检查该群是否正在总结中
        if group_id in self.summarizing_groups:
            logger.info(f"群 {group_id} (完整ID: {full_session_id}) 已在总结中，拒绝新的总结请求")
            yield event.plain_result("当前群正在进行总结，请稍后再试")
            return
        
        # 获取最新消息数量
        last_position = self.last_summarized_positions.get(group_id, 0)
        new_message_count = len(self.message_buffers[group_id]) - last_position
        logger.info(f"群 {group_id} (完整ID: {full_session_id}) 自上次总结以来有 {new_message_count} 条新消息")
        
        if new_message_count <= 0:
            logger.info(f"群 {group_id} (完整ID: {full_session_id}) 没有新消息，无需总结")
            yield event.plain_result("自上次总结以来没有新消息，无需总结")
            return
            
        logger.info(f"准备手动触发群 {group_id} (完整ID: {full_session_id}) 的总结")
        yield event.plain_result(f"正在总结自上次总结以来的 {new_message_count} 条新消息...")
        
        # 标记该群正在进行总结
        self.summarizing_groups.add(group_id)
        logger.info(f"已将群 {group_id} (完整ID: {full_session_id}) 标记为正在总结状态")
        
        try:
            logger.info(f"开始为群 {group_id} (完整ID: {full_session_id}) 执行总结")
            summary = await self.summarize_messages(group_id, event)
            
            # 重置计数
            old_count = self.message_counters[group_id]
            self.message_counters[group_id] = 0
            logger.info(f"群 {group_id} (完整ID: {full_session_id}) 消息计数已重置: {old_count} -> 0")
            
            # 更新总结时间
            current_time = int(time.time())
            old_time = self.last_summary_time[group_id]
            self.last_summary_time[group_id] = current_time
            logger.info(f"群 {group_id} (完整ID: {full_session_id}) 上次总结时间已更新: {old_time} -> {current_time}")
            
            # 保存状态
            logger.info(f"保存群 {group_id} (完整ID: {full_session_id}) 的状态")
            self._save_state()
            
            if not summary:
                logger.warning(f"群 {group_id} (完整ID: {full_session_id}) 总结失败")
                yield event.plain_result("总结失败，请查看日志获取详细信息")
            else:
                logger.info(f"群 {group_id} (完整ID: {full_session_id}) 总结成功，总结内容长度: {len(summary)}")
                yield event.plain_result("总结完成，已保存到文件")
        finally:
            # 无论总结是否成功，都移除标记
            self.summarizing_groups.discard(group_id)
            logger.info(f"已移除群 {group_id} (完整ID: {full_session_id}) 的正在总结标记")
    
    @filter.command("summary")
    @filter.permission_type(PermissionType.ADMIN)  # 仅管理员可执行
    async def summarize_with_count(self, event: AstrMessageEvent):
        """手动触发当前群的消息总结，可以指定消息数量"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("此命令只能在群聊中使用")
            return
        
        # 获取完整会话ID
        platform_name = event.get_platform_name() or "unknown"
        full_session_id = event.unified_msg_origin or f"{platform_name}:GroupMessage:{group_id}"
        
        if group_id not in self.message_buffers or not self.message_buffers[group_id]:
            logger.info(f"群 {group_id} (完整ID: {full_session_id}) 没有收集到消息，无法总结")
            yield event.plain_result("当前群没有收集到消息，无法总结")
            return
            
        # 检查该群是否正在总结中
        if group_id in self.summarizing_groups:
            logger.info(f"群 {group_id} (完整ID: {full_session_id}) 已在总结中，拒绝新的总结请求")
            yield event.plain_result("当前群正在进行总结，请稍后再试")
            return
        
        # 获取消息内容，尝试解析数量参数
        msg = event.get_message_str().strip()
        parts = msg.split()
        
        # 如果提供了数量参数
        count = None
        if len(parts) > 1:
            try:
                count = int(parts[1])
                if count <= 0:
                    yield event.plain_result("消息数量必须大于0")
                    return
            except ValueError:
                yield event.plain_result("参数格式错误，正确格式: /summary [数量]")
                return
        
        # 获取要总结的消息
        messages = self.message_buffers.get(group_id, [])
        if count and count < len(messages):
            messages = messages[-count:]
            
        yield event.plain_result(f"正在总结当前群内 {len(messages)} 条消息...")
        
        # 标记该群正在进行总结
        self.summarizing_groups.add(group_id)
        
        try:
            # 存储原始消息缓冲区、上次总结位置，并替换为要总结的部分
            original_buffer = self.message_buffers.get(group_id, [])
            original_position = self.last_summarized_positions.get(group_id, 0)
            self.message_buffers[group_id] = messages
            self.last_summarized_positions[group_id] = 0  # 从0开始总结临时缓冲区
            
            # 进行总结
            summary = await self.summarize_messages(group_id, event)
            
            # 恢复原始消息缓冲区和上次总结位置
            self.message_buffers[group_id] = original_buffer
            self.last_summarized_positions[group_id] = original_position
            
            # 不重置计数器，只保存当前时间
            self.last_summary_time[group_id] = int(time.time())
            self._save_state()
            
            if not summary:
                yield event.plain_result("总结失败，请查看日志获取详细信息")
            else:
                yield event.plain_result("总结完成，已保存到文件")
        finally:
            # 无论总结是否成功，都移除标记
            self.summarizing_groups.discard(group_id)
    
    @filter.command("summary_status")
    async def check_status(self, event: AstrMessageEvent):
        """查看当前群的消息收集状态"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("此命令只能在群聊中使用")
            return
        
        # 获取完整会话ID
        platform_name = event.get_platform_name() or "unknown"
        full_session_id = event.unified_msg_origin or f"{platform_name}:GroupMessage:{group_id}"
        
        count = self.message_counters.get(group_id, 0)
        threshold = self.config["summary_threshold"]
        last_time = self.last_summary_time.get(group_id, 0)
        
        # 获取新消息数量
        total_messages = len(self.message_buffers.get(group_id, []))
        last_position = self.last_summarized_positions.get(group_id, 0)
        new_messages = total_messages - last_position
        
        if last_time > 0:
            last_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_time))
        else:
            last_time_str = "从未总结"
            
        status = f"当前群消息总结状态 (完整ID: {full_session_id}):\n"
        status += f"已收集消息数: {count}/{threshold}\n"
        status += f"自上次总结以来新消息: {new_messages} 条\n"
        status += f"上次总结时间: {last_time_str}"
        
        # 添加发送设置信息
        sending_config = self.config.get("sending", {})
        if sending_config.get("send_to_chat", False):
            target_config = sending_config.get("target", {})
            target_type = target_config.get("type", "current")
            
            if target_type == "current":
                status += "\n总结发送: 启用 (发送到当前群)"
            else:
                custom_id = target_config.get("custom_group_id", "未设置")
                status += f"\n总结发送: 启用 (发送到指定群 {custom_id})"
        else:
            status += "\n总结发送: 禁用 (仅保存到文件)"
            
        yield event.plain_result(status)
    
    @filter.command("summary_help")
    async def show_help(self, event: AstrMessageEvent):
        """显示插件帮助信息"""
        help_text = """群聊消息总结插件使用帮助:
        
/summary_status - 查看当前群的消息收集状态
/summarize_now - 立即总结当前收集的所有消息 (仅管理员)
/summary [数量] - 立即总结指定数量的最近消息 (仅管理员)
/summary_debug [计数] - 设置当前群消息计数器的值用于调试自动触发功能 (仅管理员)
/get_session_id - 获取当前会话的ID信息，用于配置白名单或自定义目标群"""

        # 如果API功能已启用，添加API相关命令
        if self.api_enabled:
            help_text += """
/summary_api_info - 显示API相关信息，包括访问地址和令牌 (仅管理员)"""
            
        help_text += """
/summary_help - 显示本帮助信息

自动总结会在消息达到阈值({threshold}条)且满足时间间隔({min_interval}秒)后触发。
根据配置，总结结果会保存在本地文件中，也可能会发送到群聊。

高级功能:
- 提示词配置: 在配置文件中可以修改系统提示词(summary_system_prompt)和其他提示词模板(prompts部分)
- 外部提示词: 可以编辑prompts.py文件自定义提示词，并通过use_external_prompts选项启用或禁用
- 消息发送: 可以配置总结结果发送到当前群或自定义目标群""".format(
            threshold=self.config["summary_threshold"],
            min_interval=self.config["min_interval"]
        )
        
        yield event.plain_result(help_text)
    
    @filter.command("summary_debug")
    @filter.permission_type(PermissionType.ADMIN)  # 仅管理员可执行
    async def debug_counter(self, event: AstrMessageEvent):
        """调试命令：设置消息计数以测试自动触发功能"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("此命令只能在群聊中使用")
            return
        
        # 获取完整会话ID
        platform_name = event.get_platform_name() or "unknown"
        full_session_id = event.unified_msg_origin or f"{platform_name}:GroupMessage:{group_id}"
        
        # 获取消息内容，尝试解析参数
        msg = event.get_message_str().strip()
        parts = msg.split()
        
        # 如果提供了计数参数
        if len(parts) > 1:
            try:
                count = int(parts[1])
                if count < 0:
                    yield event.plain_result("消息计数不能为负数")
                    return
                
                # 设置消息计数
                old_count = self.message_counters.get(group_id, 0)
                self.message_counters[group_id] = count
                
                yield event.plain_result(f"已将群 {group_id} (完整ID: {full_session_id}) 的消息计数从 {old_count} 设置为 {count}")
                
                # 检查是否达到总结条件
                threshold = self.config["summary_threshold"]
                min_interval = self.config["min_interval"]
                current_time = int(time.time())
                last_time = self.last_summary_time.get(group_id, 0)
                time_diff = current_time - last_time
                
                status = f"总结阈值: {threshold} 条消息\n"
                status += f"当前计数: {count} 条消息\n"
                status += f"时间间隔: {time_diff} 秒 (最小间隔: {min_interval} 秒)\n"
                
                if count >= threshold:
                    status += "✅ 消息数量已达到阈值\n"
                else:
                    status += "❌ 消息数量未达到阈值\n"
                    
                if time_diff >= min_interval or last_time == 0:
                    status += "✅ 时间间隔条件满足\n"
                else:
                    status += f"❌ 时间间隔不足 (还需等待 {min_interval - time_diff} 秒)\n"
                
                if count >= threshold and (time_diff >= min_interval or last_time == 0):
                    status += "\n✅ 满足自动触发条件，将在下一条消息时触发总结"
                else:
                    status += "\n❌ 暂不满足自动触发条件"
                
                yield event.plain_result(status)
            except ValueError:
                yield event.plain_result("参数格式错误，正确格式: /summary_debug [计数]")
        else:
            yield event.plain_result("请指定要设置的消息计数，格式: /summary_debug [计数]")
    
    @filter.command("get_session_id")
    async def get_session_id(self, event: AstrMessageEvent):
        """获取当前会话的ID和详细信息"""
        unified_id = event.unified_msg_origin
        group_id = event.get_group_id() or "无"
        sender_id = event.get_sender_id() or "无"
        platform = event.get_platform_name() or "无"
        msg_type = event.get_message_type() or "无"
        
        info = f"会话统一ID: {unified_id}\n"
        info += f"群ID: {group_id}\n"
        info += f"发送者ID: {sender_id}\n"
        info += f"平台名称: {platform}\n"
        info += f"消息类型: {msg_type}\n"
        info += "\n此信息可用于配置白名单或自定义目标群ID"
        
        yield event.plain_result(info)
        
        # 同时在日志中记录
        logger.info(f"会话ID信息: {unified_id}")
        logger.info(f"群ID: {group_id}, 发送者ID: {sender_id}, 平台: {platform}, 消息类型: {msg_type}")
    
    @filter.command("summary_api_info")
    @filter.permission_type(PermissionType.ADMIN)  # 仅管理员可执行
    async def api_info(self, event: AstrMessageEvent):
        """显示API相关信息"""
        if not self.api_enabled:
            yield event.plain_result("群消息总结API功能未启用，请在配置中启用")
            return
            
        host = self.api_config.get("host", "0.0.0.0")
        port = self.api_config.get("port", 9966)
        token = self.api_config.get("token", "")
        
        # 如果host是0.0.0.0，显示本地IP地址可能更有用
        display_host = "localhost" if host == "0.0.0.0" else host
        
        info = "群消息总结HTTP API信息:\n\n"
        info += f"API地址: http://{display_host}:{port}\n"
        info += f"API令牌: {token}\n\n"
        info += "使用示例 (curl):\n"
        info += f"curl -X POST \\\n"
        info += f"  -H \"Authorization: Bearer {token}\" \\\n"
        info += f"  -H \"Content-Type: application/json\" \\\n"
        info += f"  -d '{{\"content\":\"测试消息\",\"umo\":\"目标ID\"}}' \\\n"
        info += f"  http://{display_host}:{port}/send"
        
        yield event.plain_result(info)
    
    async def terminate(self):
        """插件终止时的清理工作"""
        try:
            # 保存状态
            self._save_state()
            
            # 清除总结标记
            self.summarizing_groups.clear()
            
            # 取消现有任务
            if hasattr(self, 'save_task') and not self.save_task.done():
                self.save_task.cancel()
                try:
                    await self.save_task
                except asyncio.CancelledError:
                    pass
                
            if hasattr(self, 'clean_task') and not self.clean_task.done():
                self.clean_task.cancel()
                try:
                    await self.clean_task
                except asyncio.CancelledError:
                    pass
                
            # 关闭API服务
            if hasattr(self, 'api_enabled') and self.api_enabled and hasattr(self, 'api_running') and self.api_running:
                self.api_running = False
                if hasattr(self, 'api_process') and self.api_process:
                    self.api_process.terminate()
                    self.api_process.join(5)
                if hasattr(self, 'api_in_queue') and self.api_in_queue:
                    while not self.api_in_queue.empty():
                        try:
                            self.api_in_queue.get(False)
                        except:
                            pass
                    
        except Exception as e:
            logger.error(f"群消息总结插件终止时发生错误: {e}")
            logger.error(f"错误详情: {traceback.format_exc()}")
        
        logger.info("群消息总结插件已终止")

    async def initialize(self):
        """初始化插件，如果启用了API功能则启动API服务器"""
        if self.api_enabled:
            try:
                from hypercorn.asyncio import serve
                from hypercorn.config import Config
                from quart import Quart, abort, jsonify, request
                
                logger.info("初始化群消息总结插件的HTTP API服务")
                self.api_in_queue = Queue()
                self.api_process = Process(
                    target=self._run_api_server,
                    args=(
                        self.api_config.get("token"),
                        self.api_config.get("host", "0.0.0.0"),
                        self.api_config.get("port", 9966),
                        self.api_in_queue,
                    ),
                    daemon=True,
                )
                self.api_process.start()
                self.api_running = True
                asyncio.create_task(self._process_api_messages())
                logger.info(f"群消息总结插件的HTTP API服务已启动，地址: http://{self.api_config.get('host', '0.0.0.0')}:{self.api_config.get('port', 9966)}")
            except ImportError:
                logger.error("无法启动HTTP API服务：缺少必要的依赖库。请安装hypercorn和quart库。")
                self.api_enabled = False
                self.api_running = False

    def _run_api_server(self, token, host, port, in_queue):
        """运行API服务器（在单独的进程中）"""
        try:
            from hypercorn.asyncio import serve
            from hypercorn.config import Config
            from quart import Quart, abort, jsonify, request
            
            # 创建Quart应用
            app = Quart(__name__)
            
            # 设置路由
            @app.errorhandler(400)
            async def bad_request(e):
                return jsonify({"error": "Bad Request", "details": str(e)}), 400

            @app.errorhandler(403)
            async def forbidden(e):
                return jsonify({"error": "Forbidden", "details": str(e)}), 403

            @app.errorhandler(500)
            async def server_error(e):
                return jsonify({"error": "Internal Server Error", "details": str(e)}), 500

            # 发送消息接口
            @app.route("/send", methods=["POST"])
            async def send_endpoint():
                auth_header = request.headers.get("Authorization")
                if not auth_header or auth_header != f"Bearer {token}":
                    print(f"来自 {request.remote_addr} 的无效令牌")
                    abort(403, description="无效令牌")

                data = await request.get_json()
                if not data:
                    abort(400, description="无效的JSON数据")

                required_fields = {"content", "umo"}
                if missing := required_fields - data.keys():
                    abort(400, description=f"缺少必填字段: {missing}")

                message = {
                    "message_id": data.get("message_id", str(uuid.uuid4())),
                    "content": data["content"],
                    "umo": data["umo"],
                    "type": data.get("type", "text"),
                    "callback_url": data.get("callback_url"),
                }

                in_queue.put(message)
                print(f"消息已加入队列: {message['message_id']}")

                return jsonify({
                    "status": "queued",
                    "message_id": message["message_id"],
                    "queue_size": in_queue.qsize(),
                })

            # 健康检查接口
            @app.route("/health", methods=["GET"])
            async def health_check():
                return jsonify({
                    "status": "ok",
                    "queue_size": in_queue.qsize(),
                })
                
            # 启动服务器
            config = Config()
            config.bind = [f"{host}:{port}"]
            
            async def run_server():
                print(f"群消息总结API服务已启动于 {host}:{port}")
                await serve(app, config)
                
            # 运行服务器
            asyncio.run(run_server())
            
        except ImportError:
            print("缺少运行HTTP API服务器所需的依赖库")
        except Exception as e:
            print(f"启动API服务器失败: {e}")
            
    async def _process_api_messages(self):
        """处理API消息队列中的消息"""
        while self.api_running:
            try:
                # 使用非阻塞方式获取消息，避免进程终止时阻塞
                try:
                    message = self.api_in_queue.get_nowait()
                except Exception:
                    # 队列为空，等待一段时间后继续
                    await asyncio.sleep(0.5)
                    continue
                
                logger.info(f"处理API消息: {message['message_id']}")
                try:
                    result = {"message_id": message["message_id"], "success": True}
                    
                    # 构建消息链
                    if message["type"] == "image":
                        try:
                            # 如果是base64编码的图片内容
                            image_data = base64.b64decode(message["content"])
                            from PIL import Image as PILImage
                            PILImage.open(BytesIO(image_data)).verify()  # 验证图片格式
                            from astrbot.core.message.components import Image
                            chain = MessageChain(chain=[Image.fromBytes(image_data)])
                        except Exception as e:
                            logger.error(f"图片处理失败: {e}")
                            raise Exception(f"不支持的图片格式: {e}")
                    else:
                        # 文本消息
                        from astrbot.core.message.components import Plain
                        chain = MessageChain(chain=[Plain(message["content"])])
                    
                    # 发送消息
                    await self.context.send_message(message["umo"], chain)
                    logger.info(f"API消息已发送: {message['message_id']}")
                    
                except Exception as e:
                    logger.error(f"API消息处理失败: {e}")
                    result.update({"success": False, "error": str(e)})
                finally:
                    # 如果有回调URL，发送结果通知
                    if callback_url := message.get("callback_url"):
                        await self._send_api_callback(callback_url, result)
            except Exception as e:
                # 捕获所有异常，确保循环不会中断
                logger.error(f"处理API消息队列时发生异常: {e}")
                await asyncio.sleep(1)

    async def _send_api_callback(self, url, data):
        """发送API回调通知"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data, timeout=5) as resp:
                    if resp.status >= 400:
                        logger.warning(f"API回调失败: 状态码 {resp.status}")
        except Exception as e:
            logger.error(f"API回调错误: {e}") 
