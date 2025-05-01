"""
群聊消息总结插件主模块
自动收集群聊消息并在达到一定条件时生成总结
"""

import os
import sys
from typing import Dict, List, Any, Optional

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot import logger

# 确保当前目录在sys.path中以便正确导入本地模块
current_dir = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)
    logger.info(f"已将插件目录添加到sys.path: {current_dir}")
else:
    logger.info(f"插件目录已在sys.path中: {current_dir}")

# 记录Python搜索路径，帮助诊断问题
logger.debug(f"Python模块搜索路径: {str(sys.path)}")

# 导入本地模块
try:
    import llm_provider
    import api_service
    import state_manager
    import message_processor
    import commands
    logger.info("成功导入所有本地模块")
except ImportError as e:
    logger.error(f"导入本地模块失败: {e}")
    # 尝试单独导入每个模块，以便找出具体哪个模块导入失败
    modules_to_import = ["llm_provider", "api_service", "state_manager", "message_processor", "commands"]
    for module_name in modules_to_import:
        try:
            # 使用importlib动态导入
            import importlib
            globals()[module_name] = importlib.import_module(module_name)
            logger.info(f"成功导入模块: {module_name}")
        except ImportError as e2:
            logger.error(f"导入模块 {module_name} 失败: {e2}")
    
    # 检查是否有必需模块缺失
    missing_modules = [m for m in modules_to_import if m not in globals()]
    if missing_modules:
        error_msg = f"无法导入以下关键模块: {', '.join(missing_modules)}，插件无法启动"
        logger.error(error_msg)
        raise ImportError(error_msg)

@register("group-summarizer", "Ayu-u", "自动总结群聊消息", "1.0.0")
class GroupSummarizer(Star):
    """群聊消息总结插件
    
    自动收集群聊消息，并在消息数量达到阈值时触发总结。
    总结结果会保存在本地，并可选择性地发送到指定的群聊。
    """
    
    def __init__(self, context: Context, config=None):
        """初始化群聊消息总结插件
        
        Args:
            context (Context): AstrBot上下文对象
            config: 配置信息
        """
        super().__init__(context)
        # 加载配置
        self.config = config or {}
        
        # 输出LLM配置信息
        self._log_llm_config()
        
        # 创建模块实例
        self.state_manager = state_manager.StateManager(self.config)
        self.api_service = api_service.ApiService(context, self.config)
        self.message_processor = message_processor.MessageProcessor(context, self.config, self.state_manager)
        self.command_handler = commands.CommandHandler(context, self.config, self.state_manager, self.message_processor, self.api_service)
        
        # 设置引用，使命令处理器可以使用消息处理器
        logger.info("群聊消息总结插件已初始化")
    
    def _log_llm_config(self):
        """记录LLM配置信息"""
        llm_config = self.config.get("llm_config", {})
        logger.info("LLM配置信息:")
        
        # 检查是否指定了提供商
        provider_name = llm_config.get("provider_name")
        if provider_name:
            logger.info(f"- 指定使用提供商: {provider_name}")
        else:
            logger.info("- 使用AstrBot默认提供商")
        
        # 检查是否使用自定义API
        use_custom_api = llm_config.get("use_custom_api", False)
        logger.info(f"- 使用自定义API: {'是' if use_custom_api else '否'}")
        
        if use_custom_api:
            # 记录自定义API配置
            api_base = llm_config.get("api_base", "https://api.openai.com/v1")
            model = llm_config.get("model", "gpt-3.5-turbo")
            temperature = llm_config.get("temperature", 0.7)
            max_tokens = llm_config.get("max_tokens", 2000)
            
            logger.info(f"  - API基础URL: {api_base}")
            logger.info(f"  - API密钥: {'已设置' if llm_config.get('api_key') else '未设置'}")
            logger.info(f"  - 模型: {model}")
            logger.info(f"  - 温度: {temperature}")
            logger.info(f"  - 最大令牌数: {max_tokens}")
        
        # 记录总结阈值和间隔
        logger.info(f"消息总结阈值: {self.config.get('summary_threshold', 300)}条")
        logger.info(f"最小总结间隔: {self.config.get('min_interval', 3600)}秒")
        
        # 记录发送配置
        sending_config = self.config.get("sending", {})
        send_to_chat = sending_config.get("send_to_chat", False)
        logger.info(f"总结发送到群聊: {'是' if send_to_chat else '否'}")
        
        if send_to_chat:
            target_config = sending_config.get("target", {})
            target_type = target_config.get("type", "current")
            if target_type == "current":
                logger.info("- 发送目标: 当前群聊")
            elif target_type == "custom":
                custom_id = target_config.get("custom_group_id", "")
                logger.info(f"- 发送目标: 自定义群聊 ({custom_id})")
            elif target_type == "api":
                logger.info("- 发送目标: 通过API发送")
    
    async def initialize(self):
        """插件初始化"""
        # 加载状态
        self.state_manager.load_state()
        
        # 启动状态管理任务
        await self.state_manager.start_tasks()
        
        # 初始化API服务
        await self.api_service.initialize()
        
        logger.info("群聊消息总结插件初始化完成")
        
    async def terminate(self):
        """插件终止时的清理工作"""
        try:
            # 停止状态管理任务
            await self.state_manager.stop_tasks()
            
            # 保存状态
            self.state_manager.save_state()
            
            # 终止API服务
            await self.api_service.terminate()
                
        except Exception as e:
            logger.error(f"群消息总结插件终止时发生错误: {e}")
            import traceback
            logger.error(f"错误详情: {traceback.format_exc()}")
        
        logger.info("群消息总结插件已终止")
    
    # 转发事件到处理模块的装饰器方法
    async def on_group_message(self, event: AstrMessageEvent):
        """处理群消息事件"""
        return await self.message_processor.on_group_message(event)
    
    # 转发命令到处理模块的装饰器方法
    async def force_summarize(self, event: AstrMessageEvent):
        """立即总结当前群聊的所有消息"""
        async for result in self.command_handler.force_summarize(event):
            yield result
    
    async def summarize_with_count(self, event: AstrMessageEvent):
        """总结指定数量的最近消息"""
        async for result in self.command_handler.summarize_with_count(event):
            yield result
    
    async def check_status(self, event: AstrMessageEvent):
        """查看当前消息收集状态"""
        async for result in self.command_handler.check_status(event):
            yield result
    
    async def show_help(self, event: AstrMessageEvent):
        """显示插件帮助信息"""
        async for result in self.command_handler.show_help(event):
            yield result
    
    async def debug_counter(self, event: AstrMessageEvent):
        """调试用，设置消息计数器"""
        async for result in self.command_handler.debug_counter(event):
            yield result
    
    async def get_session_id(self, event: AstrMessageEvent):
        """获取当前会话ID"""
        async for result in self.command_handler.get_session_id(event):
            yield result
    
    async def llm_info(self, event: AstrMessageEvent):
        """显示当前LLM提供商配置信息"""
        async for result in self.command_handler.llm_info(event):
            yield result
    
    async def api_info(self, event: AstrMessageEvent):
        """显示API相关信息"""
        async for result in self.command_handler.api_info(event):
            yield result 
