"""
群聊消息总结插件的LLM提供商相关功能
提供LLM提供商的获取和管理功能
"""

import os
import sys
import traceback
from typing import Optional, Any, Dict

# 确保当前目录在sys.path中以便正确导入本地模块
current_dir = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)
    # 避免循环导入
    from astrbot import logger
    logger.info(f"[LLMProvider] 已将插件目录添加到sys.path: {current_dir}")

from astrbot import logger

async def get_llm_provider(context, config: Dict[str, Any]):
    """根据配置获取LLM提供商
    
    Args:
        context: AstrBot上下文对象
        config: 配置信息，包含llm_config部分
        
    Returns:
        Provider: LLM提供商实例，如果未找到则返回None
    """
    llm_config = config.get("llm_config", {})
    
    # 如果配置了使用自定义API
    if llm_config.get("use_custom_api", False):
        try:
            # 检查必要的配置项
            api_key = llm_config.get("api_key", "")
            if not api_key:
                logger.error("未设置API密钥，无法使用自定义API")
                # 失败时尝试使用指定或默认提供商
            else:
                # 尝试导入相关模块
                try:
                    # OpenAI提供商（或其他兼容OpenAI接口的提供商）
                    from astrbot.provider.openai import OpenAIProvider
                    
                    # 创建自定义提供商实例
                    custom_provider = OpenAIProvider(
                        api_key=api_key,
                        api_base=llm_config.get("api_base", "https://api.openai.com/v1"),
                        model=llm_config.get("model", "gpt-3.5-turbo"),
                        temperature=llm_config.get("temperature", 0.7),
                        max_tokens=llm_config.get("max_tokens", 2000)
                    )
                    logger.info("使用自定义OpenAI兼容API提供商")
                    return custom_provider
                except ImportError:
                    logger.error("无法导入OpenAIProvider，请确保AstrBot版本支持自定义提供商")
                except Exception as e:
                    logger.error(f"创建自定义提供商失败: {e}")
        except Exception as e:
            logger.error(f"使用自定义API失败: {e}")
    
    # 如果配置了使用特定提供商
    provider_name = llm_config.get("provider_name", "")
    if provider_name:
        try:
            # 尝试获取指定名称的提供商
            provider = context.get_provider_by_id(provider_name)
            if provider:
                logger.info(f"使用指定提供商: {provider_name}")
                return provider
            else:
                logger.warning(f"未找到名为 {provider_name} 的提供商，将使用默认提供商")
        except Exception as e:
            logger.error(f"获取指定提供商失败: {e}")
    
    # 使用默认提供商
    provider = context.get_using_provider()
    if provider:
        logger.info("使用AstrBot默认LLM提供商")
        return provider
    else:
        logger.error("未找到任何可用的LLM提供商")
        return None

async def get_llm_info(context, config: Dict[str, Any]) -> str:
    """生成LLM提供商配置信息
    
    Args:
        context: AstrBot上下文对象
        config: 配置信息，包含llm_config部分
    
    Returns:
        str: 格式化的LLM信息文本
    """
    # 获取LLM配置
    llm_config = config.get("llm_config", {})
    
    # 构建基本信息
    info = "【群消息总结插件LLM配置信息】\n\n"
    
    # 检查当前使用的提供商
    provider = context.get_using_provider()
    if provider:
        info += f"AstrBot当前默认提供商: {provider.__class__.__name__}\n"
    else:
        info += "AstrBot当前未设置默认提供商\n"
    
    # 显示插件LLM配置
    info += "\n插件LLM配置:\n"
    
    if llm_config.get("provider_name"):
        info += f"- 指定提供商名称: {llm_config.get('provider_name')}\n"
        
        # 尝试获取该提供商
        try:
            specified_provider = context.get_provider_by_id(llm_config.get('provider_name'))
            if specified_provider:
                info += f"  (已找到该提供商: {specified_provider.__class__.__name__})\n"
            else:
                info += "  (未找到该提供商，将使用默认提供商)\n"
        except Exception:
            info += "  (获取该提供商失败，将使用默认提供商)\n"
    else:
        info += "- 未指定特定提供商，使用AstrBot默认提供商\n"
    
    # 显示自定义API配置
    use_custom_api = llm_config.get("use_custom_api", False)
    info += f"\n是否使用自定义API: {'是' if use_custom_api else '否'}\n"
    
    if use_custom_api:
        api_key = llm_config.get("api_key", "")
        api_base = llm_config.get("api_base", "https://api.openai.com/v1")
        model = llm_config.get("model", "gpt-3.5-turbo")
        temperature = llm_config.get("temperature", 0.7)
        max_tokens = llm_config.get("max_tokens", 2000)
        
        info += "自定义API配置:\n"
        info += f"- API基础URL: {api_base}\n"
        info += f"- API密钥: {'已设置' if api_key else '未设置'}\n"
        info += f"- 模型: {model}\n"
        info += f"- 温度: {temperature}\n"
        info += f"- 最大令牌数: {max_tokens}\n"
        
        # 检查是否存在必要的模块
        try:
            from astrbot.provider.openai import OpenAIProvider
            info += "\n已找到OpenAIProvider模块，可以使用自定义API\n"
        except ImportError:
            info += "\n警告: 未找到OpenAIProvider模块，无法使用自定义API\n"
    
    # 测试当前实际使用的提供商
    info += "\n当前实际使用的提供商:\n"
    try:
        actual_provider = await get_llm_provider(context, config)
        if actual_provider:
            info += f"提供商类型: {actual_provider.__class__.__name__}\n"
            
            # 尝试获取更多信息
            if hasattr(actual_provider, "api_base"):
                info += f"API基础URL: {getattr(actual_provider, 'api_base')}\n"
            if hasattr(actual_provider, "model"):
                info += f"模型: {getattr(actual_provider, 'model')}\n"
        else:
            info += "未找到可用的LLM提供商，总结功能将无法使用\n"
    except Exception as e:
        info += f"获取实际提供商时出错: {str(e)}\n"
    
    # 提供配置建议
    info += "\n配置提示:\n"
    info += "- 您可以在AstrBot管理面板中修改群消息总结插件的LLM配置\n"
    info += "- 如果希望使用AstrBot默认提供商，请保持provider_name为空\n"
    info += "- 如果希望使用其他已加载的提供商，请设置provider_name为对应ID\n"
    info += "- 如果希望使用自定义API，请开启use_custom_api并配置API密钥等参数\n"
    
    return info 
