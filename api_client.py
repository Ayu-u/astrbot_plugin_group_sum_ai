import requests
import json
import base64
import argparse
import sys
import os

# 默认API基础URL - 请根据实际部署环境修改
DEFAULT_API_URL = "http://localhost:9966"

def make_api_request(method, endpoint, api_token=None, payload=None, api_url=DEFAULT_API_URL):
    """
    发送API请求的通用函数
    
    参数:
        method (str): HTTP方法，例如'GET'或'POST'
        endpoint (str): API端点，例如'/send'或'/health'
        api_token (str, optional): API令牌，需要授权的请求必填
        payload (dict, optional): 请求体，POST请求必填
        api_url (str): API服务的基础URL
    
    返回:
        dict: API响应或错误信息
    
    错误码:
        400: 请求格式错误或缺少必要字段
        403: API 令牌无效
    """
    url = f"{api_url}{endpoint}"
    
    headers = {}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    
    if payload:
        headers["Content-Type"] = "application/json"
    
    try:
        if method.upper() == 'GET':
            response = requests.get(url, headers=headers)
        elif method.upper() == 'POST':
            response = requests.post(url, headers=headers, json=payload)
        else:
            return {"error": f"不支持的HTTP方法: {method}"}
        
        response.raise_for_status()  # 如果状态码不是200，抛出异常
        return response.json()
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code
        if status_code == 400:
            print("错误: 请求格式错误或缺少必要字段")
        elif status_code == 403:
            print("错误: API 令牌无效")
        else:
            print(f"HTTP错误: {status_code}")
        
        try:
            return e.response.json()
        except:
            return {"error": str(e), "status_code": status_code}
    except requests.exceptions.ConnectionError as e:
        print(f"连接错误: 无法连接到API服务器 {url}")
        return {"error": f"连接错误: {str(e)}"}
    except requests.exceptions.Timeout as e:
        print(f"超时错误: API请求超时")
        return {"error": f"超时错误: {str(e)}"}
    except requests.exceptions.RequestException as e:
        print(f"请求错误: {e}")
        return {"error": str(e)}
    except Exception as e:
        print(f"未知错误: {e}")
        return {"error": str(e)}

def check_health(api_url=DEFAULT_API_URL):
    """
    检查API服务健康状态
    
    参数:
        api_url (str): API服务的基础URL
    
    返回:
        dict: 健康状态响应，例如 {"status": "ok", "queue_size": 1}
    """
    return make_api_request('GET', '/health', api_url=api_url)

def send_text_message(api_token, message_content, umo, callback_url=None, api_url=DEFAULT_API_URL):
    """
    发送文本消息到指定目标
    
    参数:
        api_token (str): API令牌
        message_content (str): 要发送的文本消息内容
        umo (str): 目标会话标识
        callback_url (str, optional): 处理结果回调URL
        api_url (str): API服务的基础URL
    
    返回:
        dict: API响应，成功时返回 {"status": "queued", "message_id": "xxx", "queue_size": 1}
    
    错误码:
        400: 请求格式错误或缺少必要字段
        403: API 令牌无效
        
    回调通知:
        如果提供了callback_url，API服务会在消息处理完成后发送POST请求：
        成功: {"message_id": "xxx", "success": true}
        失败: {"message_id": "xxx", "success": false, "error": "错误信息"}
    """
    payload = {
        "content": message_content,
        "umo": umo,
        "type": "text"
    }
    
    if callback_url:
        payload["callback_url"] = callback_url
    
    return make_api_request('POST', '/send', api_token, payload, api_url)

def send_image_message(api_token, image_path, umo, callback_url=None, api_url=DEFAULT_API_URL):
    """
    发送图片消息到指定目标
    
    参数:
        api_token (str): API令牌
        image_path (str): 图片文件路径
        umo (str): 目标会话标识
        callback_url (str, optional): 处理结果回调URL
        api_url (str): API服务的基础URL
    
    返回:
        dict: API响应，成功时返回 {"status": "queued", "message_id": "xxx", "queue_size": 1}
    
    错误码:
        400: 请求格式错误或缺少必要字段
        403: API 令牌无效
        
    回调通知:
        如果提供了callback_url，API服务会在消息处理完成后发送POST请求：
        成功: {"message_id": "xxx", "success": true}
        失败: {"message_id": "xxx", "success": false, "error": "错误信息"}
    """
    # 读取图片并转换为base64
    try:
        with open(image_path, "rb") as image_file:
            encoded_image = base64.b64encode(image_file.read()).decode('utf-8')
    except FileNotFoundError:
        print(f"错误: 找不到图片文件 {image_path}")
        return {"error": f"找不到图片文件: {image_path}"}
    except PermissionError:
        print(f"错误: 没有权限读取图片文件 {image_path}")
        return {"error": f"没有权限读取图片文件: {image_path}"}
    except Exception as e:
        print(f"读取图片失败: {e}")
        return {"error": f"读取图片失败: {e}"}
    
    payload = {
        "content": encoded_image,
        "umo": umo,
        "type": "image"
    }
    
    if callback_url:
        payload["callback_url"] = callback_url
    
    return make_api_request('POST', '/send', api_token, payload, api_url)

def main():
    parser = argparse.ArgumentParser(description="群聊消息总结插件HTTP API客户端")
    parser.add_argument("--token", help="API令牌")
    parser.add_argument("--umo", help="目标会话标识，可使用/get_session_id命令获取")
    parser.add_argument("--type", choices=["text", "image", "health"], default="text", 
                        help="消息类型: text, image 或 health (健康检查)")
    parser.add_argument("--content", help="文本消息内容")
    parser.add_argument("--image", help="图片文件路径")
    parser.add_argument("--callback", help="处理结果回调URL")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, 
                        help=f"API服务的基础URL，默认为 {DEFAULT_API_URL}")
    
    args = parser.parse_args()
    
    # 健康检查不需要token
    if args.type == "health":
        result = check_health(args.api_url)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if result.get("status") == "ok":
            print("服务状态: 正常")
            print(f"队列大小: {result.get('queue_size')}")
        else:
            print("服务状态: 异常")
            print(f"错误信息: {result.get('error')}")
        return
    
    # 非健康检查需要token
    if not args.token:
        print("错误: 非健康检查操作需要提供API令牌")
        print("请使用 --token 参数提供API令牌")
        sys.exit(1)
    
    # 需要目标会话ID
    if not args.umo:
        print("错误: 请提供目标会话ID (--umo 参数)")
        print("可通过插件的 /get_session_id 命令获取")
        sys.exit(1)
    
    if args.type == "text" and not args.content:
        print("错误: 发送文本消息时，必须提供--content参数")
        sys.exit(1)
    
    if args.type == "image" and not args.image:
        print("错误: 发送图片消息时，必须提供--image参数")
        sys.exit(1)
    
    try:
        if args.type == "text":
            result = send_text_message(args.token, args.content, args.umo, args.callback, args.api_url)
        else:
            result = send_image_message(args.token, args.image, args.umo, args.callback, args.api_url)
        
        # 检查是否有错误
        if "error" in result:
            print(f"发送失败: {result.get('error')}")
            sys.exit(1)
        
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"消息ID: {result.get('message_id')}")
        print(f"队列状态: {result.get('status')}")
        print(f"队列大小: {result.get('queue_size')}")
        
    except Exception as e:
        print(f"发送消息失败: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main() 
