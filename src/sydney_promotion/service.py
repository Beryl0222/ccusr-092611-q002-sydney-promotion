"""悉尼文旅推介协作库的持久化边界和协作服务。

一条主线：多家合作机构围绕江苏十三城的展项协作，服务必须保证——

* 隔离：机构只能操作自己负责的展项与翻译稿，授权与配额由中心签发；
* 版本：并行编辑带 expected_version，冲突时按三方合并处理，合不进就拒绝；
* 授权：授权有生效窗口，过期或已撤回的授权不能再生成发布包；
* 幂等：同一 request_key 重放返回同一结果，配额只扣一次；
* 恢复：每个机构有交接 outbox 和游标，离线后可从未确认位置继续；
* 清单：按城市、语言生成带哈希的最终清单，供双方离线核验。

所有写操作在同一个 ``BEGIN IMMEDIATE`` 事务里完成，失败整体回滚。
"""
import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager

from .domain import (
    GRANT_STATES,
    JIANGSU_CITIES,
    LANGUAGES,
    RECORD_STATES,
    SCOPES,
    TRANSLATION_STATES,
    Grant,
    Record,
    ReleasePackage,
    Translation,
    parse_time,
    utc_now,
)
from .domain import ServiceError  #  re-export，保持 ``from .service import ServiceError``

CENTER_ID = "center"  # 主办方身份，拥有签发/撤回/确认等管理权限


def canonical_json(value):
    """排序键、无空白的 JSON，作为哈希与快照的稳定序列化形式。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class DomainStore:
    def __init__(self, database=":memory:", clock=utc_now, admins=(CENTER_ID,)):
        # HTTP 边界是多线程的：允许跨线程复用同一连接，并用锁串行化写事务，
        # 配合 BEGIN IMMEDIATE 保证并行请求下的调度顺序确定。
        # isolation_level=None 让驱动不要隐式开事务——所有事务都由本类
        # 显式 BEGIN/COMMIT 管理，避免“事务中再开事务”。
        self.connection = sqlite3.connect(
            database, check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = clock
        self.admins = set(admins)
        self._tx_lock = threading.Lock()
        self.connection.executescript(
            """
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS records(
              record_id TEXT PRIMARY KEY,
              owner_id  TEXT NOT NULL,
              city      TEXT,
              kind      TEXT NOT NULL,
              title     TEXT NOT NULL,
              state     TEXT NOT NULL,
              version   INTEGER NOT NULL,
              payload   TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS record_versions(
              record_id TEXT NOT NULL,
              version   INTEGER NOT NULL,
              snapshot  TEXT NOT NULL,
              editor_id TEXT NOT NULL,
              merged    INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              PRIMARY KEY(record_id, version)
            );

            CREATE TABLE IF NOT EXISTS translations(
              record_id TEXT NOT NULL,
              language  TEXT NOT NULL,
              owner_id  TEXT NOT NULL,
              state     TEXT NOT NULL,
              version   INTEGER NOT NULL,
              content   TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(record_id, language)
            );

            CREATE TABLE IF NOT EXISTS grants(
              grant_id TEXT PRIMARY KEY,
              record_id TEXT NOT NULL,
              org_id   TEXT NOT NULL,
              scope    TEXT NOT NULL,
              state    TEXT NOT NULL,
              valid_from TEXT NOT NULL,
              valid_until TEXT NOT NULL,
              version  INTEGER NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS quotas(
              org_id TEXT PRIMARY KEY,
              "limit" INTEGER NOT NULL,
              used   INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS releases(
              request_key TEXT PRIMARY KEY,
              package_id  TEXT NOT NULL UNIQUE,
              org_id      TEXT NOT NULL,
              items       TEXT NOT NULL,
              quota_charged INTEGER NOT NULL,
              created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outbox(
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              org_id TEXT NOT NULL,
              op     TEXT NOT NULL,
              payload TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS handshakes(
              handshake_id TEXT PRIMARY KEY,
              org_id TEXT NOT NULL,
              cursor_seq INTEGER NOT NULL DEFAULT 0,
              state  TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_open_handshake
              ON handshakes(org_id) WHERE state='open';

            CREATE TABLE IF NOT EXISTS events(
              event_id TEXT PRIMARY KEY,
              record_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              body TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS idempotency(
              request_key TEXT PRIMARY KEY,
              result TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    # -- 基础事务 / 工具 ---------------------------------------------------

    @contextmanager
    def transaction(self):
        with self._tx_lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def _now(self):
        return parse_time(self.clock())

    def _replay(self, request_key):
        if request_key is None:
            return None
        row = self.connection.execute(
            "SELECT result FROM idempotency WHERE request_key=?", (request_key,)
        ).fetchone()
        return json.loads(row["result"]) if row else None

    def _remember(self, request_key, result):
        if request_key is not None:
            self.connection.execute(
                "INSERT OR REPLACE INTO idempotency VALUES(?,?)",
                (request_key, canonical_json(result)),
            )

    def _event(self, record_id, kind, body):
        self.connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?)",
            (f"{record_id}:{kind}:{uuid.uuid4().hex}", record_id, kind,
             canonical_json(body), self.clock()),
        )

    def _outbox(self, org_id, op, payload):
        self.connection.execute(
            "INSERT INTO outbox(org_id,op,payload,created_at) VALUES(?,?,?,?)",
            (org_id, op, canonical_json(payload), self.clock()),
        )

    def _is_admin(self, actor):
        return actor in self.admins

    def _require_admin(self, actor):
        if not self._is_admin(actor):
            raise ServiceError("仅主办方可执行该操作", 403)

    def _record_row(self, record_id):
        row = self.connection.execute(
            "SELECT * FROM records WHERE record_id=?", (record_id,)
        ).fetchone()
        if row is None:
            raise ServiceError("展项不存在", 404)
        return row

    @staticmethod
    def _record_from_row(row):
        return Record(
            row["record_id"], row["owner_id"], row["city"], row["kind"],
            row["title"], row["state"], row["version"],
            json.loads(row["payload"]), row["updated_at"],
        )

    # -- 展项 --------------------------------------------------------------

    def create(self, record_id, owner_id, payload=None, city=None,
               kind="exhibit", title=""):
        payload = dict(payload or {})
        if city is not None and city not in JIANGSU_CITIES:
            raise ServiceError(f"未知城市: {city}", 400)
        now = self.clock()
        with self.transaction():
            exists = self.connection.execute(
                "SELECT 1 FROM records WHERE record_id=?", (record_id,)
            ).fetchone()
            if exists:
                raise ServiceError("展项已存在", 409)
            self.connection.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)",
                (record_id, owner_id, city, kind, title, "draft", 1,
                 canonical_json(payload), now),
            )
            self.connection.execute(
                "INSERT INTO record_versions VALUES(?,?,?,?,?,?)",
                (record_id, 1, canonical_json(payload), owner_id, 0, now),
            )
            self._event(record_id, "created", {"owner_id": owner_id, "city": city})
            self._outbox(owner_id, "record_created",
                         {"record_id": record_id, "city": city, "title": title})
        return self.get(record_id)

    def get(self, record_id):
        return self._record_from_row(self._record_row(record_id))

    def transition(self, record_id, owner_id, target, request_key,
                   expected_version=None):
        """展项状态迁移，保留给最早版本的调用方与简单审批流使用。"""
        with self.transaction():
            old = self._record_row(record_id)
            if old["owner_id"] != owner_id and not self._is_admin(owner_id):
                raise ServiceError("无权操作", 403)
            cached = self._replay(request_key)
            if cached is not None:
                return cached
            if expected_version is not None and old["version"] != expected_version:
                raise ServiceError("版本冲突", 409)
            allowed = {
                "draft": {"pending"},
                "pending": {"approved", "cancelled"},
                "approved": {"closed"},
                "cancelled": set(),
                "closed": set(),
            }
            if target not in allowed.get(old["state"], set()):
                raise ServiceError("状态迁移不允许", 409)
            version = old["version"] + 1
            now = self.clock()
            self.connection.execute(
                "UPDATE records SET state=?,version=?,updated_at=? WHERE record_id=?",
                (target, version, now, record_id),
            )
            # 状态迁移同样产生一个版本快照，后续编辑可以把它当作合并基准
            self.connection.execute(
                "INSERT INTO record_versions VALUES(?,?,?,?,?,?)",
                (record_id, version, old["payload"], owner_id, 0, now),
            )
            self._event(record_id, "transition",
                        {"from": old["state"], "to": target, "version": version})
            self._outbox(old["owner_id"], "record_transition",
                         {"record_id": record_id, "state": target, "version": version})
            result = {"record_id": record_id, "state": target, "version": version}
            self._remember(request_key, result)
            return result

    def edit_record(self, record_id, editor_id, changes, expected_version,
                    request_key=None):
        """带乐观锁的字段编辑。

        调用方提交自己读过的 ``expected_version``：
        * 期间无人改过 —— 直接提交一个新版本；
        * 期间有人提交过 —— 用 v(expected) 作 base 做三方合并，双方改动的
          键不重叠则自动合并，重叠且取值不同则整单拒绝（409）。
        """
        if not isinstance(changes, dict) or not changes:
            raise ServiceError("changes 必须是非空对象", 400)
        if expected_version is None:
            raise ServiceError("缺少 expected_version", 400)
        with self.transaction():
            row = self._record_row(record_id)
            if row["owner_id"] != editor_id and not self._is_admin(editor_id):
                raise ServiceError("无权操作该展项", 403)
            cached = self._replay(request_key)
            if cached is not None:
                return cached
            if row["state"] in ("cancelled", "closed"):
                raise ServiceError("该展项已终结，不能再编辑", 409)

            base = self.connection.execute(
                "SELECT snapshot FROM record_versions WHERE record_id=? AND version=?",
                (record_id, expected_version),
            ).fetchone()
            if base is None:
                raise ServiceError("基准版本不存在", 409)

            current_payload = json.loads(row["payload"])
            base_payload = json.loads(base["snapshot"])
            merged_payload = dict(current_payload)
            conflicts = []
            for key, their_value in changes.items():
                our_value = current_payload.get(key)
                base_value = base_payload.get(key)
                they_changed = base_value != their_value
                we_changed = base_value != our_value
                if they_changed and we_changed and our_value != their_value:
                    conflicts.append(key)
                elif they_changed:
                    merged_payload[key] = their_value
            if conflicts:
                raise ServiceError(
                    f"字段与他人修改冲突: {', '.join(sorted(conflicts))}", 409)

            is_merge = row["version"] != expected_version
            new_version = row["version"] + 1
            new_state = "pending" if row["state"] == "approved" else row["state"]
            now = self.clock()
            self.connection.execute(
                "UPDATE records SET payload=?,state=?,version=?,updated_at=? "
                "WHERE record_id=?",
                (canonical_json(merged_payload), new_state, new_version, now,
                 record_id),
            )
            self.connection.execute(
                "INSERT INTO record_versions VALUES(?,?,?,?,?,?)",
                (record_id, new_version, canonical_json(merged_payload),
                 editor_id, int(is_merge), now),
            )
            self._event(record_id, "edited", {
                "editor_id": editor_id, "base_version": expected_version,
                "version": new_version, "merged": is_merge,
                "keys": sorted(changes), "reset_to_pending": new_state == "pending",
            })
            self._outbox(row["owner_id"], "record_edited", {
                "record_id": record_id, "version": new_version,
                "merged": is_merge, "editor_id": editor_id,
            })
            result = {
                "record_id": record_id, "version": new_version,
                "state": new_state, "merged": is_merge,
                "payload": merged_payload,
            }
            self._remember(request_key, result)
            return result

    # -- 翻译稿 ------------------------------------------------------------

    def _translation_row(self, record_id, language):
        if language not in LANGUAGES:
            raise ServiceError(f"不支持的语言: {language}", 400)
        row = self.connection.execute(
            "SELECT * FROM translations WHERE record_id=? AND language=?",
            (record_id, language),
        ).fetchone()
        if row is None:
            raise ServiceError("翻译稿不存在", 404)
        return row

    @staticmethod
    def _translation_from_row(row):
        return Translation(
            row["record_id"], row["language"], row["owner_id"], row["state"],
            row["version"], json.loads(row["content"]), row["updated_at"],
        )

    def submit_translation(self, record_id, language, owner_id, content,
                           expected_version=None, request_key=None):
        """负责机构提交/重交翻译稿；每次提交回到 submitted 等待对方确认。"""
        if language not in LANGUAGES:
            raise ServiceError(f"不支持的语言: {language}", 400)
        if not isinstance(content, dict) or not content:
            raise ServiceError("content 必须是非空对象", 400)
        with self.transaction():
            record = self._record_row(record_id)
            if record["owner_id"] != owner_id and not self._is_admin(owner_id):
                raise ServiceError("无权为该展项提交翻译稿", 403)
            cached = self._replay(request_key)
            if cached is not None:
                return cached
            if record["state"] in ("cancelled", "closed"):
                raise ServiceError("该展项已终结，不能再提交翻译稿", 409)

            row = self.connection.execute(
                "SELECT * FROM translations WHERE record_id=? AND language=?",
                (record_id, language),
            ).fetchone()
            if row is not None:
                if expected_version is not None and row["version"] != expected_version:
                    raise ServiceError("翻译稿版本冲突", 409)
                version = row["version"] + 1
            else:
                if expected_version not in (None, 0):
                    raise ServiceError("翻译稿版本冲突", 409)
                version = 1
            now = self.clock()
            self.connection.execute(
                "INSERT INTO translations VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(record_id,language) DO UPDATE SET "
                "state=excluded.state,version=excluded.version,"
                "content=excluded.content,updated_at=excluded.updated_at",
                (record_id, language, owner_id, "submitted", version,
                 canonical_json(content), now),
            )
            self._event(record_id, "translation_submitted",
                        {"language": language, "version": version})
            self._outbox(record["owner_id"], "translation_submitted",
                         {"record_id": record_id, "language": language,
                          "version": version})
            result = {"record_id": record_id, "language": language,
                      "state": "submitted", "version": version}
            self._remember(request_key, result)
            return result

    def review_translation(self, record_id, language, reviewer_id, approve,
                           note=None, request_key=None):
        """主办方确认翻译稿或退回要求修改。机构不能确认自己的稿件。"""
        target_state = "confirmed" if approve else "changes_requested"
        with self.transaction():
            record = self._record_row(record_id)
            self._require_admin(reviewer_id)
            row = self._translation_row(record_id, language)
            cached = self._replay(request_key)
            if cached is not None:
                return cached
            version = row["version"] + 1
            now = self.clock()
            self.connection.execute(
                "UPDATE translations SET state=?,version=?,updated_at=? "
                "WHERE record_id=? AND language=?",
                (target_state, version, now, record_id, language),
            )
            self._event(record_id, "translation_reviewed",
                        {"language": language, "state": target_state, "note": note})
            self._outbox(record["owner_id"], "translation_reviewed",
                         {"record_id": record_id, "language": language,
                          "state": target_state, "note": note})
            result = {"record_id": record_id, "language": language,
                      "state": target_state, "version": version}
            self._remember(request_key, result)
            return result

    def get_translation(self, record_id, language):
        return self._translation_from_row(self._translation_row(record_id, language))

    # -- 授权与发布窗口 -----------------------------------------------------

    def issue_grant(self, grant_id, record_id, org_id, scope, valid_from,
                    valid_until, issuer_id=CENTER_ID, request_key=None):
        """主办方为某机构就某展项签发带使用范围与时间窗口的授权。"""
        self._require_admin(issuer_id)
        scope = tuple(scope) if not isinstance(scope, str) else (scope,)
        unknown = [s for s in scope if s not in SCOPES]
        if unknown:
            raise ServiceError(f"未知授权范围: {', '.join(unknown)}", 400)
        start, end = parse_time(valid_from), parse_time(valid_until)
        if end <= start:
            raise ServiceError("授权结束时间必须晚于开始时间", 400)
        with self.transaction():
            self._record_row(record_id)
            cached = self._replay(request_key)
            if cached is not None:
                return cached
            if self.connection.execute(
                "SELECT 1 FROM grants WHERE grant_id=?", (grant_id,)
            ).fetchone():
                raise ServiceError("授权已存在", 409)
            now = self.clock()
            self.connection.execute(
                "INSERT INTO grants VALUES(?,?,?,?,?,?,?,?,?)",
                (grant_id, record_id, org_id, canonical_json(list(scope)), "active",
                 start.isoformat(), end.isoformat(), 1, now),
            )
            self._event(record_id, "grant_issued",
                        {"grant_id": grant_id, "org_id": org_id, "scope": list(scope)})
            self._outbox(org_id, "grant_issued", {
                "grant_id": grant_id, "record_id": record_id,
                "scope": list(scope),
                "valid_from": start.isoformat(), "valid_until": end.isoformat(),
            })
            result = {"grant_id": grant_id, "record_id": record_id,
                      "org_id": org_id, "state": "active", "version": 1}
            self._remember(request_key, result)
            return result

    def revoke_grant(self, grant_id, reviewer_id=CENTER_ID, request_key=None):
        with self.transaction():
            self._require_admin(reviewer_id)
            row = self.connection.execute(
                "SELECT * FROM grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if row is None:
                raise ServiceError("授权不存在", 404)
            cached = self._replay(request_key)
            if cached is not None:
                return cached
            if row["state"] != "active":
                raise ServiceError("授权已失效，无需撤回", 409)
            version = row["version"] + 1
            now = self.clock()
            self.connection.execute(
                "UPDATE grants SET state='revoked',version=?,updated_at=? "
                "WHERE grant_id=?", (version, now, grant_id),
            )
            self._event(row["record_id"], "grant_revoked",
                        {"grant_id": grant_id, "org_id": row["org_id"]})
            self._outbox(row["org_id"], "grant_revoked",
                         {"grant_id": grant_id, "record_id": row["record_id"]})
            result = {"grant_id": grant_id, "state": "revoked", "version": version}
            self._remember(request_key, result)
            return result

    def _sweep_expired(self, at):
        """把窗口已过但仍标 active 的授权翻成 expired，并通知机构。

        过期是时间推进造成的客观事实，必须独立于调用方的业务事务提交：
        即便调用方随后因授权无效而失败回滚，过期状态也不能被带回 active。
        """
        with self.transaction():
            rows = self.connection.execute(
                "SELECT * FROM grants WHERE state='active' AND valid_until<=?",
                (at.isoformat(),),
            ).fetchall()
            for row in rows:
                self.connection.execute(
                    "UPDATE grants SET state='expired',version=version+1,"
                    "updated_at=? WHERE grant_id=?",
                    (self.clock(), row["grant_id"]),
                )
                self._event(row["record_id"], "grant_expired",
                            {"grant_id": row["grant_id"], "org_id": row["org_id"]})
                self._outbox(row["org_id"], "grant_expired",
                             {"grant_id": row["grant_id"],
                              "record_id": row["record_id"]})
        return rows

    def _effective_grant(self, record_id, org_id, channel, at):
        """返回此刻有效的授权；过期或撤回一律视为无授权。"""
        row = self.connection.execute(
            "SELECT * FROM grants WHERE record_id=? AND org_id=? AND state='active' "
            "AND valid_from<=? AND valid_until>?",
            (record_id, org_id, at.isoformat(), at.isoformat()),
        ).fetchone()
        if row is None:
            return None
        if channel not in json.loads(row["scope"]):
            raise ServiceError(
                f"授权 {row['grant_id']} 不含使用范围 {channel}", 403)
        return row

    def get_grant(self, grant_id):
        row = self.connection.execute(
            "SELECT * FROM grants WHERE grant_id=?", (grant_id,)
        ).fetchone()
        if row is None:
            raise ServiceError("授权不存在", 404)
        return self._grant_from_row(row)

    def list_grants(self, record_id=None, org_id=None):
        sql, params = "SELECT * FROM grants WHERE 1=1", []
        if record_id:
            sql += " AND record_id=?"; params.append(record_id)
        if org_id:
            sql += " AND org_id=?"; params.append(org_id)
        sql += " ORDER BY grant_id"
        return [self._grant_from_row(r)
                for r in self.connection.execute(sql, params).fetchall()]

    @staticmethod
    def _grant_from_row(row):
        return Grant(
            row["grant_id"], row["record_id"], row["org_id"],
            tuple(json.loads(row["scope"])), row["state"],
            row["valid_from"], row["valid_until"], row["version"], row["updated_at"],
        )

    # -- 配额与发布包 -------------------------------------------------------

    def set_quota(self, org_id, limit, issuer_id=CENTER_ID):
        self._require_admin(issuer_id)
        if limit < 0:
            raise ServiceError("配额不能为负", 400)
        with self.transaction():
            self.connection.execute(
                "INSERT INTO quotas(org_id,\"limit\",used) VALUES(?,?,0) "
                "ON CONFLICT(org_id) DO UPDATE SET \"limit\"=excluded.\"limit\"",
                (org_id, limit),
            )

    def create_release(self, request_key, org_id, items, at=None, channel="web"):
        """生成一批发布包。

        同一 ``request_key`` 重放（网络重试、伙伴重新提交同一批）直接返回
        原包，配额不二次扣减。每个条目都要求：展项已定稿、该语言翻译稿已
        确认、授权在发布时刻有效且覆盖使用渠道。
        """
        if not request_key:
            raise ServiceError("缺少 request_key", 400)
        if not items:
            raise ServiceError("发布条目为空", 400)
        at = parse_time(at) if at else self._now()
        # 过期判定独立提交，避免随后失败的发布事务把过期状态回滚
        self._sweep_expired(at)
        with self.transaction():
            cached = self.connection.execute(
                "SELECT * FROM releases WHERE request_key=?", (request_key,)
            ).fetchone()
            if cached is not None:
                if cached["org_id"] != org_id:
                    raise ServiceError("request_key 已被其他机构使用", 409)
                return self._release_from_row(cached)

            frozen = []
            seen = set()
            for item in items:
                record_id = item.get("record_id")
                language = item.get("language")
                item_channel = item.get("channel", channel)
                if language not in LANGUAGES:
                    raise ServiceError(f"不支持的语言: {language}", 400)
                key = (record_id, language, item_channel)
                if key in seen:
                    raise ServiceError(f"重复条目: {record_id}/{language}", 400)
                seen.add(key)

                record = self._record_row(record_id)
                if record["state"] != "approved":
                    raise ServiceError(
                        f"展项 {record_id} 尚未定稿（当前 {record['state']}）", 409)
                translation = self.connection.execute(
                    "SELECT * FROM translations WHERE record_id=? AND language=?",
                    (record_id, language),
                ).fetchone()
                if translation is None:
                    raise ServiceError(
                        f"展项 {record_id} 缺少 {language} 翻译稿", 409)
                if translation["state"] != "confirmed":
                    raise ServiceError(
                        f"展项 {record_id} 的 {language} 翻译稿"
                        f"尚未确认（{translation['state']}）", 409)
                grant = self._effective_grant(record_id, org_id, item_channel, at)
                if grant is None:
                    raise ServiceError(
                        f"展项 {record_id} 对机构 {org_id} 无有效授权", 403)

                content = json.loads(translation["content"])
                frozen.append({
                    "record_id": record_id,
                    "city": record["city"],
                    "language": language,
                    "channel": item_channel,
                    "record_version": record["version"],
                    "translation_version": translation["version"],
                    "grant_id": grant["grant_id"],
                    "title": record["title"],
                    "content": content,
                    "content_hash": content_hash({
                        "record_id": record_id,
                        "record_version": record["version"],
                        "language": language,
                        "translation_version": translation["version"],
                        "content": content,
                    }),
                })

            quota = self.connection.execute(
                "SELECT * FROM quotas WHERE org_id=?", (org_id,)
            ).fetchone()
            charge = len(frozen)
            if quota is None:
                raise ServiceError(f"机构 {org_id} 未配置发布配额", 402)
            if quota["used"] + charge > quota["limit"]:
                raise ServiceError(
                    f"发布配额不足：需要 {charge}，剩余 "
                    f"{quota['limit'] - quota['used']}", 402)

            package_id = uuid.uuid4().hex
            now = self.clock()
            self.connection.execute(
                "UPDATE quotas SET used=used+? WHERE org_id=?", (charge, org_id))
            self.connection.execute(
                "INSERT INTO releases VALUES(?,?,?,?,?,?)",
                (request_key, package_id, org_id, canonical_json(frozen), charge, now),
            )
            self._remember(request_key, {
                "package_id": package_id, "request_key": request_key,
                "org_id": org_id, "quota_charged": charge,
            })
            self._outbox(org_id, "release_created",
                         {"package_id": package_id, "quota_charged": charge,
                          "items": [{"record_id": f["record_id"],
                                     "language": f["language"]} for f in frozen]})
            return ReleasePackage(package_id, request_key, org_id, tuple(frozen),
                                  charge, now)

    def get_package(self, package_id):
        row = self.connection.execute(
            "SELECT * FROM releases WHERE package_id=?", (package_id,)
        ).fetchone()
        if row is None:
            raise ServiceError("发布包不存在", 404)
        return self._release_from_row(row)

    def list_packages(self, org_id):
        rows = self.connection.execute(
            "SELECT * FROM releases WHERE org_id=? ORDER BY created_at", (org_id,)
        ).fetchall()
        return [self._release_from_row(r) for r in rows]

    @staticmethod
    def _release_from_row(row):
        return ReleasePackage(
            row["package_id"], row["request_key"], row["org_id"],
            tuple(json.loads(row["items"])), row["quota_charged"], row["created_at"],
        )

    # -- 离线交接 -----------------------------------------------------------

    def open_handshake(self, org_id, actor=None):
        """打开（或复用）某机构的交接通道；状态落库，重启/离线后仍在。"""
        if actor is not None and org_id != actor and not self._is_admin(actor):
            raise ServiceError("无权为其他机构打开交接", 403)
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM handshakes WHERE org_id=? AND state='open'",
                (org_id,),
            ).fetchone()
            if row is not None:
                return self._handshake_dict(row)
            handshake_id = uuid.uuid4().hex
            now = self.clock()
            self.connection.execute(
                "INSERT INTO handshakes(handshake_id,org_id,state,updated_at) "
                "VALUES(?,?, 'open',?)",
                (handshake_id, org_id, now),
            )
            return {"handshake_id": handshake_id, "org_id": org_id,
                    "cursor_seq": 0, "state": "open"}

    def pull_handshake(self, handshake_id, actor, limit=100, at=None):
        """读取游标之后的交接事件；过期授权在拉取时一并结算。"""
        at = parse_time(at) if at else self._now()
        at = parse_time(at) if at else self._now()
        self._sweep_expired(at)
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM handshakes WHERE handshake_id=?", (handshake_id,)
            ).fetchone()
            if row is None:
                raise ServiceError("交接不存在", 404)
            if row["org_id"] != actor and not self._is_admin(actor):
                raise ServiceError("无权读取该交接", 403)
            ops = self.connection.execute(
                "SELECT seq,op,payload,created_at FROM outbox "
                "WHERE org_id=? AND seq>? ORDER BY seq LIMIT ?",
                (row["org_id"], row["cursor_seq"], limit),
            ).fetchall()
            return {
                "handshake_id": handshake_id,
                "org_id": row["org_id"],
                "cursor_seq": row["cursor_seq"],
                "next_cursor": ops[-1]["seq"] if ops else row["cursor_seq"],
                "has_more": len(ops) == limit,
                "ops": [{"seq": o["seq"], "op": o["op"],
                         "payload": json.loads(o["payload"]),
                         "created_at": o["created_at"]} for o in ops],
            }

    def ack_handshake(self, handshake_id, actor, cursor_seq):
        """确认已处理到某个游标；下次离线恢复从这里继续。"""
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM handshakes WHERE handshake_id=?", (handshake_id,)
            ).fetchone()
            if row is None:
                raise ServiceError("交接不存在", 404)
            if row["org_id"] != actor and not self._is_admin(actor):
                raise ServiceError("无权确认该交接", 403)
            if cursor_seq < row["cursor_seq"]:
                raise ServiceError("游标不能回退", 409)
            self.connection.execute(
                "UPDATE handshakes SET cursor_seq=?,updated_at=? WHERE handshake_id=?",
                (cursor_seq, self.clock(), handshake_id),
            )
            return {"handshake_id": handshake_id, "cursor_seq": cursor_seq}

    def close_handshake(self, handshake_id, actor):
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM handshakes WHERE handshake_id=?", (handshake_id,)
            ).fetchone()
            if row is None:
                raise ServiceError("交接不存在", 404)
            if row["org_id"] != actor and not self._is_admin(actor):
                raise ServiceError("无权关闭该交接", 403)
            self.connection.execute(
                "UPDATE handshakes SET state='closed',updated_at=? WHERE handshake_id=?",
                (self.clock(), handshake_id),
            )
            return {"handshake_id": handshake_id, "state": "closed"}

    @staticmethod
    def _handshake_dict(row):
        return {"handshake_id": row["handshake_id"], "org_id": row["org_id"],
                "cursor_seq": row["cursor_seq"], "state": row["state"]}

    # -- 最终清单 -----------------------------------------------------------

    def checklist(self, city=None, language=None, at=None):
        """按城市和语言汇总可核验的最终清单。

        每个条目给出展项版本、翻译稿版本/状态、当前有效授权到哪些机构，
        以及内容哈希；整份清单再给一个 manifest_hash，双方可离线复算比对。
        """
        if city is not None and city not in JIANGSU_CITIES:
            raise ServiceError(f"未知城市: {city}", 400)
        languages = (language,) if language else LANGUAGES
        if language and language not in LANGUAGES:
            raise ServiceError(f"不支持的语言: {language}", 400)
        at = parse_time(at) if at else self._now()
        self._sweep_expired(at)
        with self.transaction():
            sql = "SELECT * FROM records WHERE 1=1"
            params = []
            if city:
                sql += " AND city=?"; params.append(city)
            sql += " ORDER BY city,record_id"
            entries = []
            for record in self.connection.execute(sql, params).fetchall():
                for lang in languages:
                    t = self.connection.execute(
                        "SELECT * FROM translations WHERE record_id=? AND language=?",
                        (record["record_id"], lang),
                    ).fetchone()
                    full_grants = self.connection.execute(
                        "SELECT * FROM grants WHERE record_id=? ORDER BY grant_id",
                        (record["record_id"],),
                    ).fetchall()
                    authorized_orgs = sorted({
                        g["org_id"] for g in full_grants
                        if g["state"] == "active"
                        and parse_time(g["valid_from"]) <= at < parse_time(g["valid_until"])
                    })
                    content = json.loads(t["content"]) if t else None
                    translation_state = t["state"] if t else "missing"
                    t_version = t["version"] if t else 0
                    is_final = (
                        record["state"] == "approved"
                        and translation_state == "confirmed"
                        and bool(authorized_orgs)
                    )
                    material = {
                        "record_id": record["record_id"],
                        "record_version": record["version"],
                        "language": lang,
                        "translation_version": t_version,
                        "content": content,
                    }
                    entries.append({
                        "city": record["city"],
                        "record_id": record["record_id"],
                        "title": record["title"],
                        "record_state": record["state"],
                        "record_version": record["version"],
                        "language": lang,
                        "translation_state": translation_state,
                        "translation_version": t_version,
                        "authorized_orgs": authorized_orgs,
                        "final": is_final,
                        "content_hash": content_hash(material) if content else None,
                    })
            manifest = {
                "generated_at": at.isoformat(),
                "city": city or "all",
                "language": language or "all",
                "entries": entries,
            }
            manifest["manifest_hash"] = content_hash({
                k: v for k, v in manifest.items() if k != "manifest_hash"
            })
            return manifest

    def close(self):
        self.connection.close()
