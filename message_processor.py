"""
群聊消息总结插件的消息处理模块
提供消息收集、处理和总结的核心功能
"""

import os
import sys
import time
import traceback
from typing import Dict, List, Any, Optional, Set, Tuple

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.star.filter.event_message_type import EventMessageType
from astrbot.core.star.register import register_event_message_type
from astrbot.api.platform import MessageType

# 确保当前目录在sys.path中以便正确导入本地模块
current_dir = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)
    logger.info(f"[MessageProcessor] 已将插件目录添加到sys.path: {current_dir}")

# 导入提示词
try:
    from prompts import SYSTEM_PROMPT, SUMMARY_PROMPT_TEMPLATE, MESSAGE_FORMAT, FEW_MESSAGES_PROMPT
    logger.info("[MessageProcessor] 成功导入提示词模块")
except ImportError:
    # 默认提示词，仅在无法导入时使用
    logger.warning("[MessageProcessor] 无法导入提示词模块，使用默认提示词")
    SYSTEM_PROMPT = "你是一个群聊消息总结助手，请根据以下消息内容进行总结。"
    SUMMARY_PROMPT_TEMPLATE = "请总结以下消息内容，提取主要讨论话题和重点信息。\n\n{messages}"
    MESSAGE_FORMAT = "[{time_str}] {sender_name}: {content}"
    FEW_MESSAGES_PROMPT = "消息数量较少，请重点关注其中可能包含的重要信息。"

# 导入LLM提供商
try:
    import llm_provider
    logger.info("[MessageProcessor] 成功导入llm_provider模块")
except ImportError as e:
    logger.error(f"[MessageProcessor] 导入llm_provider模块失败: {e}")
    # 尝试使用importlib动态导入
    try:
        import importlib
        llm_provider = importlib.import_module("llm_provider")
        logger.info("[MessageProcessor] 成功通过importlib导入llm_provider模块")
    except ImportError as e2:
        logger.error(f"[MessageProcessor] 通过importlib导入llm_provider模块仍失败: {e2}")
        raise ImportError(f"无法导入llm_provider模块，消息处理器无法正常工作: {e2}")

class MessageProcessor:
    """消息处理类，提供消息收集和总结的核心功能"""
    
    def __init__(self, context, config: Dict[str, Any], state_manager):
        """初始化消息处理器
        
        Args:
            context: AstrBot上下文对象
            config: 配置信息
            state_manager: 状态管理器实例
        """
        self.context = context
        self.config = config
        self.state_manager = state_manager
        
        # 主要数据直接从状态管理器获取
        self.message_buffers = state_manager.message_buffers
        self.message_counts = state_manager.message_counts
        self.last_summary_times = state_manager.last_summary_times
        self.summarizing_groups = state_manager.summarizing_groups
        self.summary_dir = state_manager.summary_dir
        
        # 初始化配置参数
        self.summary_threshold = config.get("summary_threshold", 300)
        self.min_interval = config.get("min_interval", 3600)
        # 计算缓冲区大小，为总结阈值的1.5倍，确保能容纳足够的消息
        self.buffer_size = int(self.summary_threshold * 1.5)
    
    @register_event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """处理群消息事件
        
        Args:
            event: 消息事件
        """
        # 获取群ID
        if not event.unified_msg_origin or not ":GroupMessage:" in event.unified_msg_origin:
            return
            
        group_id = event.unified_msg_origin.split(":GroupMessage:")[1]
        
        # 获取完整会话ID
        platform_name = event.get_platform_name() or "unknown"
        full_session_id = event.unified_msg_origin or f"{platform_name}:GroupMessage:{group_id}"
        
        # 获取群名称
        group_name = "未知群组"
        try:
            group = await event.get_group()
            if group:
                group_name = group.group_name
        except Exception as e:
            logger.warning(f"获取群名称失败: {e}")
        
        # 忽略命令消息
        message_content = event.get_message_content()
        if message_content.startswith("/"):
            return
            
        # 获取发送者信息
        sender_name = event.sender_name or "未知用户"
        
        # 记录消息
        timestamp = time.time()
        message = {
            "sender_name": sender_name,
            "content": message_content,
            "timestamp": timestamp
        }
        
        # 添加到缓冲区
        if group_id not in self.message_buffers:
            self.message_buffers[group_id] = []
        
        # 如果缓冲区已满，移除最旧的消息
        if len(self.message_buffers[group_id]) >= self.buffer_size:
            self.message_buffers[group_id].pop(0)
            
        self.message_buffers[group_id].append(message)
        
        # 增加计数器
        if group_id not in self.message_counts:
            self.message_counts[group_id] = 0
        self.message_counts[group_id] += 1
        
        # 检查是否需要触发总结
        if self.message_counts[group_id] >= self.summary_threshold:
            # 检查距离上次总结的时间间隔
            last_time = self.last_summary_times.get(group_id, 0)
            current_time = time.time()
            if current_time - last_time >= self.min_interval:
                # 检查是否已经在进行总结
                if group_id not in self.summarizing_groups:
                    logger.info(f"群 {group_id} ({group_name}) 消息数量 {self.message_counts[group_id]} 达到阈值 {self.summary_threshold}，"
                              f"距离上次总结已经 {int(current_time - last_time)} 秒，开始自动总结")
                    
                    # 标记正在总结
                    self.summarizing_groups.add(group_id)
                    
                    try:
                        # 执行总结
                        summary = await self.summarize_messages(group_id, event)
                        
                        if summary:
                            logger.info(f"群 {group_id} ({group_name}) 的消息自动总结成功")
                            # 重置消息计数
                            self.message_counts[group_id] = 0
                            # 更新最后总结时间
                            self.last_summary_times[group_id] = current_time
                        else:
                            logger.error(f"群 {group_id} ({group_name}) 的消息自动总结失败")
                    except Exception as e:
                        logger.error(f"总结群 {group_id} ({group_name}) 消息时发生错误: {e}")
                        logger.error(f"错误详情: {traceback.format_exc()}")
                    finally:
                        # 清除正在总结标记
                        self.summarizing_groups.discard(group_id)
                else:
                    logger.info(f"群 {group_id} ({group_name}) 已经在进行总结，跳过自动总结")

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
        
        # 获取群信息
        group = await event.get_group()
        group_name = group.group_name if group else "未知群组"
        
        # 使用最近的N条消息进行总结，N为总结阈值
        threshold = self.config["summary_threshold"]
        if len(messages) > threshold:
            messages_to_summarize = messages[-threshold:]
            logger.info(f"群 {group_id} (完整ID: {full_session_id}) ({group_name}) 消息数量 {len(messages)} 超过阈值 {threshold}，将使用最近 {threshold} 条消息")
        else:
            messages_to_summarize = messages
            logger.info(f"群 {group_id} (完整ID: {full_session_id}) ({group_name}) 消息数量 {len(messages)} 未超过阈值 {threshold}，使用全部消息")
        
        msg_count = len(messages_to_summarize)
        logger.info(f"开始总结群 {group_id} (完整ID: {full_session_id}) ({group_name}) 的 {msg_count} 条消息")
        
        # 准备提示词
        current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        
        # 构建消息文本
        message_texts = []
        # 最多使用最近200条消息，防止token过多
        if msg_count > 200:
            recent_messages = messages_to_summarize[-200:]
            logger.info(f"消息数量 {msg_count} 超过200，将只使用最近 200 条消息构建提示词")
        else:
            recent_messages = messages_to_summarize
        
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
            
            # 使用新的方法获取LLM提供商
            provider = await llm_provider.get_llm_provider(self.context, self.config)
            if not provider:
                logger.error("未找到可用的LLM提供商")
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
                content += f"消息数: {len(messages_to_summarize)}\n"
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
                # 这部分应该由API服务模块处理，这里不实现
                logger.info("API发送功能由API服务模块处理")
                return
                    
            # 构建消息内容
            message_content = ""
            if target_type != "current" and target_config.get("add_group_name_prefix", True):
                message_content = f"【{source_group_name} 群聊消息总结】\n\n{summary}"
            else:
                message_content = f"【群聊消息总结】\n\n{summary}"
            
            # 获取目标群ID
            target_id = ""
            if target_type == "current":
                target_id = full_source_id
                logger.info(f"发送总结到源群: {target_id}")
            elif target_type == "custom":
                target_id = target_config.get("custom_group_id", "")
                if not target_id:
                    logger.error("未设置自定义目标群ID，无法发送总结")
                    return
                logger.info(f"发送总结到自定义群: {target_id}")
            else:
                logger.error(f"不支持的目标类型: {target_type}")
                return
            
            # 构建消息链
            from astrbot.core.message.components import Plain
            chain = MessageChain(chain=[Plain(message_content)])
            
            # 发送消息
            await self.context.send_message(target_id, chain)
            logger.info(f"成功发送总结到群 {target_id}")
            
        except Exception as e:
            logger.error(f"发送总结到群聊失败: {e}")
            logger.error(f"错误详情: {traceback.format_exc()}") 
