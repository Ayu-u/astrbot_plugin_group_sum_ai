"""
群聊消息总结插件的API服务模块
提供HTTP API服务相关功能
"""

import os
import sys
import asyncio
import traceback
import base64
from io import BytesIO
from multiprocessing import Process
from typing import Dict, Any, Optional

# 确保当前目录在sys.path中以便正确导入本地模块
current_dir = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)
    # 避免循环导入
    from astrbot import logger
    logger.info(f"[ApiService] 已将插件目录添加到sys.path: {current_dir}")

import aiohttp
from astrbot import logger
from astrbot.api.event import MessageChain

class ApiService:
    """API服务类，提供HTTP API功能"""
    
    def __init__(self, context, config: Dict[str, Any]):
        """初始化API服务
        
        Args:
            context: AstrBot上下文对象
            config: 配置信息，包含sending.api部分
        """
        self.context = context
        self.config = config
        self.api_config = config.get("sending", {}).get("api", {})
        
        # API服务状态
        self.api_enabled = self.api_config.get("enabled", False)
        self.api_running = False
        self.api_process = None
        self.api_in_queue = None
    
    async def initialize(self):
        """初始化API服务，启动服务器"""
        if not self.api_enabled:
            logger.info("API服务未启用")
            return
            
        try:
            # 初始化队列
            from multiprocessing import Queue
            self.api_in_queue = Queue()
            
            # 确保API Token
            import secrets
            token = self.api_config.get("token")
            if not token:
                token = secrets.token_urlsafe(32)
                self.api_config["token"] = token
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
            
            # 获取API服务器配置
            host = self.api_config.get("host", "0.0.0.0")
            port = self.api_config.get("port", 9966)
            
            # 启动API服务器进程
            self.api_process = Process(target=self._run_api_server, args=(token, host, port, self.api_in_queue))
            self.api_process.daemon = True
            self.api_process.start()
            self.api_running = True
            
            logger.info(f"群消息总结插件API服务已启动，监听地址: {host}:{port}")
            
            # 启动消息处理任务
            asyncio.create_task(self._process_api_messages())
            
        except Exception as e:
            logger.error(f"启动API服务失败: {e}")
            logger.error(f"错误详情: {traceback.format_exc()}")
            self.api_enabled = False
    
    async def terminate(self):
        """终止API服务"""
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
            logger.info("API服务已终止")
    
    def _run_api_server(self, token, host, port, in_queue):
        """运行API服务器（在单独的进程中）"""
        try:
            from hypercorn.asyncio import serve
            from hypercorn.config import Config
            from quart import Quart, abort, jsonify, request
            import uuid
            
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
    
    def get_api_info(self) -> str:
        """获取API服务信息
        
        Returns:
            str: API信息文本
        """
        if not self.api_enabled:
            return "群消息总结API功能未启用，请在配置中启用"
            
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
        
        return info 
