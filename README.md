# 群聊消息自动总结插件 (group-summarizer)

## 插件功能

自动收集群聊消息，每300条（可配置）进行一次总结，总结结果保存在本地而不发送到群聊。

## 错误修复记录

### 2023-05-29

1. **修复群ID格式问题**:
   - 问题: 无法发送总结到自定义群聊，出现错误 `无法找到ID为 gewechat:GroupMessage:19235404447@chatroom 的群，无法发送总结`
   - 原因: 不同平台可能需要不同格式的群ID，特别是gewechat平台可能需要完整的会话ID格式
   - 解决方案: 
     - 根据平台类型动态构造正确格式的目标群ID
     - 对gewechat平台，如果提供的是纯群ID，自动添加平台前缀构造完整会话ID

2. **修复异步生成器错误**:
   - 问题: 总结发送到群聊时出现错误 `TypeError: object async_generator can't be used in 'await' expression` 和 `TypeError: object MessageEventResult can't be used in 'await' expression`
   - 原因: 将异步生成器方法错误地改为使用`await`而不是`yield`，而AstrBot的事件处理设计是基于异步生成器的
   - 解决方案: 
     - 恢复使用`yield event.plain_result()`而不是`await event.plain_result()`
     - 在调用异步生成器方法时使用`async for`循环而不是`await`
     - 示例: `async for _ in self._send_summary_to_chat(...): pass`

### 2023-05-28

1. **增加灵活配置支持**:
   - 功能：添加从外部YAML文件加载配置的支持
   - 功能：添加从专用Python文件加载提示词的支持
   - 解决方案：添加`config.yaml`和`prompts.py`文件，并修改配置加载逻辑

2. **修复配置加载逻辑**:
   - 问题: 插件配置加载失败，报错 `'Context' object has no attribute 'star_manager'`
   - 原因: 尝试使用不存在的`star_manager`属性获取插件配置
   - 解决方案: 使用标准的`self.context.get_config().get("plugin_name", {})`方法获取配置

3. **修复LLM提供商获取方法**:
   - 问题: 插件调用时报错 `AttributeError: 'ProviderManager' object has no attribute 'get_default_llm_provider'`
   - 原因: API变更，`provider_manager`不再提供`get_default_llm_provider`方法
   - 解决方案: 使用`self.context.get_using_provider()`方法获取当前使用的LLM提供商

4. **修复权限检查错误**:
   - 问题: 插件加载失败，报错 `AttributeError: module 'astrbot.api.event.filter' has no attribute 'role_filter'`
   - 原因: 使用了不存在的方法 `filter.role_filter("admin")`
   - 解决方案: 使用 `filter.permission_type(PermissionType.ADMIN)` 替代 `filter.role_filter("admin")`，并导入 `PermissionType`

5. **修复枚举值错误**:
   - 问题: 插件加载失败，报错 `AttributeError: GROUP`
   - 原因: 使用了不存在的枚举值 `EventMessageType.GROUP`
   - 解决方案: 修改为正确的枚举值 `EventMessageType.GROUP_MESSAGE`

6. **修复导入错误**: 
   - 问题: 插件加载失败，报错 `ImportError: cannot import name 'type_filter' from 'astrbot.api.event.filter'`
   - 原因: 导入了不存在的 `type_filter` 模块
   - 解决方案: 移除了不必要的导入语句 `from astrbot.api.event.filter import type_filter`

### 2023-05-27

1. **修复消息类型处理**: 
   - 问题: 使用了错误的事件类型 `EventType.AdapterMessageEvent`
   - 解决方案: 更改为使用正确的 `EventMessageType.GROUP`，从 `astrbot.core.star.filter.event_message_type` 导入

## 文件结构

```
group-summarizer/
├── main.py           # 主要插件代码
├── config.yaml       # 插件配置文件
├── prompts.py        # 提示词定义文件
├── README.md         # 说明文档
├── 使用说明.md       # 中文使用说明
└── metadata.yaml     # 插件元数据
```

## 配置方式

插件提供三种配置方式：

1. **YAML配置文件（推荐）**：编辑`config.yaml`文件
2. **AstrBot管理面板**：在插件管理页面进行配置
3. **全局配置文件**：在AstrBot配置文件中添加`group_summarizer`部分

## 自定义提示词

通过编辑`prompts.py`文件可以自定义以下提示词：

- **SYSTEM_PROMPT**：系统提示词，指导AI总结风格
- **SUMMARY_PROMPT_TEMPLATE**：消息总结提示词模板
- **MESSAGE_FORMAT**：单条消息格式模板
- **FEW_MESSAGES_PROMPT**：消息数量过少时的提示词

## 命令

- `/summarize_now` - 手动触发当前群的消息总结（仅管理员可用）
- `/summary [数量]` - 立即总结指定数量的最近消息（仅管理员可用）
- `/summary_status` - 查看当前群的消息收集状态
- `/summary_help` - 显示插件帮助信息

## 开发者信息

- 作者：zy
- 版本：1.0.0
- 许可证：MIT
