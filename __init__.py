"""
群聊消息总结插件包
将当前目录标记为Python包以增强导入可靠性
"""

import os
import sys

# 确保当前目录在Python模块搜索路径中
current_dir = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

# 列出当前包中的所有模块
__all__ = [
    'main',
    'llm_provider',
    'api_service',
    'state_manager',
    'message_processor',
    'commands',
    'prompts'
] 
