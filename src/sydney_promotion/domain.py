"""悉尼文旅推介协作库中的基础对象与时间约定。

业务背景：悉尼中国文化中心把江苏十三座城市的影像、非遗作品和活动
说明交给多家合作机构使用。资料有不同语言版本，授权有生效/失效窗口，
因此领域层统一定义城市、语言、状态和时间格式，避免各模块各写一份。
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

# 江苏十三座设区市，顺序固定，清单与校验都以此为准
JIANGSU_CITIES = (
    "南京", "无锡", "徐州", "常州", "苏州",
    "南通", "连云港", "淮安", "盐城", "扬州",
    "镇江", "泰州", "宿迁",
)

# 资料在不同语言版本之间先后到达，先登记服务方实际会用到的语种
LANGUAGES = ("zh", "en", "fr", "es", "ar")

LANGUAGE_LABELS = {
    "zh": "中文",
    "en": "English",
    "fr": "Français",
    "es": "Español",
    "ar": "العربية",
}

# 展项工作状态：draft(编辑中) -> pending(待对方确认) -> approved(已定稿)
# approved 之后如内容再改，由提交者决定是否重新进入 pending。
RECORD_STATES = ("draft", "pending", "approved", "cancelled", "closed")

# 翻译稿状态：submitted 已提交，confirmed 对方确认，changes_requested 要求修改
TRANSLATION_STATES = ("submitted", "confirmed", "changes_requested")

# 授权状态
GRANT_STATES = ("active", "revoked", "expired")

# 授权使用范围（渠道）：发布包按条目声明的渠道逐一校验
SCOPES = ("web", "print", "screen", "social")


class ServiceError(Exception):
    """业务规则被违反；code 供 HTTP 层映射状态码。"""

    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    """把外部传入的时间解析为带 UTC 时区的 datetime；仅接受 ISO 8601。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise ServiceError(f"时间格式无效: {value}", 400)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    city: str
    kind: str
    title: str
    state: str
    version: int
    payload: dict = field(default_factory=dict)
    updated_at: str = ""


@dataclass(frozen=True)
class Translation:
    record_id: str
    language: str
    owner_id: str
    state: str
    version: int
    content: dict = field(default_factory=dict)
    updated_at: str = ""


@dataclass(frozen=True)
class Grant:
    grant_id: str
    record_id: str
    org_id: str
    scope: tuple
    state: str
    valid_from: str
    valid_until: str
    version: int
    updated_at: str = ""


@dataclass(frozen=True)
class ReleasePackage:
    package_id: str
    request_key: str
    org_id: str
    items: tuple
    quota_charged: int
    created_at: str

    def to_dict(self):
        data = asdict(self)
        data["items"] = list(self.items)
        return data
