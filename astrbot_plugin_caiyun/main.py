from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter

PLUGIN_NAME = "astrbot_plugin_caiyun"
TIMEOUT_TEXT = "⏰ 输入超时，已自动退出"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; 23049RAD8C Build/TKQ1.221114.001; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/108.0.5359.128 "
    "Mobile Safari/537.36 MCloudApp/12.4.0 AppLanguage/zh-CN"
)
PHONE_RE = re.compile(r"^1\d{10}$")
CK_RE = re.compile(r"^Basic\s+([^#]{1,300})#(\d{11})$")

BUCKET_USER = "Hz_caiyun_user"
BUCKET_TOKEN = "Hz_caiyun_token"
BUCKET_PHONE = "Hz_caiyun_phone"
BUCKET_PRODUCTS = "Hz_caiyun_products"
BUCKET_EXCHANGE = "Hz_caiyun_exchange"
BUCKET_SESSION = "Hz_caiyun_sessions"

CATEGORY_NAME_MAP = {
    "0": "其他权益奖品",
    "1": "视频类会员",
    "2": "音乐类会员",
    "5": "外卖美食权益",
    "7": "快递寄件券",
    "8": "云盘转存券",
    "9": "实用工具类",
    "10": "奶茶饮品权益",
    "11": "奶茶饮品权益",
    "13": "咖啡饮品权益",
    "14": "游戏礼包权益",
    "15": "全国通用流量权益",
}
CATEGORY_DISPLAY_ORDER = [
    "其他权益奖品",
    "视频类会员",
    "音乐类会员",
    "外卖美食权益",
    "快递寄件券",
    "云盘转存券",
    "实用工具类",
    "奶茶饮品权益",
    "咖啡饮品权益",
    "游戏礼包权益",
    "全国通用流量权益",
]


@dataclass(slots=True)
class CKInfo:
    user_id: str
    phone: str
    ck: str
    remark: str
    add_time: str


@dataclass(slots=True)
class CKMetadata:
    token_key: str
    remark: str


@dataclass(slots=True)
class ProductInfo:
    prize_id: str
    prize_name: str
    p_order: int
    category: str


@dataclass(slots=True)
class ExchangeConfig:
    prize_id: str
    prize_name: str
    token_key: str
    is_long_term: bool
    create_time: str


class JsonBucketStore:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.files = {
            BUCKET_USER: base_dir / "bucket_user.json",
            BUCKET_TOKEN: base_dir / "bucket_token.json",
            BUCKET_PHONE: base_dir / "bucket_phone.json",
            BUCKET_PRODUCTS: base_dir / "bucket_products.json",
            BUCKET_EXCHANGE: base_dir / "bucket_exchange.json",
            BUCKET_SESSION: base_dir / "bucket_sessions.json",
        }
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        for bucket, path in self.files.items():
            self._data[bucket] = self._load_json(path)
            if not path.exists():
                self._write_json(path, self._data[bucket])

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def get(self, bucket: str, key: str, default: Any = None) -> Any:
        async with self._lock:
            value = self._data.get(bucket, {}).get(key, default)
            return copy.deepcopy(value)

    async def set(self, bucket: str, key: str, value: Any) -> None:
        async with self._lock:
            self._data.setdefault(bucket, {})[key] = copy.deepcopy(value)
            snapshot = copy.deepcopy(self._data[bucket])
            path = self.files[bucket]
        await asyncio.to_thread(self._write_json, path, snapshot)

    async def delete(self, bucket: str, key: str) -> None:
        async with self._lock:
            self._data.setdefault(bucket, {}).pop(key, None)
            snapshot = copy.deepcopy(self._data[bucket])
            path = self.files[bucket]
        await asyncio.to_thread(self._write_json, path, snapshot)

    async def all_keys(self, bucket: str) -> list[str]:
        async with self._lock:
            return list(self._data.get(bucket, {}).keys())

    async def all_items(self, bucket: str) -> dict[str, Any]:
        async with self._lock:
            return copy.deepcopy(self._data.get(bucket, {}))

    async def replace_bucket(self, bucket: str, data: dict[str, Any]) -> None:
        async with self._lock:
            self._data[bucket] = copy.deepcopy(data)
            snapshot = copy.deepcopy(self._data[bucket])
            path = self.files[bucket]
        await asyncio.to_thread(self._write_json, path, snapshot)

class YunpanHttpClient:
    def __init__(self, config_getter):
        self._config_getter = config_getter
        self._client = httpx.AsyncClient(follow_redirects=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _config(self) -> dict[str, Any]:
        return self._config_getter()

    def _timeout(self) -> httpx.Timeout:
        seconds = max(5.0, float(self._config().get("request_timeout_seconds", 15)))
        return httpx.Timeout(seconds)

    def _user_agent(self) -> str:
        return str(self._config().get("user_agent", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT)

    def _mcloud_headers(self, jwt_token: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self._user_agent(),
            "Accept-Encoding": "gzip, deflate",
            "showloading": "true",
            "x-requested-with": "com.chinamobile.mcloud",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        if jwt_token:
            headers["jwttoken"] = jwt_token
        return headers

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        response = await self._client.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=self._timeout(),
        )
        response.raise_for_status()
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"解析响应失败：{exc}") from exc

    async def send_sms_code(self, phone: str) -> str:
        data = await self._request_json(
            "POST",
            f"{self._config()['sms_api_base_url'].rstrip('/')}/api/sms/send",
            json_body={"phone": phone},
        )
        if data.get("code") != 0:
            raise ValueError(str(data.get("message") or "发送验证码失败"))
        task_id = (data.get("data") or {}).get("task_id")
        if not task_id:
            raise ValueError("未获取到 task_id")
        return str(task_id)

    async def verify_sms_login(self, phone: str, sms_code: str, task_id: str) -> str:
        data = await self._request_json(
            "POST",
            f"{self._config()['sms_api_base_url'].rstrip('/')}/api/sms/verify",
            json_body={"phone": phone, "code": sms_code, "task_id": task_id},
        )
        if data.get("code") != 0:
            raise ValueError(str(data.get("message") or "短信验证失败"))
        authorization = (data.get("data") or {}).get("authorization")
        if not authorization:
            raise ValueError("未获取到 authorization")
        authorization = str(authorization)
        if len(authorization) < 200:
            raise ValueError("账号无效")
        return authorization

    async def get_qinglong_token(self) -> str:
        config = self._config()
        ql_url = str(config.get("qinglong_url", "")).rstrip("/")
        client_id = str(config.get("qinglong_client_id", ""))
        client_secret = str(config.get("qinglong_client_secret", ""))
        if not (ql_url and client_id and client_secret):
            raise ValueError("青龙配置不完整")
        data = await self._request_json(
            "GET",
            f"{ql_url}/open/auth/token",
            params={"client_id": client_id, "client_secret": client_secret},
        )
        if data.get("code") != 200:
            raise ValueError(f"青龙返回错误码：{data.get('code')}")
        token = (data.get("data") or {}).get("token")
        if not token:
            raise ValueError("青龙 token 为空")
        return str(token)

    async def add_qinglong_env(self, token: str, value: str, remark: str, env_name: str) -> None:
        config = self._config()
        ql_url = str(config.get("qinglong_url", "")).rstrip("/")
        data = await self._request_json(
            "POST",
            f"{ql_url}/open/envs",
            headers={"Authorization": f"Bearer {token}"},
            json_body=[{"name": env_name, "value": value, "remarks": remark}],
        )
        if data.get("code") != 200:
            detail = data.get("data") or ""
            message = data.get("message") or "未知错误"
            if detail:
                raise ValueError(f"青龙返回错误：{message}，详情：{detail}")
            raise ValueError(f"青龙返回错误：{message}")

    async def get_daidai_token(self) -> str:
        config = self._config()
        dd_url = str(config.get("daidai_url", "")).rstrip("/")
        app_key = str(config.get("daidai_app_key", ""))
        app_secret = str(config.get("daidai_app_secret", ""))
        if not (dd_url and app_key and app_secret):
            raise ValueError("呆呆面板配置不完整")
        data = await self._request_json(
            "POST",
            f"{dd_url}/api/open-api/token",
            json_body={"app_key": app_key, "app_secret": app_secret},
        )
        token = ((data.get("data") or {}).get("access_token") or "")
        if not token:
            raise ValueError(f"呆呆面板返回错误：{data.get('message') or '未知错误'}")
        return str(token)

    async def find_daidai_env(self, token: str, env_name: str, keyword: str = "") -> int | None:
        config = self._config()
        dd_url = str(config.get("daidai_url", "")).rstrip("/")
        headers = {"Authorization": f"Bearer {token}"}
        page = 1
        while True:
            data = await self._request_json(
                "GET",
                f"{dd_url}/api/envs",
                headers=headers,
                params={"keyword": env_name, "page": page, "page_size": 100},
            )
            items = data.get("data") or []
            if isinstance(items, dict):
                items = items.get("data") or items.get("list") or []
            for env in items:
                if env.get("name") == env_name:
                    if not keyword or keyword in (env.get("remarks") or ""):
                        return env.get("id")
            if len(items) < 100:
                break
            page += 1
        return None

    async def add_or_update_daidai_env(self, token: str, value: str, remark: str, env_name: str, keyword: str = "") -> None:
        config = self._config()
        dd_url = str(config.get("daidai_url", "")).rstrip("/")
        env_id = await self.find_daidai_env(token, env_name, keyword)
        headers = {"Authorization": f"Bearer {token}"}
        if env_id is not None:
            await self._request_json(
                "PUT",
                f"{dd_url}/api/envs/{env_id}",
                headers=headers,
                json_body={"name": env_name, "value": value, "remarks": remark},
            )
        else:
            await self._request_json(
                "POST",
                f"{dd_url}/api/envs",
                headers=headers,
                json_body={"name": env_name, "value": value, "remarks": remark},
            )

    async def get_sso_token(self, authorization: str, account: str) -> str:
        data = await self._request_json(
            "POST",
            "https://orches.yun.139.com/orchestration/auth-rebuild/token/v1.0/querySpecToken",
            headers={
                "Authorization": authorization,
                "User-Agent": self._user_agent(),
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Host": "orches.yun.139.com",
            },
            json_body={"account": account, "toSourceId": "001005"},
        )
        if not data.get("success"):
            raise ValueError(f"SSO失败：{data.get('message') or '未知错误'}")
        token = (data.get("data") or {}).get("token")
        if not token:
            raise ValueError("SSO token 为空")
        return str(token)

    async def get_jwt_token(self, sso_token: str) -> str:
        data = await self._request_json(
            "POST",
            f"https://caiyun.feixin.10086.cn:7071/portal/auth/tyrzLogin.action?ssoToken={sso_token}",
            headers={
                "User-Agent": self._user_agent(),
                "Content-Type": "application/json",
                "Accept": "*/*",
            },
        )
        if data.get("code") != 0:
            raise ValueError(f"JWT失败：{data.get('msg') or '未知错误'}")
        token = (data.get("result") or {}).get("token")
        if not token:
            raise ValueError("JWT 返回的 token 为空")
        return str(token)

    async def query_product_list(self, jwt_token: str) -> dict[str, Any]:
        data = await self._request_json(
            "GET",
            "https://m.mcloud.139.com/market/signin/page/exchangeList?client=app&clientVersion=12.4.0",
            headers=self._mcloud_headers(jwt_token),
        )
        if data.get("code") != 0:
            raise ValueError(f"接口返回错误：{data.get('msg') or '未知错误'}")
        return data

    async def do_exchange(self, jwt_token: str, prize_id: str) -> dict[str, Any]:
        data = await self._request_json(
            "GET",
            f"https://m.mcloud.139.com/market/signin/page/exchange?prizeId={prize_id}&client=app&clientVersion=12.4.0&smsCode=",
            headers=self._mcloud_headers(jwt_token),
        )
        if data.get("msg") != "success":
            raise ValueError(f"兑换失败：{data.get('msg') or '未知错误'}")
        return data

    async def query_cloud_record(self, jwt_token: str) -> dict[str, Any]:
        data = await self._request_json(
            "GET",
            "https://m.mcloud.139.com/market/signin/public/cloudRecord?type=0&pageNumber=1&pageSize=10",
            headers=self._mcloud_headers(jwt_token),
        )
        if data.get("code") != 0:
            raise ValueError(f"接口返回错误：{data.get('msg') or '未知错误'}")
        return data

    async def query_sign_info(self, jwt_token: str) -> dict[str, Any]:
        data = await self._request_json(
            "GET",
            "https://m.mcloud.139.com/market/signin/page/startSignIn?client=app",
            headers=self._mcloud_headers(jwt_token),
        )
        if data.get("code") != 0 and data.get("msg") != "success":
            raise ValueError(f"接口返回错误：{data.get('msg') or '未知错误'}")
        return data

    async def query_cloud_total(self, jwt_token: str) -> dict[str, Any]:
        data = await self._request_json(
            "GET",
            "https://m.mcloud.139.com/market/signin/page/infoV3?client=app",
            headers=self._mcloud_headers(jwt_token),
        )
        if data.get("code") != 0:
            raise ValueError(f"接口返回错误：{data.get('msg') or '未知错误'}")
        return data

    async def query_prize_record(self, jwt_token: str) -> dict[str, Any]:
        data = await self._request_json(
            "GET",
            "https://m.mcloud.139.com/ycloud/prizeApi/checkPrize/getUserPrizeLogPageV2",
            headers=self._mcloud_headers(jwt_token),
            params={"currPage": 1, "pageSize": 10},
        )
        if data.get("code") != 0:
            raise ValueError(f"???????{data.get('msg') or '????'}")
        return data


def mask_phone(phone: str) -> str:
    if len(phone) != 11:
        return phone
    return f"{phone[:3]}****{phone[7:]}"


_SENSITIVE_PATTERNS: list[tuple[re.Pattern, str]] = []


def _init_sensitive_patterns() -> None:
    global _SENSITIVE_PATTERNS
    _SENSITIVE_PATTERNS = [
        (re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE), "[URL]"),
        (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
        (re.compile(r"\b(?:app_key|app_secret|client_id|client_secret|Authorization|Bearer|token|password|secret|cookie)\s*[:=]\s*\S+", re.IGNORECASE), "[CREDENTIALS]"),
        (re.compile(r"Basic\s+[A-Za-z0-9+/=]{10,}", re.IGNORECASE), "[AUTH]"),
        (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", re.IGNORECASE), "[JWT]"),
    ]


_init_sensitive_patterns()


def sanitize_error(obj: Any) -> str:
    text = str(obj)
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def parse_expire_time(expire_time: Any) -> datetime | None:
    value = str(expire_time or "").strip()
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def format_expire_time(expire_time: Any) -> str:
    parsed = parse_expire_time(expire_time)
    if parsed is not None:
        return parsed.strftime("%Y-%m-%d")
    value = str(expire_time or "").strip()
    if not value:
        return ""
    return value.replace("T", " ").split(".", 1)[0][:10]


def is_current_month_prize(expire_time: Any) -> bool:
    parsed = parse_expire_time(expire_time)
    if parsed is None:
        return False
    now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
    return parsed.year == now.year and parsed.month == now.month

def mask_user_id(user_id: str) -> str:
    if len(user_id) <= 7:
        return user_id
    return f"{user_id[:3]}****{user_id[-4:]}"


def parse_ck_from_string(ck: str) -> tuple[str, str]:
    parts = ck.split("#", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("CK格式错误，缺少#分隔符或内容为空")
    return parts[0], parts[1]


def validate_ck(ck: str) -> str:
    if "http://" in ck or "https://" in ck:
        raise ValueError("CK中不能包含http://或https://")
    match = CK_RE.fullmatch(ck.strip())
    if not match:
        raise ValueError("格式不正确，应为：Basic xxxxx#手机号")
    basic_part, phone = match.groups()
    if not basic_part.strip():
        raise ValueError("Basic部分不能为空")
    if len(basic_part) > 300:
        raise ValueError("Basic部分超过300位限制")
    return phone


def build_message(text: str) -> MessageChain:
    return MessageChain().message(text)

@register(
    PLUGIN_NAME,
    "XiaoHai",
    "移动云盘Bot管理插件 CK/商品/抢兑/查询/管理插件",
    "0.1.1",
)
class YidongYunpanPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path("data") / PLUGIN_NAME
        self.store = JsonBucketStore(self.data_dir)
        self.http = YunpanHttpClient(self._runtime_config)
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._track_task(self._scheduled_check_loop())

    async def terminate(self):
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        await self.http.aclose()

    def _runtime_config(self) -> dict[str, Any]:
        return {
            "sms_api_base_url": str(self.config.get("sms_api_base_url", "https://smscaiyun.779776.xyz") or "https://smscaiyun.779776.xyz"),
            "qinglong_url": str(self.config.get("qinglong_url", "") or ""),
            "qinglong_client_id": str(self.config.get("qinglong_client_id", "") or ""),
            "qinglong_client_secret": str(self.config.get("qinglong_client_secret", "") or ""),
            "qinglong_env_name": str(self.config.get("qinglong_env_name", "Gk_yunpan") or "Gk_yunpan"),
            "daidai_url": str(self.config.get("daidai_url", "") or ""),
            "daidai_app_key": str(self.config.get("daidai_app_key", "") or ""),
            "daidai_app_secret": str(self.config.get("daidai_app_secret", "") or ""),
            "daidai_env_name": str(self.config.get("daidai_env_name", "Gk_yunpan") or "Gk_yunpan"),
            "user_agent": str(self.config.get("user_agent", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT),
            "request_timeout_seconds": int(self.config.get("request_timeout_seconds", 15) or 15),
            "admin_user_ids": [str(item).strip() for item in (self.config.get("admin_user_ids", []) or []) if str(item).strip()],
            "batch_check_concurrency": max(1, int(self.config.get("batch_check_concurrency", 5) or 5)),
        }

    def _track_task(self, coro):
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(lambda current: self._background_tasks.discard(current))
        return task

    async def _scheduled_check_loop(self) -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            return
        while True:
            config = self._runtime_config()
            if not config.get("scheduled_check_enabled"):
                await asyncio.sleep(60)
                continue
            try:
                hour = max(0, min(23, int(config.get("scheduled_check_hour", 8) or 8)))
                minute = max(0, min(59, int(config.get("scheduled_check_minute", 0) or 0)))
            except (ValueError, TypeError):
                hour, minute = 8, 0
            now = datetime.now()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            try:
                await asyncio.sleep(wait_seconds)
            except asyncio.CancelledError:
                return
            logger.info("定时检测任务开始执行")
            try:
                total_invalid, message = await self._run_batch_check("")
                admin_ids = self._runtime_config().get("admin_user_ids", [])
                if admin_ids and total_invalid > 0:
                    for admin_id in admin_ids:
                        session_info = await self.store.get(BUCKET_SESSION, admin_id, {}) or {}
                        admin_origin = session_info.get("origin", "") if isinstance(session_info, dict) else ""
                        if admin_origin:
                            await self._send_to_origin(admin_origin, message)
            except Exception:
                logger.exception("定时检测任务执行失败")

    def _now_str(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _panel_name(self) -> str:
        config = self._runtime_config()
        if config.get("daidai_url") and config.get("daidai_app_key") and config.get("daidai_app_secret"):
            return "呆呆面板"
        if config.get("qinglong_url") and config.get("qinglong_client_id") and config.get("qinglong_client_secret"):
            return "青龙面板"
        return "面板"

    def _is_cancel(self, text: str | None) -> bool:
        return (text or "").strip().lower() == "q"

    async def _send_text(self, event: AstrMessageEvent, text: str) -> None:
        await event.send(event.plain_result(text))

    async def _send_to_origin(self, origin: str, text: str) -> bool:
        if not origin:
            return False
        try:
            await self.context.send_message(origin, MessageChain().message(text))
            return True
        except Exception:
            logger.exception("主动发送消息失败: %s", origin)
            return False

    async def _remember_origin(self, event: AstrMessageEvent) -> None:
        user_id = str(event.get_sender_id())
        origin = getattr(event, "unified_msg_origin", "")
        if not user_id or not origin:
            return
        group_id = getattr(event.message_obj, "group_id", "") if hasattr(event, "message_obj") else ""
        if group_id:
            private_origin = self._derive_private_origin(origin, user_id)
            if private_origin:
                existing = await self.store.get(BUCKET_SESSION, user_id, {}) or {}
                if not existing.get("origin"):
                    await self.store.set(BUCKET_SESSION, user_id, {"origin": private_origin, "updated_at": self._now_str()})
            return
        await self.store.set(BUCKET_SESSION, user_id, {"origin": origin, "updated_at": self._now_str()})

    @staticmethod
    def _derive_private_origin(origin: str, sender_id: str) -> str | None:
        parts = origin.split(":", 2)
        if len(parts) < 3:
            return None
        platform = parts[0]
        private_type_map = {
            "aiocqhttp": "friend",
            "qqofficial": "friend",
            "qqbot": "friend",
            "telegram": "friend",
            "gewechat": "friend",
            "wecom": "friend",
            "lark": "friend",
            "dingtalk": "friend",
            "discord": "friend",
            "slack": "friend",
        }
        msg_type = private_type_map.get(platform, "friend")
        return f"{platform}:{msg_type}:{sender_id}"

    async def _ask_text(
        self,
        event: AstrMessageEvent,
        prompt: str,
        *,
        timeout: int = 60,
        timeout_notice: str | None = TIMEOUT_TEXT,
    ) -> str | None:
        await self._send_text(event, prompt)
        holder: dict[str, str] = {}
        expected_sender_id = str(event.get_sender_id() or "")
        expected_origin = str(getattr(event, "unified_msg_origin", "") or "")
        get_session_id = getattr(event, "get_session_id", None)
        expected_session_id = str(get_session_id() or "") if callable(get_session_id) else ""

        @session_waiter(timeout=timeout, record_history_chains=False)
        async def waiter(controller: SessionController, waiter_event: AstrMessageEvent):
            current_sender_id = str(waiter_event.get_sender_id() or "")
            current_origin = str(getattr(waiter_event, "unified_msg_origin", "") or "")
            waiter_get_session_id = getattr(waiter_event, "get_session_id", None)
            current_session_id = str(waiter_get_session_id() or "") if callable(waiter_get_session_id) else ""
            if expected_sender_id and current_sender_id and current_sender_id != expected_sender_id:
                return
            if expected_session_id and current_session_id and current_session_id != expected_session_id:
                return
            if expected_origin and current_origin and current_origin != expected_origin:
                return
            text = (waiter_event.message_str or "").strip()
            if not text:
                return
            await self._remember_origin(waiter_event)
            holder["text"] = text
            waiter_event.stop_event()
            controller.stop()

        try:
            await waiter(event)
        except TimeoutError:
            if timeout_notice:
                await self._send_text(event, timeout_notice)
            return None
        return holder.get("text")

    async def _ask_required_text(self, event: AstrMessageEvent, prompt: str, *, timeout: int = 60) -> str | None:
        text = await self._ask_text(event, prompt, timeout=timeout)
        if text is None:
            return None
        if self._is_cancel(text):
            await self._send_text(event, "👋 已取消操作")
            return None
        return text

    async def _get_user_metadata(self, user_id: str) -> list[CKMetadata]:
        raw = await self.store.get(BUCKET_USER, user_id, []) or []
        result: list[CKMetadata] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("token_key"):
                    result.append(CKMetadata(token_key=str(item.get("token_key")), remark=str(item.get("remark") or "")))
        return result

    async def _set_user_metadata(self, user_id: str, metadata_list: list[CKMetadata]) -> None:
        if metadata_list:
            await self.store.set(BUCKET_USER, user_id, [asdict(item) for item in metadata_list])
        else:
            await self.store.delete(BUCKET_USER, user_id)

    async def _get_user_phones(self, user_id: str) -> list[str]:
        raw = await self.store.get(BUCKET_PHONE, user_id, []) or []
        return [str(item) for item in raw] if isinstance(raw, list) else []

    async def _set_user_phones(self, user_id: str, phones: list[str]) -> None:
        if phones:
            await self.store.set(BUCKET_PHONE, user_id, phones)
        else:
            await self.store.delete(BUCKET_PHONE, user_id)

    async def _get_all_products(self) -> dict[str, ProductInfo]:
        raw = await self.store.all_items(BUCKET_PRODUCTS)
        result: dict[str, ProductInfo] = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                result[key] = ProductInfo(
                    prize_id=str(value.get("prize_id") or key),
                    prize_name=str(value.get("prize_name") or ""),
                    p_order=int(value.get("p_order") or 0),
                    category=str(value.get("category") or "未知分类"),
                )
        return result

    async def _get_exchange_config(self, token_key: str) -> ExchangeConfig | None:
        raw = await self.store.get(BUCKET_EXCHANGE, token_key, None)
        if not isinstance(raw, dict):
            return None
        return ExchangeConfig(
            prize_id=str(raw.get("prize_id") or ""),
            prize_name=str(raw.get("prize_name") or ""),
            token_key=str(raw.get("token_key") or token_key),
            is_long_term=bool(raw.get("is_long_term")),
            create_time=str(raw.get("create_time") or ""),
        )

    async def _generate_token_key(self, user_id: str) -> str:
        metadata = await self._get_user_metadata(user_id)
        seq = len(metadata) + 1
        while True:
            token_key = f"{int(time.time() * 1000)}{seq:03d}"
            if not await self.store.get(BUCKET_TOKEN, token_key, None):
                return token_key
            seq += 1
            await asyncio.sleep(0)

    async def _save_ck_record(self, user_id: str, ck: str, remark: str, phone: str | None = None) -> str:
        token_key = await self._generate_token_key(user_id)
        await self.store.set(BUCKET_TOKEN, token_key, ck)
        metadata = await self._get_user_metadata(user_id)
        metadata.append(CKMetadata(token_key=token_key, remark=remark))
        await self._set_user_metadata(user_id, metadata)
        if phone:
            phones = await self._get_user_phones(user_id)
            if phone not in phones:
                phones.append(phone)
                await self._set_user_phones(user_id, phones)
        return token_key

    async def _save_ck_info(self, info: CKInfo) -> str:
        return await self._save_ck_record(info.user_id, info.ck, info.remark, info.phone)

    async def _sync_to_qinglong(self, token_key: str, info: CKInfo) -> None:
        config = self._runtime_config()
        if config.get("daidai_url") and config.get("daidai_app_key") and config.get("daidai_app_secret"):
            token = await self.http.get_daidai_token()
            env_name = config["daidai_env_name"]
            remark = f"UID:{token_key}丨用户:{info.user_id}丨手机:{info.phone}"
            await self.http.add_or_update_daidai_env(token, info.ck, remark, env_name, keyword=f"UID:{token_key}")
        elif config.get("qinglong_url") and config.get("qinglong_client_id") and config.get("qinglong_client_secret"):
            token = await self.http.get_qinglong_token()
            env_name = config["qinglong_env_name"]
            remark = f"UID:{token_key}丨用户:{info.user_id}丨手机:{info.phone}"
            await self.http.add_qinglong_env(token, info.ck, remark, env_name)
        else:
            raise ValueError("未配置面板同步，请填写青龙面板或呆呆面板配置")

    async def _get_jwt_from_ck(self, ck: str) -> str:
        authorization, account = parse_ck_from_string(ck)
        sso_token = await self.http.get_sso_token(authorization, account)
        return await self.http.get_jwt_token(sso_token)

    async def _get_jwt_for_user(self, event: AstrMessageEvent) -> str:
        user_id = str(event.get_sender_id())
        all_cks: list[str] = []
        failed_reasons: list[str] = []
        user_metadata = await self._get_user_metadata(user_id)
        for metadata in user_metadata:
            ck = await self.store.get(BUCKET_TOKEN, metadata.token_key, "")
            if ck:
                all_cks.append(str(ck))
        for uid in await self.store.all_keys(BUCKET_USER):
            if uid == user_id:
                continue
            for metadata in await self._get_user_metadata(uid):
                ck = await self.store.get(BUCKET_TOKEN, metadata.token_key, "")
                if ck:
                    all_cks.append(str(ck))
        if not all_cks:
            raise ValueError("桶中没有可用的CK，请先发送「云盘登录」添加CK")
        for index, ck in enumerate(all_cks, start=1):
            try:
                token = await self._get_jwt_from_ck(ck)
                if index > len(user_metadata) and user_metadata:
                    await self._send_text(event, "💡 您的CK可能已失效，已自动切换到备用CK")
                return token
            except Exception as exc:
                failed_reasons.append(f"CK{index}: {sanitize_error(exc)}")
        raise ValueError("所有CK都无法获取JWT令牌（共%d个）\n\n%s\n\n💡 建议：请重新发送「云盘登录」更新CK" % (len(all_cks), "\n".join(failed_reasons)))

    def _get_account_label(self, metadata: CKMetadata, index: int) -> str:
        if metadata.remark and metadata.remark != "无备注":
            return metadata.remark
        return f"账号{index}"

    async def _collect_ck_by_sms(self, event: AstrMessageEvent) -> tuple[str, str] | None:
        phone = await self._ask_required_text(
            event,
            "=====短信登录=====\n📱 请输入手机号（11位）\n------------------\n回复\"q\"退出\n⏱️ 60秒无响应自动退出",
        )
        if phone is None:
            return None
        if not PHONE_RE.fullmatch(phone):
            await self._send_text(event, "❌ 手机号格式不正确，需为11位数字且以1开头")
            return None
        await self._send_text(event, "📤 正在发送短信验证码...")
        task_id = await self.http.send_sms_code(phone)
        await self._send_text(event, "✅ 验证码已发送，请注意查收")
        sms_code = await self._ask_required_text(
            event,
            "📩 请输入收到的短信验证码\n------------------\n回复\"q\"退出\n⏱️ 120 秒无响应自动退出",
            timeout=120,
        )
        if sms_code is None:
            return None
        await self._send_text(event, "🔐 正在验证...")
        try:
            authorization = await self.http.verify_sms_login(phone, sms_code, task_id)
        except ValueError as exc:
            if "账号无效" in str(exc):
                await self._send_text(event, "❌ 账号无效或已失效，请检查账号状态后重新登录")
                return None
            raise
        return f"{authorization}#{phone}", phone

    async def _collect_ck_manually(self, event: AstrMessageEvent) -> tuple[str, str] | None:
        ck_input = await self._ask_required_text(
            event,
            "=====账号登录=====\n❶ 下载Via浏览器访问 yun.139.com/m/#/login 完成登录，左上角查看Cookies找到参数authorization的值『Basic xxxxx』\n❷请勿点击退出将导致CK失效，多号用户请清软件数据重复操作，不用带『;』号，分隔『#』号是英文符，参数『Basic xxxxx』内的空格不能删\n❸按如下格式发送\n『参数值#手机号』 例: Basic xxxxx#110\n------------------\n📺 视频教程：https://www.bilibili.com/video/BV1ehegewEsR\n------------------\n回复\"q\"退出\n⏱️ 60秒无响应自动退出",
        )
        if ck_input is None:
            return None
        phone = validate_ck(ck_input)
        return ck_input, phone

    async def _remove_invalid_metadata(self, user_id: str, metadata_list: list[CKMetadata], invalid_metadata: list[CKMetadata]) -> None:
        invalid_keys = {item.token_key for item in invalid_metadata}
        new_list = [item for item in metadata_list if item.token_key not in invalid_keys]
        await self._set_user_metadata(user_id, new_list)
        for item in invalid_metadata:
            await self.store.delete(BUCKET_TOKEN, item.token_key)

    async def _notify_invalid_ck(self, user_id: str, invalid_metadata: list[CKMetadata]) -> bool:
        session_info = await self.store.get(BUCKET_SESSION, user_id, {}) or {}
        origin = session_info.get("origin", "") if isinstance(session_info, dict) else ""
        if not origin:
            return False
        message = "======云盘通知======\n"
        for index, metadata in enumerate(invalid_metadata, start=1):
            message += f"👤 账号: {self._get_account_label(metadata, index)}\n"
            message += "📢 消息: CK已失效: 鉴权失效\n"
            message += "====================\n"
        message += "\n💡 建议重新发送「云盘登录」更新CK"
        return await self._send_to_origin(origin, message)

    async def _show_products_in_category(self, event: AstrMessageEvent, products: list[ProductInfo], category_name: str) -> None:
        if not products:
            await self._send_text(event, f"❌ {category_name} 暂无商品")
            return
        message = f"📦 {category_name}（共{len(products)}个）\n\n"
        for index, product in enumerate(products, start=1):
            message += f"🎁 {index}. {product.prize_name}\n💎 云朵：{product.p_order}\n🆔 ID：{product.prize_id}\n------------------\n"
        await self._send_text(event, message)

    async def _check_user_cks(self, user_id: str) -> tuple[int, int, list[CKMetadata], list[CKMetadata]]:
        metadata_list = await self._get_user_metadata(user_id)
        invalid_metadata: list[CKMetadata] = []
        valid_metadata: list[CKMetadata] = []
        for metadata in metadata_list:
            ck = await self.store.get(BUCKET_TOKEN, metadata.token_key, "")
            if not ck:
                invalid_metadata.append(metadata)
                continue
            try:
                authorization, account = parse_ck_from_string(str(ck))
                await self.http.get_sso_token(authorization, account)
            except Exception:
                invalid_metadata.append(metadata)
                continue
            valid_metadata.append(metadata)
        return len(metadata_list), len(valid_metadata), invalid_metadata, metadata_list

    async def _run_batch_check(self, summary_origin: str) -> tuple[int, str]:
        all_user_ids = await self.store.all_keys(BUCKET_USER)
        total_users = 0
        total_cks = 0
        total_valid = 0
        total_invalid = 0
        notified_users = 0
        semaphore = asyncio.Semaphore(self._runtime_config()["batch_check_concurrency"])

        async def process_user(user_id: str):
            async with semaphore:
                total, valid, invalid_list, metadata_list = await self._check_user_cks(user_id)
                return user_id, total, valid, invalid_list, metadata_list

        results = await asyncio.gather(*(process_user(user_id) for user_id in all_user_ids), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.exception("批量检测某个用户时失败", exc_info=result)
                continue
            user_id, total, valid, invalid_list, metadata_list = result
            if total == 0:
                continue
            total_users += 1
            total_cks += total
            total_valid += valid
            total_invalid += len(invalid_list)
            if invalid_list:
                await self._remove_invalid_metadata(user_id, metadata_list, invalid_list)
                if await self._notify_invalid_ck(user_id, invalid_list):
                    notified_users += 1
        message = (
            "✅ 批量检测完成\n\n"
            f"📊 统计信息：\n👥 检测用户：{total_users}\n📦 总CK数：{total_cks}\n"
            f"✅ 有效：{total_valid}\n❌ 失效：{total_invalid}\n📢 已通知：{notified_users} 个用户"
        )
        await self._send_to_origin(summary_origin, message)
        return total_invalid, message

    async def _handle_yunpan_login(self, event: AstrMessageEvent) -> None:
        choice = await self._ask_required_text(
            event,
            "=====账号登录=====\n请选择登录方式：\n\n1️⃣ 短信登录（推荐）\n2️⃣ CK登录\n\n💡 请输入数字选择\n回复\"q\"退出\n⏱️ 60秒无响应自动退出",
        )
        if choice is None:
            return
        if choice == "1":
            await self._handle_sms_login(event)
        elif choice == "2":
            await self._handle_ck_login(event)
        else:
            await self._send_text(event, "❌ 无效的选择，请输入 1 或 2")

    async def _handle_sms_login(self, event: AstrMessageEvent) -> None:
        user_id = str(event.get_sender_id())
        collected = await self._collect_ck_by_sms(event)
        if not collected:
            return
        ck_str, phone = collected
        remark = await self._ask_text(
            event,
            "✅ 登录成功\n\n💬 请输入备注信息（用于标识该CK）\n------------------\n回复\"q\"退出\n⏱️ 60秒无响应自动退出",
        )
        if remark is None:
            return
        if self._is_cancel(remark):
            await self._send_text(event, "👋 已取消操作")
            return
        remark = remark or "无备注"
        info = CKInfo(user_id=user_id, phone=phone, ck=ck_str, remark=remark, add_time=self._now_str())
        token_key = await self._save_ck_info(info)
        try:
            await self._sync_to_qinglong(token_key, info)
        except Exception as exc:
            await self._send_text(event, f"⚠️ 数据已保存，但同步到{self._panel_name()}失败：{sanitize_error(exc)}\n\n您可以稍后重试")
            return
        await self._send_text(
            event,
            f"✅ 添加成功！\n\n🆔 UID：{token_key}\n👤 用户ID：{mask_user_id(user_id)}\n📱 手机号：{mask_phone(phone)}\n💬 备注：{remark}\n⏰ 添加时间：{info.add_time}\n\n已成功同步到{self._panel_name()}！",
        )

    async def _handle_ck_login(self, event: AstrMessageEvent) -> None:
        user_id = str(event.get_sender_id())
        collected = await self._collect_ck_manually(event)
        if not collected:
            return
        ck_input, phone = collected
        remark = await self._ask_text(
            event,
            "✅ CK格式正确\n\n💬 请输入备注信息（用于标识该CK）\n------------------\n回复\"q\"退出\n⏱️ 60秒无响应自动退出",
        )
        if remark is None:
            return
        if self._is_cancel(remark):
            await self._send_text(event, "👋 已取消操作")
            return
        remark = remark or "无备注"
        info = CKInfo(user_id=user_id, phone=phone, ck=ck_input, remark=remark, add_time=self._now_str())
        token_key = await self._save_ck_info(info)
        try:
            await self._sync_to_qinglong(token_key, info)
        except Exception as exc:
            await self._send_text(event, f"⚠️ 数据已保存，但同步到{self._panel_name()}失败：{sanitize_error(exc)}\n\n您可以稍后重试")
            return
        await self._send_text(
            event,
            f"✅ 添加成功！\n\n🆔 UID：{token_key}\n👤 用户ID：{mask_user_id(user_id)}\n📱 手机号：{mask_phone(phone)}\n💬 备注：{remark}\n⏰ 添加时间：{info.add_time}\n\n已成功同步到{self._panel_name()}！",
        )

    async def _handle_view_my_ck(self, event: AstrMessageEvent) -> None:
        user_id = str(event.get_sender_id())
        metadata_list = await self._get_user_metadata(user_id)
        if not metadata_list:
            await self._send_text(event, "📭 您还没有上传任何CK\n\n发送「云盘登录」开始添加")
            return
        message = f"📋 您的CK列表（共{len(metadata_list)}个）：\n\n"
        for index, metadata in enumerate(metadata_list, start=1):
            ck = await self.store.get(BUCKET_TOKEN, metadata.token_key, "")
            phone = "未知"
            if ck:
                try:
                    _, phone = parse_ck_from_string(str(ck))
                except ValueError:
                    phone = "未知"
            message += (
                f"🆔 序号：{index}\n🔑 UID：{metadata.token_key}\n📱 手机号：{mask_phone(phone)}\n"
                f"💬 备注：{metadata.remark}\n------------------\n"
            )
        message += "\n💡 删除CK请发送：删除CK 序号"
        await self._send_text(event, message)

    async def _handle_delete_ck(self, event: AstrMessageEvent, message: str) -> None:
        user_id = str(event.get_sender_id())
        match = re.search(r"删除CK\s+(\d+)$", message)
        if not match:
            await self._send_text(event, "❌ 请指定要删除的序号\n\n格式：删除CK 1\n\n💡 发送「查看我的CK」查看序号")
            return
        target_index = int(match.group(1))
        metadata_list = await self._get_user_metadata(user_id)
        if not metadata_list:
            await self._send_text(event, "📭 您还没有上传任何CK")
            return
        if target_index < 1 or target_index > len(metadata_list):
            await self._send_text(event, f"❌ 序号 {target_index} 不存在\n\n您共有 {len(metadata_list)} 个CK，请输入 1-{len(metadata_list)} 之间的序号")
            return
        target = metadata_list[target_index - 1]
        deleted_ck = await self.store.get(BUCKET_TOKEN, target.token_key, "")
        await self.store.delete(BUCKET_TOKEN, target.token_key)
        new_metadata = [item for idx, item in enumerate(metadata_list) if idx != target_index - 1]
        await self._set_user_metadata(user_id, new_metadata)
        deleted_phone = "未知"
        if deleted_ck:
            try:
                _, deleted_phone = parse_ck_from_string(str(deleted_ck))
            except ValueError:
                deleted_phone = "未知"
        await self._send_text(event, f"✅ 已删除CK\n\n🔑 UID：{target.token_key}\n📱 手机号：{mask_phone(deleted_phone)}\n💬 备注：{target.remark}")

    async def _handle_show_products(self, event: AstrMessageEvent) -> None:
        products = await self._get_all_products()
        if not products:
            await self._send_text(event, "📭 商品数据为空\n\n请先发送「云盘商品更新」获取最新商品信息")
            return
        category_map: dict[str, list[ProductInfo]] = {}
        unknown_products: list[ProductInfo] = []
        for product in products.values():
            if product.category and product.category != "未知分类":
                category_map.setdefault(product.category, []).append(product)
            else:
                unknown_products.append(product)
        category_names = [name for name in CATEGORY_DISPLAY_ORDER if name in category_map]
        message = "📦 商品分类列表\n\n"
        for index, category in enumerate(category_names, start=1):
            message += f"{index}. {category} ({len(category_map[category])}个)\n"
        if unknown_products:
            message += f"\n🔍 未知分类商品 ({len(unknown_products)}个)\n"
        choice = await self._ask_text(
            event,
            message + "\n💡 请输入序号查看该分类商品详情\n💡 发送「云盘商品更新」刷新商品数据\n回复\"q\"退出\n⏱️ 60秒无响应自动退出",
        )
        if choice is None:
            return
        if self._is_cancel(choice):
            await self._send_text(event, "👋 已取消操作")
            return
        if not choice.isdigit():
            await self._send_text(event, "❌ 序号格式错误，请输入正确的数字")
            return
        selected_index = int(choice)
        if selected_index < 1 or selected_index > len(category_names):
            await self._send_text(event, "❌ 序号格式错误，请输入正确的数字")
            return
        selected_category = category_names[selected_index - 1]
        await self._show_products_in_category(event, category_map[selected_category], selected_category)
        if unknown_products:
            unknown_message = "\n\n🔍 未知分类商品：\n\n"
            for index, product in enumerate(unknown_products, start=1):
                unknown_message += f"🎁 {index}. {product.prize_name}\n💎 云朵：{product.p_order}\n🆔 商品ID：{product.prize_id}\n------------------\n"
            await self._send_text(event, unknown_message)

    async def _handle_update_products(self, event: AstrMessageEvent) -> None:
        jwt_token = await self._get_jwt_for_user(event)
        product_resp = await self.http.query_product_list(jwt_token)
        result = product_resp.get("result") or {}
        product_bucket: dict[str, Any] = {}
        total_saved = 0
        unknown_count = 0
        for category_id, prizes in result.items():
            category_name = CATEGORY_NAME_MAP.get(str(category_id), f"未知分类{category_id}")
            if not isinstance(prizes, list):
                continue
            for prize in prizes:
                if not isinstance(prize, dict):
                    continue
                prize_id = str(prize.get("memo") or "")
                if not prize_id:
                    continue
                product = ProductInfo(
                    prize_id=prize_id,
                    prize_name=str(prize.get("prizeName") or ""),
                    p_order=int(prize.get("pOrder") or 0),
                    category=category_name,
                )
                product_bucket[prize_id] = asdict(product)
                total_saved += 1
                if category_name.startswith("未知分类"):
                    unknown_count += 1
        await self.store.replace_bucket(BUCKET_PRODUCTS, product_bucket)
        await self._send_text(event, f"✅ 商品数据更新完成\n\n📊 总商品数：{total_saved}\n🔍 未知分类：{unknown_count}\n\n💡 发送「云盘商品」查看商品列表")

    async def _handle_check_my_ck(self, event: AstrMessageEvent) -> None:
        user_id = str(event.get_sender_id())
        await self._send_text(event, "🔍 开始检测您的CK...")
        total, valid, invalid_list, metadata_list = await self._check_user_cks(user_id)
        if total == 0:
            await self._send_text(event, "📭 您还没有上传任何CK\n\n发送「云盘登录」开始添加")
            return
        if invalid_list:
            await self._remove_invalid_metadata(user_id, metadata_list, invalid_list)
            message = "======云盘通知======\n"
            for index, metadata in enumerate(invalid_list, start=1):
                message += f"👤 账号: {self._get_account_label(metadata, index)}\n"
                message += "📢 消息: CK已失效: 鉴权失效\n"
                message += "====================\n"
            message += "\n💡 建议重新发送「云盘登录」更新CK"
            await self._send_text(event, message)
            return
        await self._send_text(event, f"🎉 所有CK都有效！\n\n📦 检测数量：{total}\n✅ 有效数量：{valid}")

    async def _handle_check_all_ck(self, event: AstrMessageEvent) -> None:
        admin_ids = set(self._runtime_config()["admin_user_ids"])
        if admin_ids and str(event.get_sender_id()) not in admin_ids:
            await self._send_text(event, "❌ 当前用户没有权限执行「云盘一键检测」")
            return
        await self._send_text(event, "🔍 已开始批量检测所有用户CK，任务正在后台运行，完成后会回传统计结果。")
        summary_origin = getattr(event, "unified_msg_origin", "")
        self._track_task(self._run_batch_check(summary_origin))

    async def _handle_exchange_menu(self, event: AstrMessageEvent) -> None:
        choice = await self._ask_text(
            event,
            "⚠️ 使用抢兑功能前必读\n\n请先确保：\n1️⃣ 已手动登录过移动云盘APP\n2️⃣ 打开「我的」-「云朵中心」-「福利专区」\n3️⃣ 已开启「自动备份」功能\n\n否则可能无法正常抢兑！\n\n==================\n\n请选择功能：\n1️⃣ 提交抢兑\n2️⃣ 云盘商品\n3️⃣ 清除抢兑\n4️⃣ 立即兑换\n\n💡 可直接输入数字或文字\n回复\"q\"退出\n⏱️ 60秒无响应自动退出",
        )
        if choice is None:
            return
        if self._is_cancel(choice):
            await self._send_text(event, "👋 已取消操作")
            return
        mapping = {
            "1": self._handle_submit_exchange,
            "提交抢兑": self._handle_submit_exchange,
            "2": self._handle_show_products,
            "云盘商品": self._handle_show_products,
            "3": self._handle_clear_exchange,
            "清除抢兑": self._handle_clear_exchange,
            "4": self._handle_instant_exchange,
            "立即兑换": self._handle_instant_exchange,
        }
        handler = mapping.get(choice)
        if not handler:
            await self._send_text(event, "❌ 无效的选择，请输入 1-4 或对应文字")
            return
        await handler(event)

    async def _handle_submit_exchange(self, event: AstrMessageEvent) -> None:
        user_id = str(event.get_sender_id())
        input_text = await self._ask_required_text(
            event,
            "📝 请输入商品名称或商品ID\n\n💡 商品信息可通过「云盘商品」查看\n回复\"q\"退出\n⏱️ 60秒无响应自动退出",
        )
        if input_text is None:
            return
        products = await self._get_all_products()
        target_product = None
        for product in products.values():
            if product.prize_id == input_text or product.prize_name == input_text:
                target_product = product
                break
        if target_product is None:
            await self._send_text(event, "❌ 未找到该商品\n\n💡 请检查商品名称是否完整或商品ID是否正确\n💡 发送「云盘商品更新」更新商品数据")
            return
        await self._send_text(event, f"✅ 找到商品\n\n🎁 商品名：{target_product.prize_name}\n💎 云朵：{target_product.p_order}\n🆔 商品ID：{target_product.prize_id}\n\n正在准备账号选择...")
        metadata_list = await self._get_user_metadata(user_id)
        if not metadata_list:
            await self._send_text(event, "❌ 您还没有上传任何CK\n\n请先发送「云盘登录」添加CK")
            return
        selected_index = 0
        selected_token_key = metadata_list[0].token_key
        if len(metadata_list) > 1:
            message = f"您有 {len(metadata_list)} 个账号，请选择用于抢兑的账号：\n\n"
            for index, metadata in enumerate(metadata_list, start=1):
                message += f"{index}. {self._get_account_label(metadata, index)}\n"
            choice = await self._ask_text(event, message + "\n💡 请输入序号选择\n回复\"q\"退出\n⏱️ 60秒无响应自动退出")
            if choice is None:
                return
            if self._is_cancel(choice):
                await self._send_text(event, "👋 已取消操作")
                return
            if not choice.isdigit() or int(choice) < 1 or int(choice) > len(metadata_list):
                await self._send_text(event, "❌ 序号格式错误")
                return
            selected_index = int(choice) - 1
            selected_token_key = metadata_list[selected_index].token_key
        long_term_input = await self._ask_text(
            event,
            "📌 是否设置为长期抢兑？\n\n✅ 输入 y 或 yes = 长期抢兑（需手动删除）\n❌ 输入 n 或 no 或直接回车 = 短期抢兑（成功后自动删除）\n\n回复\"q\"退出\n⏱️ 60秒无响应自动退出",
            timeout_notice=None,
        )
        if long_term_input is not None and self._is_cancel(long_term_input):
            await self._send_text(event, "👋 已取消操作")
            return
        normalized = (long_term_input or "n").strip().lower()
        is_long_term = normalized in {"y", "yes"}
        config = ExchangeConfig(
            prize_id=target_product.prize_id,
            prize_name=target_product.prize_name,
            token_key=selected_token_key,
            is_long_term=is_long_term,
            create_time=self._now_str(),
        )
        await self.store.set(BUCKET_EXCHANGE, selected_token_key, asdict(config))
        term_type = "长期" if is_long_term else "短期"
        await self._send_text(event, f"✅ 抢兑配置已保存\n\n🎁 商品：{target_product.prize_name}\n💎 云朵：{target_product.p_order}\n👤 账号：{self._get_account_label(metadata_list[selected_index], selected_index + 1)}\n⏰ 类型：{term_type}抢兑\n\n💡 发送「立即兑换」开始抢兑")

    async def _handle_clear_exchange(self, event: AstrMessageEvent) -> None:
        user_id = str(event.get_sender_id())
        metadata_list = await self._get_user_metadata(user_id)
        if not metadata_list:
            await self._send_text(event, "📭 您还没有配置任何抢兑任务")
            return
        exchange_configs: list[tuple[str, ExchangeConfig]] = []
        for metadata in metadata_list:
            config = await self._get_exchange_config(metadata.token_key)
            if config:
                exchange_configs.append((metadata.token_key, config))
        if not exchange_configs:
            await self._send_text(event, "📭 您还没有配置任何抢兑任务")
            return
        if len(exchange_configs) == 1:
            token_key, config = exchange_configs[0]
            await self.store.delete(BUCKET_EXCHANGE, token_key)
            account_label = next((self._get_account_label(item, idx + 1) for idx, item in enumerate(metadata_list) if item.token_key == token_key), "账号1")
            await self._send_text(event, f"✅ 已清除抢兑配置\n\n🎁 商品：{config.prize_name}\n👤 账号：{account_label}")
            return
        message = f"您有 {len(exchange_configs)} 个抢兑配置，请选择要删除的：\n\n"
        for index, (token_key, config) in enumerate(exchange_configs, start=1):
            account_label = next((self._get_account_label(item, idx + 1) for idx, item in enumerate(metadata_list) if item.token_key == token_key), f"账号{index}")
            term = "长期" if config.is_long_term else "短期"
            message += f"{index}. {config.prize_name} | {account_label} | {term}\n"
        choice = await self._ask_text(event, message + "\n💡 请输入序号选择\n回复\"q\"退出\n⏱️ 60秒无响应自动退出")
        if choice is None:
            return
        if self._is_cancel(choice):
            await self._send_text(event, "👋 已取消操作")
            return
        if not choice.isdigit() or int(choice) < 1 or int(choice) > len(exchange_configs):
            await self._send_text(event, "❌ 序号格式错误")
            return
        token_key, config = exchange_configs[int(choice) - 1]
        await self.store.delete(BUCKET_EXCHANGE, token_key)
        account_label = next((self._get_account_label(item, idx + 1) for idx, item in enumerate(metadata_list) if item.token_key == token_key), f"账号{choice}")
        await self._send_text(event, f"✅ 已清除抢兑配置\n\n🎁 商品：{config.prize_name}\n👤 账号：{account_label}")

    async def _print_exchange_result(self, event: AstrMessageEvent, account_label: str, prize_name: str, result_msg: str, success: bool) -> None:
        prefix = "🎉 结果" if success else "❌ 结果"
        message = "=====抢兑结果=====\n"
        message += f"👤 账号: {account_label}\n🎁 奖品: {prize_name}\n{prefix}: {result_msg}\n=================="
        await self._send_text(event, message)

    async def _handle_instant_exchange(self, event: AstrMessageEvent) -> None:
        user_id = str(event.get_sender_id())
        metadata_list = await self._get_user_metadata(user_id)
        if not metadata_list:
            await self._send_text(event, "❌ 您还没有配置任何抢兑任务\n\n请先发送「云盘抢兑」-「提交抢兑」")
            return
        exchange_configs: list[tuple[str, ExchangeConfig]] = []
        for metadata in metadata_list:
            config = await self._get_exchange_config(metadata.token_key)
            if config:
                exchange_configs.append((metadata.token_key, config))
        if not exchange_configs:
            await self._send_text(event, "❌ 您还没有配置任何抢兑任务\n\n请先发送「云盘抢兑」-「提交抢兑」")
            return
        for token_key, config in exchange_configs:
            account_label = next((self._get_account_label(item, idx + 1) for idx, item in enumerate(metadata_list) if item.token_key == token_key), "账号")
            ck = await self.store.get(BUCKET_TOKEN, token_key, "")
            if not ck:
                await self._print_exchange_result(event, account_label, config.prize_name, "CK不存在", False)
                continue
            try:
                jwt_token = await self._get_jwt_from_ck(str(ck))
                exchange_result = await self.http.do_exchange(jwt_token, config.prize_id)
            except Exception as exc:
                await self._print_exchange_result(event, account_label, config.prize_name, str(exc), False)
                continue
            prize_name = str((exchange_result.get("result") or {}).get("prizeName") or config.prize_name)
            await self._print_exchange_result(event, account_label, prize_name, "兑换成功", True)
            if not config.is_long_term:
                await self.store.delete(BUCKET_EXCHANGE, token_key)

    async def _handle_yunpan_query(self, event: AstrMessageEvent) -> None:
        user_id = str(event.get_sender_id())
        metadata_list = await self._get_user_metadata(user_id)
        if not metadata_list:
            await self._send_text(event, "❌ 您还没有上传任何CK\n\n请先发送「云盘登录」添加CK")
            return
        for index, metadata in enumerate(metadata_list, start=1):
            account_label = self._get_account_label(metadata, index)
            ck = await self.store.get(BUCKET_TOKEN, metadata.token_key, "")
            if not ck:
                await self._send_text(event, f"❌ {account_label} - CK不存在")
                continue
            try:
                _, phone = parse_ck_from_string(str(ck))
                jwt_token = await self._get_jwt_from_ck(str(ck))
            except Exception as exc:
                await self._send_text(event, f"❌ {account_label} - 获取令牌失败：{sanitize_error(exc)}")
                continue
            cloud_result, sign_result, prize_result, total_result = await asyncio.gather(
                self.http.query_cloud_record(jwt_token),
                self.http.query_sign_info(jwt_token),
                self.http.query_prize_record(jwt_token),
                self.http.query_cloud_total(jwt_token),
                return_exceptions=True,
            )
            errors: list[str] = []
            today_cloud = 0
            if isinstance(cloud_result, Exception):
                errors.append(f"云朵记录: {sanitize_error(cloud_result)}")
                cloud_data = None
            else:
                cloud_data = cloud_result
                today_cloud = self._calculate_today_cloud(cloud_data)
            if isinstance(sign_result, Exception):
                errors.append(f"签到信息: {sanitize_error(sign_result)}")
                sign_data = None
            else:
                sign_data = sign_result
            if isinstance(prize_result, Exception):
                errors.append(f"待领奖品: {sanitize_error(prize_result)}")
                prize_data = None
            else:
                prize_data = prize_result
            if isinstance(total_result, Exception):
                total_clouds = 0
            else:
                total_clouds = int((total_result.get("result") or {}).get("total") or 0)
            message = "=====账号信息=====\n"
            message += f"👤 账号: {account_label}\n📱 手机: {mask_phone(phone)}\n"
            if sign_data:
                sign_body = sign_data.get("result") or {}
                message += f"💰 当前云朵: {total_clouds}\n"
                # 处理 todaySignIn 可能是字符串或布尔值的情况
                today_sign_in = sign_body.get("todaySignIn")
                is_signed = today_sign_in is True or today_sign_in == "true" or today_sign_in == 1 or str(today_sign_in).lower() == "true"
                message += "✅ 签到状态: 已签到\n" if is_signed else "📝 签到状态: 未签到\n"
            else:
                message += f"💰 当前云朵: {total_clouds}\n📝 签到状态: 查询失败\n"
            message += f"🔥 今日云朵: {today_cloud}\n"
            prize_body = (prize_data or {}).get("result") or {}
            records = prize_body.get("result") or prize_body.get("records") or []
            if not isinstance(records, list):
                records = []
            monthly_records = [prize for prize in records if is_current_month_prize(prize.get("expireTime"))]
            if monthly_records:
                message += "🎉 本月待领奖品:\n"
                for item_index, prize in enumerate(monthly_records, start=1):
                    prize_name = prize.get("prizeName") or "未知奖品"
                    expire_text = format_expire_time(prize.get("expireTime"))
                    if expire_text:
                        message += f"   {item_index}. {prize_name}（到期时间：{expire_text}）\n"
                    else:
                        message += f"   {item_index}. {prize_name}\n"
            else:
                message += "🎉 本月待领奖品: 暂无\n"
            message += "=================="
            if errors:
                message += "\n\n⚠️ 部分信息查询失败：\n"
                for item in errors:
                    message += f"- {item}\n"
            await self._send_text(event, message)
            if index < len(metadata_list):
                await asyncio.sleep(0.5)

    def _calculate_today_cloud(self, cloud_record: dict[str, Any]) -> int:
        records = (((cloud_record or {}).get("result") or {}).get("records") or [])
        now = datetime.now().date()
        total = 0
        for record in records:
            insert_time = str(record.get("inserttime") or "")
            if not insert_time:
                continue
            try:
                parsed = datetime.fromisoformat(insert_time.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.astimezone().date() == now:
                total += int(record.get("num") or 0)
        return total

    async def _handle_account_manage(self, event: AstrMessageEvent) -> None:
        user_id = str(event.get_sender_id())
        metadata_list = await self._get_user_metadata(user_id)
        if not metadata_list:
            await self._send_text(event, "📭 您还没有上传任何CK\n\n发送「云盘登录」开始添加")
            return
        message = f"=====云盘管理=====\n📋 您的账号列表（共{len(metadata_list)}个）：\n\n"
        phone_cache: list[str] = []
        for index, metadata in enumerate(metadata_list, start=1):
            ck = await self.store.get(BUCKET_TOKEN, metadata.token_key, "")
            phone = "未知"
            if ck:
                try:
                    _, phone = parse_ck_from_string(str(ck))
                except ValueError:
                    phone = "未知"
            phone_cache.append(phone)
            message += f"{index}. 📱 {mask_phone(phone)} | 💬 {metadata.remark}\n"
        choice = await self._ask_text(event, message + "\n请输入账号序号进行管理\n回复\"q\"退出\n⏱️ 60秒无响应自动退出")
        if choice is None:
            return
        if self._is_cancel(choice):
            await self._send_text(event, "👋 已取消操作")
            return
        if not choice.isdigit() or int(choice) < 1 or int(choice) > len(metadata_list):
            await self._send_text(event, f"❌ 序号错误，请输入 1-{len(metadata_list)} 之间的数字")
            return
        index = int(choice) - 1
        selected = metadata_list[index]
        phone = phone_cache[index]
        ck = await self.store.get(BUCKET_TOKEN, selected.token_key, "")
        action = await self._ask_text(
            event,
            f"=====账号管理=====\n📱 手机号：{mask_phone(phone)}\n💬 备注：{selected.remark}\n🔑 UID：{selected.token_key}\n------------------\n\n请选择操作：\n\n1. 修改备注\n2. 更新CK（重新登录）\n3. 删除账号\n4. 查看CK详情\n\n回复\"q\"退出\n⏱️ 60秒无响应自动退出",
        )
        if action is None:
            return
        if self._is_cancel(action):
            await self._send_text(event, "👋 已取消操作")
            return
        if action == "1":
            await self._handle_modify_remark(event, user_id, index, metadata_list)
        elif action == "2":
            await self._handle_update_ck(event, user_id, index, metadata_list, phone)
        elif action == "3":
            await self._handle_delete_account(event, user_id, index, metadata_list, phone)
        elif action == "4":
            await self._handle_view_ck_detail(event, selected, phone, str(ck or ""))
        else:
            await self._send_text(event, "❌ 无效的选择，请输入 1-4")

    async def _handle_modify_remark(self, event: AstrMessageEvent, user_id: str, index: int, metadata_list: list[CKMetadata]) -> None:
        new_remark = await self._ask_required_text(
            event,
            f"当前备注：{metadata_list[index].remark}\n\n💬 请输入新的备注\n------------------\n回复\"q\"退出\n⏱️ 60秒无响应自动退出",
        )
        if new_remark is None:
            return
        old_remark = metadata_list[index].remark
        metadata_list[index].remark = new_remark
        await self._set_user_metadata(user_id, metadata_list)
        await self._send_text(event, f"✅ 备注修改成功\n\n旧备注：{old_remark}\n新备注：{new_remark}")

    async def _handle_update_ck(self, event: AstrMessageEvent, user_id: str, index: int, metadata_list: list[CKMetadata], old_phone: str) -> None:
        choice = await self._ask_text(
            event,
            "请选择更新方式：\n\n1. 短信登录（推荐）\n2. 手动输入CK\n\n回复\"q\"退出\n⏱️ 60秒无响应自动退出",
        )
        if choice is None:
            return
        if self._is_cancel(choice):
            await self._send_text(event, "👋 已取消操作")
            return
        collected = None
        if choice == "1":
            collected = await self._collect_ck_by_sms(event)
        elif choice == "2":
            collected = await self._collect_ck_manually(event)
        else:
            await self._send_text(event, "❌ 无效的选择")
            return
        if not collected:
            return
        new_ck, new_phone = collected
        token_key = metadata_list[index].token_key
        remark = metadata_list[index].remark
        await self.store.set(BUCKET_TOKEN, token_key, new_ck)
        if new_phone != old_phone:
            phones = await self._get_user_phones(user_id)
            if new_phone not in phones:
                phones.append(new_phone)
                await self._set_user_phones(user_id, phones)
        try:
            info = CKInfo(user_id=user_id, phone=new_phone, ck=new_ck, remark=remark, add_time=self._now_str())
            await self._sync_to_qinglong(token_key, info)
        except Exception as exc:
            await self._send_text(event, f"⚠️ CK已更新，但同步到{self._panel_name()}失败：{sanitize_error(exc)}")
            return
        await self._send_text(event, f"✅ CK更新成功\n\n🔑 UID：{token_key}\n📱 手机号：{mask_phone(new_phone)}\n💬 备注：{remark}\n\n已同步到{self._panel_name()}！")

    async def _handle_delete_account(self, event: AstrMessageEvent, user_id: str, index: int, metadata_list: list[CKMetadata], phone: str) -> None:
        confirm = await self._ask_text(
            event,
            f"⚠️ 确认删除该账号？\n\n📱 手机号：{mask_phone(phone)}\n💬 备注：{metadata_list[index].remark}\n\n输入 y 确认删除\n其他输入取消",
            timeout=30,
        )
        if confirm is None:
            return
        if confirm.strip().lower() not in {"y", "yes"}:
            await self._send_text(event, "👋 已取消删除")
            return
        target = metadata_list[index]
        await self.store.delete(BUCKET_TOKEN, target.token_key)
        await self.store.delete(BUCKET_EXCHANGE, target.token_key)
        new_metadata = [item for idx, item in enumerate(metadata_list) if idx != index]
        await self._set_user_metadata(user_id, new_metadata)
        await self._send_text(event, f"✅ 已删除账号\n\n📱 手机号：{mask_phone(phone)}\n💬 备注：{target.remark}")

    async def _handle_view_ck_detail(self, event: AstrMessageEvent, metadata: CKMetadata, phone: str, ck: str) -> None:
        if not ck:
            await self._send_text(event, "❌ CK数据不存在")
            return
        masked_ck = ck if len(ck) <= 40 else f"{ck[:20]}...{ck[-10:]}"
        await self._send_text(event, f"=====CK详情=====\n🔑 UID：{metadata.token_key}\n📱 手机号：{mask_phone(phone)}\n💬 备注：{metadata.remark}\n📝 CK：{masked_ck}\n📏 CK长度：{len(ck)}\n==================")

    async def _run_handler(self, event: AstrMessageEvent, handler, *args) -> None:
        await self._remember_origin(event)
        try:
            await handler(event, *args)
        except Exception as exc:
            logger.exception("移动云盘插件处理消息失败")
            await self._send_text(event, f"❌ 操作失败：{sanitize_error(exc)}")
        finally:
            event.stop_event()

    @filter.command("云盘登录")
    async def cmd_yunpan_login(self, event: AstrMessageEvent):
        await self._run_handler(event, self._handle_yunpan_login)

    @filter.command("查看我的CK")
    async def cmd_view_my_ck(self, event: AstrMessageEvent):
        await self._run_handler(event, self._handle_view_my_ck)

    @filter.command("删除CK")
    async def cmd_delete_ck(self, event: AstrMessageEvent, index: int):
        await self._run_handler(event, self._handle_delete_ck, f"删除CK {index}")

    @filter.command("云盘商品")
    async def cmd_show_products(self, event: AstrMessageEvent):
        await self._run_handler(event, self._handle_show_products)

    @filter.command("云盘商品更新")
    async def cmd_update_products(self, event: AstrMessageEvent):
        await self._run_handler(event, self._handle_update_products)

    @filter.command("云盘检测")
    async def cmd_check_my_ck(self, event: AstrMessageEvent):
        await self._run_handler(event, self._handle_check_my_ck)

    @filter.command("云盘一键检测")
    async def cmd_check_all_ck(self, event: AstrMessageEvent):
        await self._run_handler(event, self._handle_check_all_ck)

    @filter.command("云盘抢兑")
    async def cmd_exchange_menu(self, event: AstrMessageEvent):
        await self._run_handler(event, self._handle_exchange_menu)

    @filter.command("立即兑换")
    async def cmd_instant_exchange(self, event: AstrMessageEvent):
        await self._run_handler(event, self._handle_instant_exchange)

    @filter.command("云盘查询")
    async def cmd_yunpan_query(self, event: AstrMessageEvent):
        await self._run_handler(event, self._handle_yunpan_query)

    @filter.command("云盘管理")
    async def cmd_account_manage(self, event: AstrMessageEvent):
        await self._run_handler(event, self._handle_account_manage)
