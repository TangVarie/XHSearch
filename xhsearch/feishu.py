"""飞书多维表格读写。

用企业自建应用 + tenant_access_token，不用扣子的官方飞书插件——后者走的是
用户级 OAuth，绑在某个具体人的飞书身份上，无人值守的定时任务里那个人离职、
改密码或撤销授权，整条链路就断了，而且断得很安静。

上线前必做的两件事，漏了必报错：
1. 应用要开 bitable:app 权限
2. 要在**那张多维表格**里「添加文档应用」把这个应用加成协作者
   （只发布应用不加协作者 → 99991672 权限错误）
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from . import transport

BASE = "https://open.feishu.cn/open-apis"

# 飞书文档给的批量写上限是 1000，这里取一半。批量操作是全成功或全失败，
# 一批越大，一个坏值导致的回滚面就越大。
BATCH_SIZE = 500


class FeishuError(RuntimeError):
    def __init__(self, code: int, msg: str, hint: str = ""):
        self.code = code
        self.msg = msg
        super().__init__(f"飞书接口报错 [{code}] {msg}" + (f"\n → {hint}" if hint else ""))


# 高频踩坑的错误码，直接把解法写进异常里，省得回头翻文档。
_HINTS = {
    10003: "参数不合法：多半是 app_id / app_secret 填错或填串了（实测过的返回）",
    99991663: "app_id / app_secret 不对，或应用还没发布",
    99991672: "应用没有这张多维表格的权限：去表格右上角「…」→「添加文档应用」把应用加成协作者",
    1254005: "record_id 不存在，可能这行已被删除",
    1254045: "字段名对不上：config.py 里的列名必须和表头逐字相同（含空格、括号全半角）",
    1254060: "字段值类型不对：数字列不能写字符串，日期列要写毫秒时间戳",
    1254291: "写入的多选选项在字段配置里不存在，先把选项建好",
    91403: "没有权限，检查应用权限范围和文档协作者设置",
}


def _check(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise FeishuError(-1, f"响应不是 JSON：{str(payload)[:200]}")
    code = payload.get("code")
    if code not in (0, None):
        raise FeishuError(int(code), str(payload.get("msg", "")), _HINTS.get(int(code), ""))
    return payload.get("data") or {}


@dataclass
class _Token:
    value: str
    expires_at: float


class Bitable:
    """一张多维表格的读写客户端。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        app_token: str,
        table_id: str,
        *,
        timeout: float = 30.0,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self.table_id = table_id
        self.timeout = timeout
        self._token: Optional[_Token] = None

    # ---------- 鉴权 ----------

    def token(self) -> str:
        """tenant_access_token，有效期 7200 秒，提前 5 分钟续。

        飞书对重复请求会返回同一个未过期的 token，所以这里不需要跨进程缓存，
        每次冷启动重新取一次是安全的。
        """
        now = time.monotonic()
        if self._token and self._token.expires_at > now:
            return self._token.value

        resp = transport.post(
            f"{BASE}/auth/v3/tenant_access_token/internal",
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}),
            timeout=self.timeout,
        )
        payload = resp.json()
        if not isinstance(payload, dict):
            raise FeishuError(-1, f"取 token 失败：HTTP {resp.status} {resp.body[:200]}")
        code = payload.get("code")
        if code not in (0, None):
            raise FeishuError(int(code), str(payload.get("msg", "")), _HINTS.get(int(code), ""))

        value = payload.get("tenant_access_token")
        if not value:
            raise FeishuError(-1, f"响应里没有 tenant_access_token：{resp.body[:200]}")
        expire = int(payload.get("expire", 7200))
        self._token = _Token(value, now + max(expire - 300, 60))
        return value

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _url(self, suffix: str) -> str:
        return f"{BASE}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/{suffix}"

    # ---------- 读 ----------

    def search(
        self,
        field_names: Iterable[str],
        *,
        filter_spec: Optional[dict[str, Any]] = None,
        page_size: int = 200,
        max_records: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """按条件拉记录，自动翻页。

        field_names 一定要把「流量状态」和「评论数」带上——前者是合并多选的前提，
        后者是判定掉量的基线。少读一个就会写错。
        """
        collected: list[dict[str, Any]] = []
        page_token = ""
        fields = list(field_names)

        while True:
            url = self._url(f"records/search?page_size={min(page_size, 500)}")
            if page_token:
                url += f"&page_token={page_token}"
            body: dict[str, Any] = {"field_names": fields, "automatic_fields": False}
            if filter_spec:
                body["filter"] = filter_spec

            resp = transport.post_with_retry(
                url,
                self._headers(),
                json.dumps(body, ensure_ascii=False),
                timeout=self.timeout,
                should_retry=lambda r: r.status == 0 or r.status >= 500 or r.status == 429,
            )
            data = _check(resp.json())
            collected.extend(data.get("items") or [])

            if max_records is not None and len(collected) >= max_records:
                return collected[:max_records]
            if not data.get("has_more"):
                return collected
            page_token = data.get("page_token") or ""
            if not page_token:
                return collected

    # ---------- 写 ----------

    def batch_update(self, updates: list[dict[str, Any]]) -> int:
        """批量更新。updates 形如 [{"record_id": "...", "fields": {...}}, ...]

        串行发，不并行：飞书官方建议同一时刻对同一篇多维表格只做一个写操作。
        并行写会拿到 1254607 之类的并发冲突，还可能写坏。
        """
        written = 0
        for start in range(0, len(updates), BATCH_SIZE):
            chunk = updates[start : start + BATCH_SIZE]
            resp = transport.post_with_retry(
                self._url("records/batch_update"),
                self._headers(),
                json.dumps({"records": chunk}, ensure_ascii=False),
                timeout=self.timeout,
                should_retry=lambda r: r.status == 0 or r.status >= 500 or r.status == 429,
            )
            _check(resp.json())
            written += len(chunk)
        return written

    def list_field_options(self, field_name: str) -> Optional[list[str]]:
        """读某个单选/多选字段已配置的选项，读不到返回 None。

        用来在写回前过滤掉字段里不存在的标签——batch_update 是全成功或全失败，
        一个没建过的选项名可能让整批几百行一起回滚。

        返回 None 表示「查不到，别过滤」而不是「没有选项」，两者必须区分：
        前者应放行（宁可试着写），后者应全部拦下。
        """
        resp = transport.get(
            self._url("fields?page_size=200"),
            self._headers(),
            timeout=self.timeout,
        )
        payload = resp.json()
        if not isinstance(payload, dict) or payload.get("code") not in (0, None):
            return None

        data = payload.get("data") or {}
        for field in data.get("items") or []:
            if field.get("field_name") == field_name:
                options = ((field.get("property") or {}).get("options")) or []
                return [o["name"] for o in options if isinstance(o, dict) and o.get("name")]
        return None


# ---------- 单元格取值 / 赋值 ----------


def read_text(value: Any) -> str:
    """多维表格的文本列读出来可能是字符串，也可能是富文本分段数组。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("name") or ""))
        return "".join(parts)
    if isinstance(value, dict):
        # 链接列形如 {"link": "...", "text": "..."}
        return str(value.get("link") or value.get("text") or "")
    return str(value)


def read_multi_select(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and item.get("name"):
                out.append(str(item["name"]))
        return out
    return []


def read_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def read_bool(value: Any) -> bool:
    return bool(value) if isinstance(value, bool) else str(value).lower() in ("true", "1", "是")


def read_timestamp_ms(value: Any) -> Optional[int]:
    """日期列读出来是毫秒时间戳。"""
    number = read_int(value)
    if number is None:
        return None
    # 有些表把日期存成秒，宽松兼容一下。
    return number * 1000 if number < 10_000_000_000 else number
