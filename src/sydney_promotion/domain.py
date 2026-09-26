"""悉尼中国文化中心江苏十三市推介协作库的领域对象、常量与时间约定。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

# 江苏十三市:展项归属与发布窗口都按城市划分
CITIES = ("南京", "无锡", "徐州", "常州", "苏州", "南通", "连云港",
          "淮安", "盐城", "扬州", "镇江", "泰州", "宿迁")

# 展项类型:影像、非遗作品、活动说明
KINDS = ("image", "heritage", "activity")

# 支持的语言版本,中文为原始稿件,其余语种的译稿可先后到达
LANGUAGES = ("zh", "en", "ja", "ko", "fr", "de", "es", "ru")
BASE_LANGUAGE = "zh"

EXHIBIT_STATES = ("draft", "submitted", "approved", "archived")
TRANSLATION_STATES = ("draft", "submitted", "confirmed")
GRANT_STATES = ("active", "revoked")
WINDOW_STATES = ("open", "closed")
CHANGE_STATES = ("pending", "confirmed", "rejected")
ORG_ROLES = ("center", "partner")


class ServiceError(Exception):
    """业务错误基类,status 供 HTTP 边界映射状态码。"""
    status = 400


class NotFoundError(ServiceError):
    status = 404


class ForbiddenError(ServiceError):
    status = 403


class ConflictError(ServiceError):
    """版本或幂等键冲突。"""
    status = 409


class StateError(ServiceError):
    """当前状态不允许该操作。"""
    status = 422


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value) -> str:
    """把外部时间(允许 Z 结尾或裸时间)规范为 UTC ISO 字符串,保证可比较。"""
    if value is None:
        raise ServiceError("缺少时间字段")
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ServiceError(f"时间格式无效: {value}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def content_checksum(payload: dict) -> str:
    """对发布内容生成可核验的 SHA-256 摘要。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Record:
    """通用记录(保留早期骨架能力)。"""
    record_id: str
    owner_id: str
    state: str
    version: int
    updated_at: str


@dataclass(frozen=True)
class Exhibit:
    """展项:某城市一件影像/非遗作品/活动说明的中文原稿与状态。"""
    exhibit_id: str
    city: str
    kind: str
    owner_org: str
    title: str
    body: str
    state: str
    version: int
    updated_at: str


@dataclass(frozen=True)
class Translation:
    """翻译稿:某展项某一语种的译文,独立版本与确认状态。"""
    exhibit_id: str
    language: str
    text: str
    state: str
    version: int
    updated_at: str


@dataclass(frozen=True)
class Grant:
    """授权:主办方把某展项在有效期内授权给一家合作机构使用。"""
    grant_id: str
    org_id: str
    exhibit_id: str
    valid_from: str
    valid_until: str
    state: str
    created_at: str


@dataclass(frozen=True)
class ChangeConfirmation:
    """修改待确认:展项内容变化后,需被授权方确认才能继续发布。"""
    change_id: str
    exhibit_id: str
    org_id: str
    from_version: int
    to_version: int
    state: str
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True)
class Window:
    """发布窗口:某城市允许生成发布包的时间段。"""
    window_id: str
    city: str
    opens_at: str
    closes_at: str
    state: str


@dataclass(frozen=True)
class HandoffItemState:
    handoff_id: str
    exhibit_id: str
    language: str
    state: str
    updated_at: str
