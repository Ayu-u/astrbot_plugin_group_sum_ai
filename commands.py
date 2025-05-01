"""
群聊消息总结插件的命令处理模块
提供各种命令处理功能
"""

import os
import sys
import time
import traceback
from typing import Dict, Any, Optional, List, Set, Callable, Awaitable, Tuple

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.star.filter.permission import PermissionType

# 确保当前目录在sys.path中以便正确导入本地模块
current_dir = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)
    logger.info(f"[CommandHandler] 已将插件目录添加到sys.path: {current_dir}")

# 导入可能需要的模块
try:
    import llm_provider
    logger.info("[CommandHandler] 成功导入llm_provider模块")
except ImportError as e:
    logger.error(f"[CommandHandler] 导入llm_provider模块失败: {e}")
    # 尝试使用importlib动态导入
    try:
        import importlib
        llm_provider = importlib.import_module("llm_provider")
        logger.info("[CommandHandler] 成功通过importlib导入llm_provider模块")
    except ImportError as e2:
        logger.error(f"[CommandHandler] 通过importlib导入llm_provider模块仍失败: {e2}")
        raise ImportError(f"无法导入llm_provider模块，命令处理器无法正常工作: {e2}")

class CommandHandler:
    """命令处理类，提供用户命令处理功能"""
    
    def __init__(self, context, config: Dict[str, Any], state_manager, message_processor, api_service=None):
        """初始化命令处理器
        
        Args:
            context: AstrBot上下文对象
            config: 配置信息
            state_manager: 状态管理器实例
            message_processor: 消息处理器实例
            api_service: API服务实例(可选)
        """
        self.context = context
        self.config = config
        self.state_manager = state_manager
        self.message_processor = message_processor
        self.api_service = api_service
        
        # 主要数据直接从状态管理器获取
        self.message_buffers = state_manager.message_buffers
        self.message_counts = state_manager.message_counts
        self.last_summary_times = state_manager.last_summary_times
        self.summarizing_groups = state_manager.summarizing_groups
        self.summary_dir = state_manager.summary_dir
    
    @filter.command("summarize_now")
    @filter.permission_type(PermissionType.ADMIN)  # 仅管理员可执行
    async def force_summarize(self, event: AstrMessageEvent):
        """立即总结当前群聊的所有消息"""
        # 获取群ID
        if not event.unified_msg_origin or not ":GroupMessage:" in event.unified_msg_origin:
            yield event.plain_result("该命令仅在群聊中可用")
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
        
        # 检查是否有收集的消息
        if not self.message_buffers.get(group_id, []):
            logger.info(f"群 {group_id} ({group_name}) 没有收集到消息，无法总结")
            yield event.plain_result("当前没有收集到消息，无法总结")
            return
        
        # 检查是否已经在进行总结
        if group_id in self.summarizing_groups:
            logger.info(f"群 {group_id} ({group_name}) 正在进行总结，请稍后再试")
            yield event.plain_result("正在进行总结，请稍后再试")
            return
        
        # 获取消息数量
        msg_count = len(self.message_buffers.get(group_id, []))
        logger.info(f"准备总结群 {group_id} ({group_name}) 的 {msg_count} 条消息")
        
        # 标记正在总结
        self.summarizing_groups.add(group_id)
        
        try:
            # 执行总结
            yield event.plain_result(f"开始总结群 {group_name} 的 {msg_count} 条消息...")
            
            summary = await self.message_processor.summarize_messages(group_id, event)
            
            if summary:
                logger.info(f"群 {group_id} ({group_name}) 的消息总结成功")
                # 重置消息计数
                self.message_counts[group_id] = 0
                # 更新最后总结时间
                self.last_summary_times[group_id] = time.time()
                yield event.plain_result(f"群消息总结已完成，共总结了 {msg_count} 条消息")
            else:
                logger.error(f"群 {group_id} ({group_name}) 的消息总结失败")
                yield event.plain_result("消息总结失败，请查看日志了解详情")
        except Exception as e:
            logger.error(f"总结群 {group_id} ({group_name}) 消息时发生错误: {e}")
            logger.error(f"错误详情: {traceback.format_exc()}")
            yield event.plain_result(f"总结过程中发生错误: {str(e)}")
        finally:
            # 清除正在总结标记
            self.summarizing_groups.discard(group_id)
    
    @filter.command("summary")
    @filter.permission_type(PermissionType.ADMIN)  # 仅管理员可执行
    async def summarize_with_count(self, event: AstrMessageEvent):
        """总结指定数量的最近消息"""
        # 获取群ID
        if not event.unified_msg_origin or not ":GroupMessage:" in event.unified_msg_origin:
            yield event.plain_result("该命令仅在群聊中可用")
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
        
        # 检查是否有收集的消息
        if not self.message_buffers.get(group_id, []):
            logger.info(f"群 {group_id} ({group_name}) 没有收集到消息，无法总结")
            yield event.plain_result("当前没有收集到消息，无法总结")
            return
        
        # 检查是否已经在进行总结
        if group_id in self.summarizing_groups:
            logger.info(f"群 {group_id} ({group_name}) 正在进行总结，请稍后再试")
            yield event.plain_result("正在进行总结，请稍后再试")
            return
        
        # 解析消息，获取数量参数
        message_content = event.get_message_content()
        parts = message_content.strip().split()
        
        # 提取数量参数
        count = None
        if len(parts) > 1:
            try:
                count = int(parts[1])
                if count <= 0:
                    yield event.plain_result("消息数量必须大于0")
                    return
            except ValueError:
                yield event.plain_result("无效的消息数量，请输入一个正整数")
                return
        
        # 获取消息
        messages = self.message_buffers.get(group_id, [])
        total_count = len(messages)
        
        # 如果指定了数量，仅使用最近的N条消息
        if count and count < total_count:
            messages_to_summarize = messages[-count:]
            msg_count = len(messages_to_summarize)
            logger.info(f"将总结群 {group_id} ({group_name}) 的最近 {msg_count}/{total_count} 条消息")
        else:
            messages_to_summarize = messages
            msg_count = total_count
            logger.info(f"将总结群 {group_id} ({group_name}) 的全部 {msg_count} 条消息")
        
        # 标记正在总结
        self.summarizing_groups.add(group_id)
        
        try:
            # 执行总结
            yield event.plain_result(f"开始总结群 {group_name} 的 {msg_count} 条消息...")
            
            # 因为我们可能只总结一部分消息，所以需要暂时替换消息缓冲区
            original_messages = self.message_buffers.get(group_id, [])
            if count and count < total_count:
                self.message_buffers[group_id] = messages_to_summarize
            
            summary = await self.message_processor.summarize_messages(group_id, event)
            
            # 恢复原始消息缓冲区
            if count and count < total_count:
                self.message_buffers[group_id] = original_messages
            
            if summary:
                logger.info(f"群 {group_id} ({group_name}) 的消息总结成功")
                # 注意：这里不重置消息计数，因为我们可能只总结了一部分消息
                # 只有在总结所有消息时才更新最后总结时间
                if not count or count >= total_count:
                    self.message_counts[group_id] = 0
                    self.last_summary_times[group_id] = time.time()
                yield event.plain_result(f"群消息总结已完成，共总结了 {msg_count} 条消息")
            else:
                logger.error(f"群 {group_id} ({group_name}) 的消息总结失败")
                yield event.plain_result("消息总结失败，请查看日志了解详情")
        except Exception as e:
            logger.error(f"总结群 {group_id} ({group_name}) 消息时发生错误: {e}")
            logger.error(f"错误详情: {traceback.format_exc()}")
            yield event.plain_result(f"总结过程中发生错误: {str(e)}")
        finally:
            # 清除正在总结标记
            self.summarizing_groups.discard(group_id)
    
    @filter.command("summary_status")
    async def check_status(self, event: AstrMessageEvent):
        """查看当前消息收集状态"""
        # 获取群ID
        if not event.unified_msg_origin or not ":GroupMessage:" in event.unified_msg_origin:
            yield event.plain_result("该命令仅在群聊中可用")
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
        
        # 获取当前消息数量
        message_count = self.message_counts.get(group_id, 0)
        summary_threshold = self.config.get("summary_threshold", 300)
        buffer_size = len(self.message_buffers.get(group_id, []))
        
        # 格式化上次总结时间
        last_summary_time = self.last_summary_times.get(group_id, 0)
        if last_summary_time > 0:
            time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_summary_time))
            
            # 计算距离上次总结的时间
            current_time = time.time()
            diff_seconds = int(current_time - last_summary_time)
            diff_hours = diff_seconds // 3600
            diff_minutes = (diff_seconds % 3600) // 60
            diff_str = f"{diff_hours}小时{diff_minutes}分钟"
        else:
            time_str = "从未总结"
            diff_str = "N/A"
        
        # 获取发送设置
        sending_config = self.config.get("sending", {})
        send_to_chat = sending_config.get("send_to_chat", False)
        target_type = sending_config.get("target", {}).get("type", "current")
        
        # 构建发送配置信息
        if not send_to_chat:
            sending_info = "不发送到群聊（仅保存到文件）"
        elif target_type == "current":
            sending_info = "发送到当前群聊"
        elif target_type == "custom":
            custom_id = sending_config.get("target", {}).get("custom_group_id", "")
            sending_info = f"发送到指定群聊: {custom_id}"
        elif target_type == "api":
            sending_info = "通过API发送"
        else:
            sending_info = "未知发送配置"
        
        # 构建状态消息
        status = f"【群 {group_name} 消息收集状态】\n\n"
        status += f"会话ID: {full_session_id}\n"
        status += f"当前消息计数: {message_count}/{summary_threshold}\n"
        status += f"已收集消息: {buffer_size} 条\n"
        status += f"缓冲区大小: {int(summary_threshold * 1.5)} 条\n"
        status += f"上次总结时间: {time_str}\n"
        status += f"距上次总结: {diff_str}\n"
        status += f"发送配置: {sending_info}"
        
        yield event.plain_result(status)
    
    @filter.command("summary_help")
    async def show_help(self, event: AstrMessageEvent):
        """显示插件帮助信息"""
        help_text = """【群聊消息总结插件】使用帮助

本插件会自动收集群聊消息，在消息数量达到阈值后触发总结。总结结果会保存在服务器本地，并可选择性地发送到群聊中。

触发条件:
- 当消息数量达到设定阈值 (默认300条)
- 且距离上次总结时间超过最小间隔 (默认1小时)

可用命令:
/summary_status - 查看当前群的消息收集状态
/summarize_now - 立即总结当前收集的所有消息 (仅管理员)
/summary [数量] - 立即总结指定数量的最近消息 (仅管理员)
/summary_debug [计数] - 设置当前群消息计数器的值用于调试自动触发功能 (仅管理员)
/get_session_id - 获取当前会话的ID信息，用于配置白名单或自定义目标群
/summary_llm_info - 显示当前LLM提供商配置信息"""

        # 如果API功能已启用，添加API相关命令
        if self.api_service and self.api_service.api_enabled:
            help_text += """
/summary_api_info - 显示API相关信息，包括访问地址和令牌 (仅管理员)"""
            
        help_text += """
/summary_help - 显示本帮助信息

附加说明:
1. 总结结果会保存在服务器本地，路径为 data/group_summaries/
2. 文件命名格式为 "群ID_日期_时间.txt"
3. 总结完成后会重置该群的消息计数器
4. 如果开启了"发送到群聊"选项，总结结果会同时发送到指定的群
5. 缓冲区大小自动设置为总结阈值的1.5倍，确保总能容纳足够的消息
6. 支持自定义LLM配置，可在管理面板指定使用的提供商或自定义API
"""

        try:
            # 获取当前LLM信息的简短版本
            provider = self.context.get_using_provider()
            provider_info = "未使用任何LLM提供商"
            if provider:
                provider_info = f"当前使用的LLM提供商: {provider.__class__.__name__}"
            
            # 检查是否指定了自定义提供商
            llm_config = self.config.get("llm_config", {})
            if llm_config.get("provider_name"):
                provider_info += f"\n指定使用提供商: {llm_config.get('provider_name')}"
            if llm_config.get("use_custom_api", False):
                provider_info += f"\n使用自定义API: {llm_config.get('api_base')}"
                provider_info += f"\n模型: {llm_config.get('model', 'gpt-3.5-turbo')}"
            
            help_text += f"\nLLM信息:\n{provider_info}"
        except Exception as e:
            logger.error(f"获取LLM信息失败: {e}")
            help_text += "\nLLM信息: 获取失败，请使用 /summary_llm_info 命令查看详细信息"
        
        yield event.plain_result(help_text)
    
    @filter.command("summary_debug")
    @filter.permission_type(PermissionType.ADMIN)  # 仅管理员可执行
    async def debug_counter(self, event: AstrMessageEvent):
        """调试用，设置消息计数器"""
        # 获取群ID
        if not event.unified_msg_origin or not ":GroupMessage:" in event.unified_msg_origin:
            yield event.plain_result("该命令仅在群聊中可用")
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
        
        # 解析消息，获取计数参数
        message_content = event.get_message_content()
        parts = message_content.strip().split()
        
        # 提取计数参数
        if len(parts) > 1:
            try:
                count = int(parts[1])
                if count < 0:
                    yield event.plain_result("计数值不能为负数")
                    return
                    
                # 设置计数器
                self.message_counts[group_id] = count
                logger.info(f"已设置群 {group_id} ({group_name}) 的消息计数器为 {count}")
                
                # 显示阈值信息
                summary_threshold = self.config.get("summary_threshold", 300)
                min_interval = self.config.get("min_interval", 3600)
                
                # 检查触发条件
                if count >= summary_threshold:
                    # 检查时间间隔
                    last_summary_time = self.last_summary_times.get(group_id, 0)
                    current_time = time.time()
                    time_diff = current_time - last_summary_time
                    
                    if time_diff >= min_interval:
                        trigger_info = f"满足自动触发条件（计数: {count}>={summary_threshold}, 时间间隔: {int(time_diff)}秒>={min_interval}秒）"
                    else:
                        trigger_info = f"计数条件已满足，但时间间隔未满足（{int(time_diff)}秒<{min_interval}秒）"
                else:
                    trigger_info = f"计数条件未满足（{count}<{summary_threshold}）"
                
                yield event.plain_result(f"已设置群 {group_name} 的消息计数器为 {count}\n\n{trigger_info}")
                
            except ValueError:
                yield event.plain_result("无效的计数值，请输入一个非负整数")
                return
        else:
            # 显示当前计数
            current_count = self.message_counts.get(group_id, 0)
            summary_threshold = self.config.get("summary_threshold", 300)
            yield event.plain_result(f"当前群 {group_name} 的消息计数为: {current_count}/{summary_threshold}")
    
    @filter.command("get_session_id")
    async def get_session_id(self, event: AstrMessageEvent):
        """获取当前会话ID"""
        # 获取会话ID
        session_id = event.unified_msg_origin
        if not session_id:
            platform_name = event.get_platform_name() or "unknown"
            if ":GroupMessage:" in str(event.message_type):
                group_id = event.group_id
                session_id = f"{platform_name}:GroupMessage:{group_id}"
            else:
                session_id = f"{platform_name}:{event.message_type}"
        
        yield event.plain_result(f"当前会话ID: {session_id}")
    
    @filter.command("summary_llm_info")
    async def llm_info(self, event: AstrMessageEvent):
        """显示当前LLM提供商配置信息"""
        # 使用外部函数生成LLM信息
        info = await llm_provider.get_llm_info(self.context, self.config)
        yield event.plain_result(info)
    
    @filter.command("summary_api_info")
    @filter.permission_type(PermissionType.ADMIN)  # 仅管理员可执行
    async def api_info(self, event: AstrMessageEvent):
        """显示API相关信息"""
        if not self.api_service:
            yield event.plain_result("API服务未初始化")
            return
            
        info = self.api_service.get_api_info()
        yield event.plain_result(info) 
