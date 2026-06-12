# caiyun

中国移动云盘 AstrBot 插件，通过 Bot 管理移动云盘账号，支持短信登录、CK管理、商品查询、抢兑、定时检测、面板同步等功能。

## 依赖

- AstrBot >= 4.16, < 5
- httpx >= 0.27
- 外部 SMS API（[caiyun_api](../caiyun_api/)）
- 滑块验证码识别 API（复用短信登录 API 域名）

## 已实现功能

| 命令 | 权限 | 说明 |
|------|------|------|
| `云盘登录` | 所有用户 | 短信登录（对接 caiyun_api）/ CK 手动登录 |
| `查看我的CK` | 所有用户 | 查看当前账号列表 |
| `删除CK` | 所有用户 | 按序号删除指定账号 |
| `云盘商品` | 所有用户 | 查看可兑换商品 |
| `云盘商品更新` | 所有用户 | 从云端刷新商品列表 |
| `云盘检测` | 所有用户 | 检测当前账号 CK 有效性 |
| `云盘一键检测` | 管理员 | 批量检测所有用户 CK |
| `云盘一键抢兑` | 管理员 | 手动触发所有已配置抢兑的账号执行抢兑 |
| `云盘抢兑` | 所有用户 | 设置自动抢兑任务 |
| `立即兑换` | 所有用户 | 手动立即兑换指定商品 |
| `云盘查询` | 所有用户 | 查询兑换记录、云朵余额等 |
| `云盘管理` | 所有用户 | 账号管理（修改备注、更新CK、删除） |

### 抢兑功能

- 集成滑块验证码识别，通过 `/api/sms/solve` API 自动识别偏移量
- 兑换接口使用 `exchangeV2`（支持 `puzzleOffset` 参数），识别失败时 fallback 到无滑块兑换
- 最多重试 3 次滑块识别，添加随机偏移（-3 ~ +3）模拟人工操作
- 定时抢兑：每天 **10:00、16:00、00:00** 三个时间点静默执行
- 仅处理已配置抢兑的账号，结果按用户隔离私聊通知

### Token 自动刷新

- 参考移动云盘官方协议实现 `refreshToken` 接口
- 每天定时（默认 10:30）检测所有账号，有效期低于 5 天的自动刷新
- 刷新成功后自动更新本地缓存 + 同步到青龙/呆呆面板
- 汇总结果（成功/失败/无需刷新数量）通知到指定群聊

### 安全特性

- Authorization 长度校验（>=200字符），自动识别无效账号
- 全局错误信息脱敏，自动过滤 URL、IP、域名、凭证等敏感信息
- 群聊中登录的账号，失效通知仅发送到私聊，不会在群内刷屏
- 定时自动检测账号有效性，失效后私聊通知对应用户

### 面板同步

- 支持同步 CK 到青龙面板或呆呆面板（优先呆呆面板）
- 新增/更新 CK 时自动同步，通过 UID 精确定位面板中的记录
- Token 刷新后自动同步更新面板中的环境变量
- 多账号通过相同环境变量名 + 不同备注区分

### 主动通知

- 基于 AstrBot `context.send_message()` API 实现主动消息推送
- 通过 `unified_msg_origin` 存储用户会话来源，支持跨平台通知
- 群聊用户自动推导私聊 origin，确保通知送达
- 定时抢兑结果按用户隔离，每个用户只收到自己账号的通知

## 配置项

| 配置 | 说明 | 默认值 |
|------|------|--------|
| sms_api_base_url | 短信登录 & 滑块识别 API 地址（共用域名） | http://yunpan.apisky.cn |
| qinglong_url | 青龙面板地址（可选） | - |
| qinglong_client_id | 青龙 Client ID | - |
| qinglong_client_secret | 青龙 Client Secret | - |
| qinglong_env_name | 青龙环境变量名 | Gk_yunpan |
| daidai_url | 呆呆面板地址（可选，优先） | - |
| daidai_app_key | 呆呆面板 App Key | - |
| daidai_app_secret | 呆呆面板 App Secret | - |
| daidai_env_name | 呆呆面板环境变量名 | Gk_yunpan |
| user_agent | 请求 UA | MCloudApp/12.4.0 |
| request_timeout_seconds | HTTP 超时时间（秒） | 15 |
| admin_user_ids | 管理员用户 ID 列表 | [] |
| batch_check_concurrency | 批量检测并发数 | 5 |
| scheduled_check_enabled | 启用定时自动检测 | false |
| scheduled_check_hour | 定时检测时间（小时 0-23） | 8 |
| scheduled_check_minute | 定时检测时间（分钟 0-59） | 0 |
| scheduled_refresh_enabled | 启用定时 Token 刷新 | true |
| scheduled_refresh_hour | 定时刷新时间（小时） | 10 |
| scheduled_refresh_minute | 定时刷新时间（分钟） | 30 |
| refresh_notify_group_id | Token 刷新结果通知群聊 ID | 599523323 |

## 设计说明

- 所有网络请求使用 `httpx.AsyncClient`
- 批量检测、跨账号查询使用 `asyncio`，避免阻塞式写法
- 插件运行数据保存在 `data/astrbot_plugin_caiyun/` 目录
- 支持同步 CK 到青龙面板或呆呆面板
- 定时任务基于 `asyncio.sleep` 后台循环，资源消耗极低
- Token 刷新参考官方协议，深度解析嵌套 JSON 提取新 authorization

## 数据存储

JSON 文件存储于 `data/astrbot_plugin_caiyun/` 目录：

| 文件 | 说明 |
|------|------|
| bucket_user.json | 用户与 CK 元数据映射 |
| bucket_token.json | CK 凭证数据 |
| bucket_phone.json | 用户手机号映射 |
| bucket_products.json | 商品缓存 |
| bucket_exchange.json | 抢兑配置 |
| bucket_sessions.json | 用户会话信息（用于主动通知） |

## 短信登录流程

```
用户发送「云盘登录」→ 选择短信登录 → 输入手机号
→ 调用 caiyun_api POST /api/sms/send 发送验证码
→ 用户输入验证码
→ 调用 caiyun_api POST /api/sms/verify 验证登录
→ 校验 Authorization 长度（>=200字符）
→ 获取 Authorization 凭证，保存为 CK
→ 自动同步到配置的面板（呆呆/青龙）
```

## 定时检测流程

```
插件加载 → 启动后台循环
→ 每天在配置的时间自动执行
→ 批量检测所有用户的 CK 有效性
→ 失效 CK：私聊通知对应用户 + 清理本地数据
→ 有失效时：汇总通知管理员
→ 群聊中登录的账号也能通过推导私聊 origin 正常通知
```

## Token 刷新流程

```
插件加载 → 启动后台循环
→ 每天在配置的时间（默认 10:30）自动执行
→ 遍历所有账号，解析 authorization 中的过期时间
→ 有效期低于 5 天的账号：调用 refreshToken 接口刷新
→ 刷新成功：更新本地缓存 + 同步到青龙/呆呆面板
→ 汇总结果（成功/失败/无需刷新数量）通知到指定群聊
```

## 定时抢兑流程

```
插件加载 → 启动后台循环
→ 每天 10:00、16:00、00:00 三个时间点静默执行
→ 遍历所有用户，仅处理已配置抢兑的账号
→ 获取滑块验证码 → 识别偏移量 → 调用 exchangeV2 兑换
→ 结果按用户隔离：每个用户只收到自己账号的抢兑结果
→ 短期抢兑成功后自动清除配置，长期抢兑保留
```
