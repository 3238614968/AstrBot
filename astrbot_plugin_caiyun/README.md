# caiyun

中国移动云盘 AstrBot 插件，通过 Bot 管理移动云盘账号，支持短信登录、CK管理、商品查询、抢兑、定时检测、面板同步等功能。

## 依赖

- AstrBot >= 4.16, < 5
- httpx >= 0.27
- 外部 SMS API（[caiyun_api](../caiyun_api/)）

## 已实现功能

| 命令 | 说明 |
|------|------|
| `云盘登录` | 短信登录（对接 caiyun_api）/ CK 手动登录 |
| `查看我的CK` | 查看当前账号列表 |
| `删除CK` | 按序号删除指定账号 |
| `云盘商品` | 查看可兑换商品 |
| `云盘商品更新` | 从云端刷新商品列表 |
| `云盘检测` | 检测当前账号 CK 有效性 |
| `云盘一键检测` | 管理员批量检测所有用户 CK |
| `云盘抢兑` | 设置自动抢兑任务 |
| `立即兑换` | 手动立即兑换指定商品 |
| `云盘查询` | 查询兑换记录、云朵余额等 |
| `云盘管理` | 账号管理（修改备注、更新CK、删除） |

### 安全特性

- Authorization 长度校验（>=200字符），自动识别无效账号
- 全局错误信息脱敏，自动过滤 URL、IP、域名、凭证等敏感信息
- 群聊中登录的账号，失效通知仅发送到私聊，不会在群内刷屏
- 定时自动检测账号有效性，失效后私聊通知对应用户

### 面板同步

- 支持同步 CK 到青龙面板或呆呆面板（优先呆呆面板）
- 新增/更新 CK 时自动同步，通过 UID 精确定位面板中的记录
- 多账号通过相同环境变量名 + 不同备注区分

## 配置项

| 配置 | 说明 | 默认值 |
|------|------|--------|
| sms_api_base_url | 短信登录 API 地址 | https://ydyp.apisky.cn |
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

## 设计说明

- 所有网络请求使用 `httpx.AsyncClient`
- 批量检测、跨账号查询使用 `asyncio`，避免阻塞式写法
- 插件运行数据保存在 `data/astrbot_plugin_caiyun/` 目录
- 支持同步 CK 到青龙面板或呆呆面板
- 定时任务基于 `asyncio.sleep` 后台循环，资源消耗极低

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
