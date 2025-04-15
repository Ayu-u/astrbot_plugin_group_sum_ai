import os
import json
import time
import yaml
import glob
import asyncio
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
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
    logger.info("成功从提示词文件加载提示词")
except ImportError:
    logger.warning("未找到提示词文件或提示词文件格式错误，将使用默认提示词")
    # 默认提示词
    SYSTEM_PROMPT = "你是一个专业的群聊分析师，善于提取群聊中的关键信息和主要话题。"
    SUMMARY_PROMPT_TEMPLATE = "请总结以下微信群聊（{group_name}）最近的{msg_count}条消息内容，提取主要讨论话题和重点信息。\n当前时间: {current_time}\n\n{messages}"
    MESSAGE_FORMAT = "[{time_str}] {sender_name}: {content}"
    FEW_MESSAGES_PROMPT = ""

@register("group-summarizer", "zy", "自动总结群聊消息", "1.0.0")
class GroupSummarizer(Star):
    """
    群消息自动总结插件
    
    自动收集群聊消息，定期进行总结，总结结果可保存在本地或发送到群聊
    """
    
    def __init__(self, context: Context):
        super().__init__(context)
        # 初始化配置
        self.config = self._load_config()
        
        # 创建存储目录
        self.summary_dir = "data/group_summaries"
        os.makedirs(self.summary_dir, exist_ok=True)
        
        # 初始化消息存储
        self.message_counters = {}  # 群ID -> 消息计数
        self.message_buffers = {}   # 群ID -> 消息列表
        self.last_summary_time = {} # 群ID -> 上次总结时间
        
        # 从文件加载状态
        self._load_state()
        
        # 定时保存状态的任务
        self.save_task = asyncio.create_task(self._periodic_save())
        
        # 如果启用了数据清理，创建定时清理任务
        if self.config.get("data_cleaning", {}).get("enabled", False):
            self.clean_task = asyncio.create_task(self._periodic_clean())
        
        logger.info("群消息总结插件已初始化")
    
    def _load_config(self) -> dict:
        """加载插件配置"""
        default_config = {
            "summary_threshold": 300,  # 触发总结的消息数量阈值
            "max_buffer_size": 500,    # 每个群消息缓冲区最大容量
            "min_interval": 3600,      # 两次总结的最小时间间隔(秒)
            "summary_system_prompt": SYSTEM_PROMPT,  # 从提示词文件中获取系统提示词
            "use_external_prompts": True,  # 是否使用外部提示词
            "data_cleaning": {
                "enabled": True,
                "check_interval": 86400,  # 24小时
                "max_files_per_group": 30,
                "max_retention_days": 30
            },
            "sending": {
                "send_to_chat": False,
                "target": {
                    "type": "current",
                    "custom_group_id": "",
                    "add_group_name_prefix": True
                }
            }
        }
        
        # 首先尝试从本地YAML文件加载配置
        yaml_config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
        yaml_config = {}
        
        if os.path.exists(yaml_config_path):
            try:
                with open(yaml_config_path, "r", encoding="utf-8") as f:
                    yaml_config = yaml.safe_load(f)
                logger.info("从本地YAML文件加载配置成功")
            except Exception as e:
                logger.error(f"加载本地YAML配置失败: {e}")
        
        try:
            # 从AstrBot全局配置中获取插件配置
            plugin_config = self.context.get_config().get("group_summarizer", {})
            
            # 合并配置，优先级: 默认配置 < AstrBot全局配置 < 本地YAML配置
            config = default_config.copy()
            if plugin_config:
                config.update(plugin_config)
                logger.info("成功从AstrBot全局配置加载插件配置")
            
            if yaml_config:
                config.update(yaml_config)
            
            # 记录关键配置项
            logger.info(f"群聊总结插件配置加载完成: summary_threshold={config['summary_threshold']}, "
                        f"min_interval={config['min_interval']}秒, "
                        f"send_to_chat={config.get('sending', {}).get('send_to_chat', False)}")
                
            return config
        except Exception as e:
            logger.error(f"加载插件配置失败: {e}，使用默认配置")
            # 如果全局配置加载失败，但YAML配置存在，则使用YAML配置
            if yaml_config:
                merged_config = default_config.copy()
                merged_config.update(yaml_config)
                return merged_config
            return default_config
    
    def _load_state(self):
        """从文件加载状态"""
        state_file = f"{self.summary_dir}/state.json"
        try:
            if os.path.exists(state_file):
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                    self.message_counters = state.get("counters", {})
                    self.last_summary_time = state.get("last_summary_time", {})
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
        
        # 初始化群消息缓存
        if group_id not in self.message_counters:
            self.message_counters[group_id] = 0
            self.message_buffers[group_id] = []
            self.last_summary_time[group_id] = 0
            logger.info(f"初始化群 {group_id} 的消息缓存")
        
        # 收集消息
        sender_name = event.get_sender_name() or "未知用户"
        sender_id = event.get_sender_id()
        content = event.get_message_str()
        timestamp = int(time.time())
        
        # 跳过空消息
        if not content.strip():
            return
        
        # 消息缓存，保持在最大容量以内
        self.message_buffers.setdefault(group_id, []).append({
            "sender_name": sender_name,
            "sender_id": sender_id,
            "content": content,
            "timestamp": timestamp
        })
        
        # 限制缓冲区大小
        max_size = self.config["max_buffer_size"]
        if len(self.message_buffers[group_id]) > max_size:
            self.message_buffers[group_id] = self.message_buffers[group_id][-max_size:]
        
        # 增加计数
        self.message_counters[group_id] += 1
        
        # 每收到10条消息记录一次日志
        if self.message_counters[group_id] % 10 == 0:
            logger.info(f"群 {group_id} 当前消息计数: {self.message_counters[group_id]}/{self.config['summary_threshold']}")
        
        # 检查是否达到总结条件
        threshold = self.config["summary_threshold"]
        min_interval = self.config["min_interval"]
        current_time = int(time.time())
        last_time = self.last_summary_time.get(group_id, 0)
        time_diff = current_time - last_time
        
        # 记录触发条件的判断过程
        if self.message_counters[group_id] >= threshold:
            logger.info(f"群 {group_id} 消息数量已达到阈值: {self.message_counters[group_id]}/{threshold}")
            if time_diff >= min_interval or last_time == 0:
                logger.info(f"群 {group_id} 时间间隔满足条件: 距上次总结 {time_diff} 秒 >= {min_interval} 秒或从未总结")
                logger.info(f"群 {group_id} 触发自动总结")
                
                # 符合总结条件，进行总结
                await self.summarize_messages(group_id, event)
                # 重置计数
                self.message_counters[group_id] = 0
                self.last_summary_time[group_id] = current_time
                # 保存状态
                self._save_state()
            else:
                logger.info(f"群 {group_id} 时间间隔不满足条件: 距上次总结 {time_diff} 秒 < {min_interval} 秒")
    
    async def summarize_messages(self, group_id: str, event: AstrMessageEvent) -> Optional[str]:
        """总结群消息
        
        Args:
            group_id: 群ID
            event: 消息事件
            
        Returns:
            str: 总结内容，如果总结失败则返回None
        """
        messages = self.message_buffers.get(group_id, [])
        if not messages:
            logger.info(f"群 {group_id} 没有可总结的消息")
            return None
        
        # 获取群信息
        group = await event.get_group()
        group_name = group.group_name if group else "未知群组"
        
        logger.info(f"开始总结群 {group_id} ({group_name}) 的 {len(messages)} 条消息")
        
        # 准备提示词
        current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        msg_count = len(messages)
        
        # 根据配置决定使用内部还是外部提示词
        if self.config.get("use_external_prompts", True):
            # 构建消息文本
            message_texts = []
            # 最多使用最近200条消息，防止token过多
            recent_messages = messages[-200:]
            for msg in recent_messages:
                time_str = time.strftime("%H:%M", time.localtime(msg["timestamp"]))
                message_texts.append(MESSAGE_FORMAT.format(
                    time_str=time_str,
                    sender_name=msg['sender_name'],
                    content=msg['content']
                ))
            
            # 构建提示词
            messages_str = "\n".join(message_texts)
            if msg_count < 10 and FEW_MESSAGES_PROMPT:  # 消息数量很少时添加额外提示
                messages_str += f"\n\n{FEW_MESSAGES_PROMPT}"
                
            prompt = SUMMARY_PROMPT_TEMPLATE.format(
                group_name=group_name,
                msg_count=msg_count,
                current_time=current_time,
                messages=messages_str
            )
            
            system_prompt = self.config.get("summary_system_prompt", SYSTEM_PROMPT)
        else:
            # 使用简单提示词
            prompt = (
                f"请总结以下微信群聊（{group_name}）最近的{msg_count}条消息内容，"
                f"提取主要讨论话题和重点信息。\n"
                f"当前时间: {current_time}\n\n"
            )
            
            # 最多使用最近200条消息，防止token过多
            recent_messages = messages[-200:]
            for msg in recent_messages:
                time_str = time.strftime("%H:%M", time.localtime(msg["timestamp"]))
                prompt += f"[{time_str}] {msg['sender_name']}: {msg['content']}\n"
                
            system_prompt = self.config.get("summary_system_prompt", "你是一个专业的群聊分析师，善于提取群聊中的关键信息和主要话题。")
        
        # 调用LLM进行总结
        try:
            logger.info(f"调用LLM为群 {group_id} ({group_name}) 生成总结")
            provider = self.context.get_using_provider()
            if not provider:
                logger.error("未找到默认LLM提供商")
                return None
            
            # 调用大模型生成总结，使用简化的接口调用方式
            try:
                # 尝试使用完整的参数列表
                response = await provider.text_chat(
                    prompt=prompt,
                    session_id=None,  # 不关联到特定会话
                    contexts=[{"role": "system", "content": system_prompt}],
                    image_urls=[]
                )
            except TypeError:
                # 如果出现参数错误，尝试使用简化版本
                logger.warning("使用简化的LLM接口调用")
                response = await provider.text_chat(
                    prompt=prompt,
                    contexts=[{"role": "system", "content": system_prompt}]
                )
            
            summary = None
            if hasattr(response, 'role') and response.role == "assistant":
                summary = response.completion_text
            elif hasattr(response, 'content'):
                summary = response.content
            elif isinstance(response, str):
                summary = response
            
            if summary:
                # 保存总结结果到文件
                timestamp = int(time.time())
                date_str = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(timestamp))
                filename = f"{self.summary_dir}/{group_id}_{date_str}.txt"
                
                # 构建保存内容
                content = f"群组: {group_name} ({group_id})\n"
                content += f"时间: {current_time}\n"
                content += f"消息数: {len(messages)}\n"
                content += f"总结内容:\n\n{summary}\n"
                
                # 保存到文件
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(content)
                
                logger.info(f"群 {group_id} ({group_name}) 的消息总结已保存到: {filename}")
                
                # 检查是否需要发送到群聊
                sending_config = self.config.get("sending", {})
                if sending_config.get("send_to_chat", False):
                    # 使用async for迭代异步生成器
                    await self._send_summary_to_chat(group_id, group_name, summary, event)
                
                return summary
            else:
                logger.error(f"群 {group_id} 的消息总结失败: 未获得有效总结内容")
                return None
            
        except Exception as e:
            logger.error(f"总结群 {group_id} 消息失败: {e}")
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
            target_config = self.config.get("sending", {}).get("target", {})
            target_type = target_config.get("type", "current")
            
            # 确定发送目标
            if target_type == "current":
                # 发送到当前群聊
                message = f"【群聊消息总结】\n\n{summary}"
                message_chain = MessageChain().message(message)
                unified_msg_origin = event.unified_msg_origin
                await self.context.send_message(unified_msg_origin, message_chain)
                logger.info(f"已将消息总结发送到当前群 {source_group_id}")
            elif target_type == "custom":
                # 发送到指定群聊
                custom_group_id = target_config.get("custom_group_id", "")
                if not custom_group_id:
                    logger.error("未配置自定义目标群ID，无法发送总结")
                    return
                
                # 构建消息内容
                if target_config.get("add_group_name_prefix", True):
                    message = f"【{source_group_name} 群聊消息总结】\n\n{summary}"
                else:
                    message = f"【群聊消息总结】\n\n{summary}"
                
                message_chain = MessageChain().message(message)
                
                # 获取指定群聊的会话信息，然后发送消息
                try:
                    # 遍历所有平台实例
                    success = False
                    for platform in self.context.platform_manager.get_insts():
                        # 获取平台名称
                        platform_name = platform.meta().name
                        
                        # 尝试查找目标群并发送消息
                        try:
                            # 处理不同平台可能需要不同格式的群ID
                            target_id = custom_group_id
                            
                            # 对于部分平台，可能需要使用完整会话ID
                            if platform_name == "gewechat":
                                # 如果ID不是完整格式，则构造完整会话ID
                                if ":" not in custom_group_id:
                                    target_id = f"{platform_name}:GroupMessage:{custom_group_id}"
                            
                            logger.info(f"尝试通过平台 {platform_name} 发送消息到群 {target_id}")
                            await self.context.send_message(target_id, message_chain)
                            logger.info(f"已将消息总结从 {source_group_id} 发送到自定义群 {target_id}")
                            success = True
                            break
                        except Exception as e:
                            # 这个平台可能没有目标群
                            logger.debug(f"通过平台 {platform_name} 发送消息失败: {e}")
                    
                    if not success:
                        # 如果所有平台都失败
                        logger.error(f"无法找到ID为 {custom_group_id} 的群，无法发送总结")
                except Exception as e:
                    logger.error(f"发送总结到自定义群失败: {e}")
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
            yield event.plain_result("此命令只能在群聊中使用")
            return
        
        if group_id not in self.message_buffers or not self.message_buffers[group_id]:
            yield event.plain_result("当前群没有收集到消息，无法总结")
            return
        
        yield event.plain_result(f"正在总结当前群内 {len(self.message_buffers[group_id])} 条消息...")
        summary = await self.summarize_messages(group_id, event)
        
        # 重置计数
        self.message_counters[group_id] = 0
        self.last_summary_time[group_id] = int(time.time())
        self._save_state()
        
        if not summary:
            yield event.plain_result("总结失败，请查看日志获取详细信息")
        else:
            yield event.plain_result("总结完成，已保存到文件")
    
    @filter.command("summary")
    @filter.permission_type(PermissionType.ADMIN)  # 仅管理员可执行
    async def summarize_with_count(self, event: AstrMessageEvent):
        """手动触发当前群的消息总结，可以指定消息数量"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("此命令只能在群聊中使用")
            return
        
        if group_id not in self.message_buffers or not self.message_buffers[group_id]:
            yield event.plain_result("当前群没有收集到消息，无法总结")
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
        
        # 存储原始消息缓冲区并替换为要总结的部分
        original_buffer = self.message_buffers.get(group_id, [])
        self.message_buffers[group_id] = messages
        
        # 进行总结
        summary = await self.summarize_messages(group_id, event)
        
        # 恢复原始消息缓冲区
        self.message_buffers[group_id] = original_buffer
        
        # 不重置计数器，只保存当前时间
        self.last_summary_time[group_id] = int(time.time())
        self._save_state()
        
        if not summary:
            yield event.plain_result("总结失败，请查看日志获取详细信息")
        else:
            yield event.plain_result("总结完成，已保存到文件")
    
    @filter.command("summary_status")
    async def check_status(self, event: AstrMessageEvent):
        """查看当前群的消息收集状态"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("此命令只能在群聊中使用")
            return
        
        count = self.message_counters.get(group_id, 0)
        threshold = self.config["summary_threshold"]
        last_time = self.last_summary_time.get(group_id, 0)
        
        if last_time > 0:
            last_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_time))
        else:
            last_time_str = "从未总结"
            
        status = f"当前群消息总结状态:\n已收集消息数: {count}/{threshold}\n上次总结时间: {last_time_str}"
        
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
/get_session_id - 获取当前会话的ID信息，用于配置白名单或自定义目标群
/summary_help - 显示本帮助信息

自动总结会在消息达到阈值({threshold}条)且满足时间间隔({min_interval}秒)后触发。
根据配置，总结结果会保存在本地文件中，也可能会发送到群聊。""".format(
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
                
                yield event.plain_result(f"已将群 {group_id} 的消息计数从 {old_count} 设置为 {count}")
                
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
    
    async def terminate(self):
        """插件终止时的清理工作"""
        try:
            self._save_state()
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
        except Exception as e:
            logger.error(f"群消息总结插件终止时发生错误: {e}")
        
        logger.info("群消息总结插件已终止") 
