#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
群聊消息总结插件HTTP API客户端示例

演示如何使用api_client.py发送消息和检查API健康状态
"""

import os
import sys
import json
from api_client import (
    check_health,
    send_text_message,
    send_image_message,
    DEFAULT_API_URL
)

# 这里配置你的API令牌和目标会话ID
# 实际使用时应从环境变量或配置文件获取
API_TOKEN = os.getenv("GROUP_SUM_API_TOKEN", "请替换为你的API令牌")
TARGET_UMO = os.getenv("GROUP_SUM_TARGET_UMO", "请替换为目标会话ID")

# 自定义API URL（可选）
API_URL = os.getenv("GROUP_SUM_API_URL", DEFAULT_API_URL)

def print_result(result, message_type=""):
    """美化输出结果"""
    print("=" * 50)
    print(f"API请求结果 ({message_type}):")
    print("=" * 50)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("=" * 50)
    print()

def example_health_check():
    """健康检查示例"""
    print("\n检查API服务健康状态...")
    result = check_health(api_url=API_URL)
    print_result(result, "健康检查")
    
    if result.get("status") == "ok":
        print("✅ API服务运行正常")
        print(f"当前队列大小: {result.get('queue_size', 0)}")
    else:
        print("❌ API服务异常")
        print(f"错误信息: {result.get('error', '未知错误')}")
    
    return result.get("status") == "ok"

def example_send_text():
    """发送文本消息示例"""
    print("\n发送文本消息示例...")
    
    # 验证API令牌和目标会话ID是否已配置
    if API_TOKEN == "请替换为你的API令牌" or TARGET_UMO == "请替换为目标会话ID":
        print("⚠️ 请先配置API_TOKEN和TARGET_UMO变量")
        return False
    
    message = "这是一条通过API发送的测试消息，来自群聊消息总结插件HTTP API客户端"
    result = send_text_message(
        API_TOKEN,
        message,
        TARGET_UMO,
        api_url=API_URL
    )
    
    print_result(result, "文本消息")
    
    if "error" in result:
        print(f"❌ 发送失败: {result.get('error')}")
        return False
    
    print(f"✅ 消息已入队")
    print(f"消息ID: {result.get('message_id')}")
    print(f"队列状态: {result.get('status')}")
    print(f"队列大小: {result.get('queue_size')}")
    
    return True

def example_send_image():
    """发送图片消息示例"""
    print("\n发送图片消息示例...")
    
    # 验证API令牌和目标会话ID是否已配置
    if API_TOKEN == "请替换为你的API令牌" or TARGET_UMO == "请替换为目标会话ID":
        print("⚠️ 请先配置API_TOKEN和TARGET_UMO变量")
        return False
    
    # 指定要发送的图片路径
    image_path = "example_image.png"
    
    # 检查图片是否存在
    if not os.path.exists(image_path):
        print(f"❌ 图片文件不存在: {image_path}")
        print("请确保example_image.png文件存在于当前目录")
        return False
    
    result = send_image_message(
        API_TOKEN,
        image_path,
        TARGET_UMO,
        api_url=API_URL
    )
    
    print_result(result, "图片消息")
    
    if "error" in result:
        print(f"❌ 发送失败: {result.get('error')}")
        return False
    
    print(f"✅ 图片消息已入队")
    print(f"消息ID: {result.get('message_id')}")
    print(f"队列状态: {result.get('status')}")
    print(f"队列大小: {result.get('queue_size')}")
    
    return True

def main():
    """主函数：运行所有示例"""
    print("群聊消息总结插件HTTP API客户端示例")
    print("===================================")
    print(f"API服务地址: {API_URL}")
    print(f"API令牌: {API_TOKEN[:3] + '***' if len(API_TOKEN) > 6 else '未设置'}")
    print(f"目标会话ID: {TARGET_UMO[:5] + '***' if len(TARGET_UMO) > 8 else '未设置'}")
    print("===================================")
    
    # 运行健康检查示例
    if not example_health_check():
        print("由于API服务异常，跳过消息发送测试")
        return
    
    # 运行文本消息发送示例
    example_send_text()
    
    # 运行图片消息发送示例
    example_send_image()

if __name__ == "__main__":
    main() 
