"""悉尼文旅推介协作库的持久化边界、状态迁移、权限与幂等规则。

DomainStore 保留早期骨架的通用记录能力;PromotionService 在其上实现
展项版本、翻译稿、授权范围、发布窗口、配额、发布包与交接的业务规则。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager

from .domain import (
    BASE_LANGUAGE, CITIES, KINDS, LANGUAGES, ORG_ROLES,
    ChangeConfirmation, ConflictError, Exhibit, ForbiddenError, Grant,
    NotFoundError, Record, ServiceError, StateError, Translation, Window,
    content_checksum, parse_ts, utc_now,
)

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS records(
 record_id TEXT PRIMARY KEY,
 owner_id TEXT NOT NULL,
 state TEXT NOT NULL,
 version INTEGER NOT NULL,
 payload TEXT NOT NULL,
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
 event_id TEXT PRIMARY KEY,
 record_id TEXT NOT NULL,
 kind TEXT NOT NULL,
 body TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency(
 request_key TEXT PRIMARY KEY,
 result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS organizations(
 org_id TEXT PRIMARY KEY,
 name TEXT NOT NULL,
 role TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exhibits(
 exhibit_id TEXT PRIMARY KEY,
 city TEXT NOT NULL,
 kind TEXT NOT NULL,
 owner_org TEXT NOT NULL REFERENCES organizations(org_id),
 title TEXT NOT NULL,
 body TEXT NOT NULL,
 state TEXT NOT NULL,
 version INTEGER NOT NULL,
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS translations(
 exhibit_id TEXT NOT NULL REFERENCES exhibits(exhibit_id),
 language TEXT NOT NULL,
 text TEXT NOT NULL,
 state TEXT NOT NULL,
 version INTEGER NOT NULL,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(exhibit_id,language));
CREATE TABLE IF NOT EXISTS grants(
 grant_id TEXT PRIMARY KEY,
 org_id TEXT NOT NULL REFERENCES organizations(org_id),
 exhibit_id TEXT NOT NULL REFERENCES exhibits(exhibit_id),
 valid_from TEXT NOT NULL,
 valid_until TEXT NOT NULL,
 state TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS change_confirmations(
 change_id TEXT PRIMARY KEY,
 exhibit_id TEXT NOT NULL REFERENCES exhibits(exhibit_id),
 org_id TEXT NOT NULL,
 from_version INTEGER NOT NULL,
 to_version INTEGER NOT NULL,
 state TEXT NOT NULL,
 created_at TEXT NOT NULL,
 resolved_at TEXT);
CREATE TABLE IF NOT EXISTS windows(
 window_id TEXT PRIMARY KEY,
 city TEXT NOT NULL,
 opens_at TEXT NOT NULL,
 closes_at TEXT NOT NULL,
 state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quotas(
 org_id TEXT NOT NULL,
 window_id TEXT NOT NULL,
 total INTEGER NOT NULL,
 used INTEGER NOT NULL,
 PRIMARY KEY(org_id,window_id));
CREATE TABLE IF NOT EXISTS packages(
 package_id TEXT PRIMARY KEY,
 org_id TEXT NOT NULL,
 window_id TEXT NOT NULL,
 batch_key TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 state TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(org_id,batch_key));
CREATE TABLE IF NOT EXISTS package_items(
 package_id TEXT NOT NULL REFERENCES packages(package_id),
 exhibit_id TEXT NOT NULL,
 language TEXT NOT NULL,
 exhibit_version INTEGER NOT NULL,
 translation_version INTEGER NOT NULL,
 checksum TEXT NOT NULL,
 PRIMARY KEY(package_id,exhibit_id,language));
CREATE TABLE IF NOT EXISTS handoffs(
 handoff_id TEXT PRIMARY KEY,
 org_id TEXT NOT NULL,
 state TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handoff_items(
 handoff_id TEXT NOT NULL REFERENCES handoffs(handoff_id),
 exhibit_id TEXT NOT NULL,
 language TEXT NOT NULL,
 state TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(handoff_id,exhibit_id,language));
"""


class DomainStore:
    """SQLite 持久化边界:事务、事件流水与幂等键,保留通用记录能力。"""

    def __init__(self, database=":memory:", clock=utc_now):
        # HTTP 层按线程分发请求,连接允许多线程使用并以锁串行化
        self.connection = sqlite3.connect(database, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.clock = clock
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    @contextmanager
    def transaction(self):
        """整次写入要么全部提交,要么全部回滚。"""
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def emit(self, entity_id, kind, body):
        """在事务内追加一条可追溯事件。"""
        self.connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?)",
            (f"{entity_id}:{uuid.uuid4().hex[:12]}", entity_id, kind,
             json.dumps(body, ensure_ascii=False), self.clock()))

    def create(self, record_id, owner_id, payload=None):
        with self.transaction():
            self.connection.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?)",
                (record_id, owner_id, "draft", 1,
                 json.dumps(payload or {}, ensure_ascii=False), self.clock()))
            self.emit(record_id, "created", {})
        return self.get(record_id)

    def get(self, record_id):
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM records WHERE record_id=?",
                (record_id,)).fetchone()
        if row is None:
            raise NotFoundError("记录不存在")
        return Record(row["record_id"], row["owner_id"], row["state"],
                      row["version"], row["updated_at"])

    def transition(self, record_id, owner_id, target, request_key,
                   expected_version=None):
        with self.transaction():
            old = self.connection.execute(
                "SELECT * FROM records WHERE record_id=?",
                (record_id,)).fetchone()
            if old is None:
                raise NotFoundError("记录不存在")
            if old["owner_id"] != owner_id:
                raise ForbiddenError("无权操作")
            cached = self.connection.execute(
                "SELECT result FROM idempotency WHERE request_key=?",
                (request_key,)).fetchone()
            if cached:
                return json.loads(cached["result"])
            if expected_version is not None and old["version"] != expected_version:
                raise ConflictError("版本冲突")
            allowed = {"draft": {"pending"},
                       "pending": {"approved", "cancelled"},
                       "approved": {"closed"},
                       "cancelled": set(), "closed": set()}
            if target not in allowed.get(old["state"], set()):
                raise StateError("状态迁移不允许")
            version = old["version"] + 1
            now = self.clock()
            self.connection.execute(
                "UPDATE records SET state=?,version=?,updated_at=? WHERE record_id=?",
                (target, version, now, record_id))
            self.emit(record_id, "transition",
                      {"from": old["state"], "to": target, "version": version})
            result = {"record_id": record_id, "state": target, "version": version}
            self.connection.execute(
                "INSERT INTO idempotency VALUES(?,?)",
                (request_key, json.dumps(result, ensure_ascii=False)))
            return result

    def close(self):
        self.connection.close()


class PromotionService:
    """展项、翻译稿、授权、发布窗口、配额、发布包与交接的业务边界。"""

    def __init__(self, database=":memory:", clock=utc_now):
        self.store = DomainStore(database, clock)
        self.clock = clock

    def close(self):
        self.store.close()

    # ---- 内部工具 ----

    def _query(self, sql, args=()):
        with self.store.lock:
            return self.store.connection.execute(sql, args).fetchall()

    def _one(self, sql, args=()):
        with self.store.lock:
            return self.store.connection.execute(sql, args).fetchone()

    def _org(self, org_id):
        row = self._one("SELECT * FROM organizations WHERE org_id=?", (org_id,))
        if row is None:
            raise NotFoundError("机构未登记")
        return row

    def _require_center(self, actor):
        if self._org(actor)["role"] != "center":
            raise ForbiddenError("仅主办方可执行该操作")

    def _exhibit_row(self, exhibit_id):
        row = self._one("SELECT * FROM exhibits WHERE exhibit_id=?", (exhibit_id,))
        if row is None:
            raise NotFoundError("展项不存在")
        return row

    def _check_exhibit_visible(self, actor, row):
        """展项对主办方、负责机构和被授权机构可见。"""
        if actor == row["owner_org"]:
            return
        if self._org(actor)["role"] == "center":
            return
        granted = self._one(
            "SELECT 1 FROM grants WHERE org_id=? AND exhibit_id=?",
            (actor, row["exhibit_id"]))
        if granted:
            return
        raise ForbiddenError("无权查看该展项")

    @staticmethod
    def _to_exhibit(row):
        return Exhibit(row["exhibit_id"], row["city"], row["kind"],
                       row["owner_org"], row["title"], row["body"],
                       row["state"], row["version"], row["updated_at"])

    @staticmethod
    def _to_translation(row):
        return Translation(row["exhibit_id"], row["language"], row["text"],
                           row["state"], row["version"], row["updated_at"])

    @staticmethod
    def _to_grant(row):
        return Grant(row["grant_id"], row["org_id"], row["exhibit_id"],
                     row["valid_from"], row["valid_until"], row["state"],
                     row["created_at"])

    @staticmethod
    def _to_change(row):
        return ChangeConfirmation(row["change_id"], row["exhibit_id"],
                                  row["org_id"], row["from_version"],
                                  row["to_version"], row["state"],
                                  row["created_at"], row["resolved_at"])

    @staticmethod
    def _to_window(row):
        return Window(row["window_id"], row["city"], row["opens_at"],
                      row["closes_at"], row["state"])

    # ---- 机构 ----

    def register_org(self, org_id, name, role="partner"):
        """登记机构;role 为 center(主办方)或 partner(合作机构)。"""
        if role not in ORG_ROLES:
            raise ServiceError("机构类型无效")
        if not name:
            raise ServiceError("机构名称不能为空")
        with self.store.transaction():
            if self._one("SELECT 1 FROM organizations WHERE org_id=?", (org_id,)):
                raise ConflictError("机构编号已存在")
            self.store.connection.execute(
                "INSERT INTO organizations VALUES(?,?,?,?)",
                (org_id, name, role, self.clock()))
            self.store.emit(org_id, "org.registered", {"name": name, "role": role})
        return {"org_id": org_id, "name": name, "role": role}

    # ---- 展项 ----

    def create_exhibit(self, actor, exhibit_id, city, kind, title, body):
        """登记展项,负责机构即调用方;中文原稿随展项一并保存。"""
        self._org(actor)
        if city not in CITIES:
            raise ServiceError("城市不在江苏十三市范围内")
        if kind not in KINDS:
            raise ServiceError("展项类型无效")
        if not title or not body:
            raise ServiceError("展项标题与正文不能为空")
        with self.store.transaction():
            if self._one("SELECT 1 FROM exhibits WHERE exhibit_id=?", (exhibit_id,)):
                raise ConflictError("展项编号已存在")
            self.store.connection.execute(
                "INSERT INTO exhibits VALUES(?,?,?,?,?,?,?,?,?)",
                (exhibit_id, city, kind, actor, title, body, "draft", 1, self.clock()))
            self.store.emit(exhibit_id, "exhibit.created",
                            {"city": city, "kind": kind, "owner": actor})
        return self.get_exhibit(actor, exhibit_id)

    def get_exhibit(self, actor, exhibit_id):
        row = self._exhibit_row(exhibit_id)
        self._check_exhibit_visible(actor, row)
        return self._to_exhibit(row)

    def list_exhibits(self, actor, city=None):
        """主办方看全部;合作机构看自己负责的与被授权给自己的。"""
        me = self._org(actor)
        if city is not None and city not in CITIES:
            raise ServiceError("城市不在江苏十三市范围内")
        if me["role"] == "center":
            sql = "SELECT * FROM exhibits"
            args = []
            if city:
                sql += " WHERE city=?"
                args.append(city)
            rows = self._query(sql + " ORDER BY exhibit_id", args)
        else:
            sql = ("SELECT DISTINCT e.* FROM exhibits e"
                   " LEFT JOIN grants g ON g.exhibit_id=e.exhibit_id AND g.org_id=?"
                   " WHERE (e.owner_org=? OR g.org_id IS NOT NULL)")
            args = [actor, actor]
            if city:
                sql += " AND e.city=?"
                args.append(city)
            rows = self._query(sql + " ORDER BY e.exhibit_id", args)
        return [self._to_exhibit(r) for r in rows]

    def exhibit_detail(self, actor, exhibit_id):
        """展项详情:各语种译稿状态、授权与待确认修改一览。"""
        row = self._exhibit_row(exhibit_id)
        self._check_exhibit_visible(actor, row)
        now = self.clock()
        translations = [self._to_translation(r).__dict__ for r in self._query(
            "SELECT * FROM translations WHERE exhibit_id=? ORDER BY language",
            (exhibit_id,))]
        grants = []
        for g in self._query(
                "SELECT * FROM grants WHERE exhibit_id=? ORDER BY created_at",
                (exhibit_id,)):
            grants.append({**self._to_grant(g).__dict__,
                           "status": self._grant_status(g, now)})
        changes = [self._to_change(r).__dict__ for r in self._query(
            "SELECT * FROM change_confirmations WHERE exhibit_id=?"
            " ORDER BY created_at", (exhibit_id,))]
        return {"exhibit": self._to_exhibit(row).__dict__,
                "translations": translations, "grants": grants,
                "changes": changes}

    def _fields_changed_since(self, exhibit_id, version):
        """从事件流水收集 version 之后被修改过的字段,用于合并判定。"""
        fields = set()
        rows = self._query(
            "SELECT body FROM events WHERE record_id=? AND kind='exhibit.updated'",
            (exhibit_id,))
        for row in rows:
            body = json.loads(row["body"])
            if body.get("from", 0) >= version:
                fields.update(body.get("fields", []))
        return fields

    def update_exhibit(self, actor, exhibit_id, expected_version, changes):
        """修改展项内容:版本一致直接落库;落后时字段不相交则自动合并,
        字段相交则拒绝并提示当前版本。内容变化会为有效授权生成待确认。"""
        allowed = {"title", "body"}
        changes = {k: v for k, v in (changes or {}).items() if k in allowed}
        if not changes:
            raise ServiceError("没有可更新的字段")
        if expected_version is None:
            raise ServiceError("缺少版本号")
        with self.store.transaction():
            row = self._exhibit_row(exhibit_id)
            if row["owner_org"] != actor:
                raise ForbiddenError("只能修改本机构负责的展项")
            if row["state"] == "archived":
                raise StateError("展项已归档,不能再修改")
            current = row["version"]
            if expected_version > current:
                raise ConflictError(f"版本校验失败,当前版本 {current}")
            if expected_version < current:
                overlap = self._fields_changed_since(exhibit_id, expected_version) \
                    & set(changes)
                if overlap:
                    raise ConflictError(
                        f"版本冲突:字段 {sorted(overlap)} 已被他人修改,"
                        f"请基于版本 {current} 重新提交")
                # 字段不相交,自动合并到最新版本
            version = current + 1
            sets = ",".join(f"{k}=?" for k in changes)
            self.store.connection.execute(
                f"UPDATE exhibits SET {sets},version=?,updated_at=?"
                " WHERE exhibit_id=?",
                (*changes.values(), version, self.clock(), exhibit_id))
            self.store.emit(exhibit_id, "exhibit.updated",
                            {"fields": sorted(changes), "from": current,
                             "to": version, "actor": actor})
            self._refresh_change_confirmations(exhibit_id, actor, current, version)
        return self.get_exhibit(actor, exhibit_id)

    def _refresh_change_confirmations(self, exhibit_id, actor, from_version,
                                      to_version):
        """内容变化后,仍在有效期内的授权都需要对方重新确认。"""
        now = self.clock()
        grants = self._query(
            "SELECT * FROM grants WHERE exhibit_id=? AND state='active'"
            " AND org_id!=? AND valid_until>=?",
            (exhibit_id, actor, now))
        grantees = []
        for grant in grants:
            grantees.append(grant["org_id"])
            pending = self._one(
                "SELECT * FROM change_confirmations WHERE exhibit_id=?"
                " AND org_id=? AND state='pending'",
                (exhibit_id, grant["org_id"]))
            if pending:
                self.store.connection.execute(
                    "UPDATE change_confirmations SET to_version=? WHERE change_id=?",
                    (to_version, pending["change_id"]))
            else:
                self.store.connection.execute(
                    "INSERT INTO change_confirmations VALUES(?,?,?,?,?,?,?,?)",
                    (f"chg-{uuid.uuid4().hex[:12]}", exhibit_id, grant["org_id"],
                     from_version, to_version, "pending", now, None))
        if grantees:
            self.store.emit(exhibit_id, "exhibit.change_pending",
                            {"to_version": to_version, "grantees": grantees})

    # 展项状态操作:动作 -> (源状态, 目标状态, 允许的角色)
    EXHIBIT_ACTIONS = {
        "submit": ("draft", "submitted", "owner"),
        "approve": ("submitted", "approved", "center"),
        "reject": ("submitted", "draft", "center"),
        "archive": ("approved", "archived", "center"),
    }

    def change_exhibit_state(self, actor, exhibit_id, action,
                             expected_version=None):
        """提交/审定/退回/归档;提交由负责机构发起,审定与归档归主办方。"""
        if action not in self.EXHIBIT_ACTIONS:
            raise ServiceError("未知的状态操作")
        source, target, who = self.EXHIBIT_ACTIONS[action]
        with self.store.transaction():
            row = self._exhibit_row(exhibit_id)
            if who == "owner" and row["owner_org"] != actor:
                raise ForbiddenError("只能操作本机构负责的展项")
            if who == "center":
                self._require_center(actor)
            if row["state"] != source:
                raise StateError(f"当前状态 {row['state']} 不允许该操作")
            if expected_version is not None and row["version"] != expected_version:
                raise ConflictError("版本冲突")
            version = row["version"] + 1
            self.store.connection.execute(
                "UPDATE exhibits SET state=?,version=?,updated_at=?"
                " WHERE exhibit_id=?",
                (target, version, self.clock(), exhibit_id))
            self.store.emit(exhibit_id, "exhibit.state",
                            {"action": action, "from": source, "to": target,
                             "version": version, "actor": actor})
        return self.get_exhibit(actor, exhibit_id)

    # ---- 翻译稿 ----

    def _translation_row(self, exhibit_id, language):
        return self._one(
            "SELECT * FROM translations WHERE exhibit_id=? AND language=?",
            (exhibit_id, language))

    def put_translation(self, actor, exhibit_id, language, text,
                        expected_version=None):
        """上传或修改某语种译稿;已确认译稿被修改后回到草稿,需重新确认。"""
        if language not in LANGUAGES:
            raise ServiceError("语言代码无效")
        if language == BASE_LANGUAGE:
            raise ServiceError("中文原文请在展项正文维护")
        if not text:
            raise ServiceError("译文不能为空")
        with self.store.transaction():
            row = self._exhibit_row(exhibit_id)
            if row["owner_org"] != actor:
                raise ForbiddenError("只能维护本机构展项的翻译稿")
            if row["state"] == "archived":
                raise StateError("展项已归档,不能再修改")
            old = self._translation_row(exhibit_id, language)
            now = self.clock()
            if old is None:
                version = 1
                self.store.connection.execute(
                    "INSERT INTO translations VALUES(?,?,?,?,?,?)",
                    (exhibit_id, language, text, "draft", version, now))
            else:
                if expected_version is None:
                    raise ServiceError("缺少翻译稿版本号")
                if old["version"] != expected_version:
                    raise ConflictError(
                        f"翻译稿版本冲突,当前版本 {old['version']}")
                version = old["version"] + 1
                self.store.connection.execute(
                    "UPDATE translations SET text=?,state='draft',version=?,"
                    "updated_at=? WHERE exhibit_id=? AND language=?",
                    (text, version, now, exhibit_id, language))
            self.store.emit(exhibit_id, "translation.upserted",
                            {"language": language, "version": version,
                             "actor": actor})
        return self.get_translation(actor, exhibit_id, language)

    def get_translation(self, actor, exhibit_id, language):
        row = self._exhibit_row(exhibit_id)
        self._check_exhibit_visible(actor, row)
        translation = self._translation_row(exhibit_id, language)
        if translation is None:
            raise NotFoundError("该语种的翻译稿尚未到达")
        return self._to_translation(translation)

    def _transition_translation(self, actor, exhibit_id, language, source,
                                target, who):
        with self.store.transaction():
            row = self._exhibit_row(exhibit_id)
            if who == "owner" and row["owner_org"] != actor:
                raise ForbiddenError("只能操作本机构展项的翻译稿")
            if who == "center":
                self._require_center(actor)
            old = self._translation_row(exhibit_id, language)
            if old is None:
                raise NotFoundError("该语种的翻译稿尚未到达")
            if old["state"] != source:
                raise StateError(f"翻译稿当前状态 {old['state']} 不允许该操作")
            version = old["version"] + 1
            self.store.connection.execute(
                "UPDATE translations SET state=?,version=?,updated_at=?"
                " WHERE exhibit_id=? AND language=?",
                (target, version, self.clock(), exhibit_id, language))
            self.store.emit(exhibit_id, "translation.state",
                            {"language": language, "from": source, "to": target,
                             "version": version, "actor": actor})
        return self.get_translation(actor, exhibit_id, language)

    def submit_translation(self, actor, exhibit_id, language):
        """负责机构提交译稿,等待主办方确认。"""
        return self._transition_translation(
            actor, exhibit_id, language, "draft", "submitted", "owner")

    def confirm_translation(self, actor, exhibit_id, language):
        """主办方确认译稿,确认后才可进入发布包。"""
        return self._transition_translation(
            actor, exhibit_id, language, "submitted", "confirmed", "center")

    # ---- 授权 ----

    def _grant_row(self, grant_id):
        row = self._one("SELECT * FROM grants WHERE grant_id=?", (grant_id,))
        if row is None:
            raise NotFoundError("授权不存在")
        return row

    @staticmethod
    def _grant_status(grant, now):
        if grant["state"] == "revoked":
            return "revoked"
        if now < grant["valid_from"]:
            return "not_yet_valid"
        if now > grant["valid_until"]:
            return "expired"
        return "active"

    def issue_grant(self, actor, grant_id, org_id, exhibit_id,
                    valid_from, valid_until):
        """主办方把已审定展项在有效期内授权给合作机构。"""
        self._require_center(actor)
        grantee = self._org(org_id)
        if grantee["role"] != "partner":
            raise ServiceError("只能授权给合作机构")
        start, end = parse_ts(valid_from), parse_ts(valid_until)
        if start >= end:
            raise ServiceError("授权有效期无效")
        with self.store.transaction():
            row = self._exhibit_row(exhibit_id)
            if row["state"] != "approved":
                raise StateError("展项未审定,不能授权")
            if self._one("SELECT 1 FROM grants WHERE grant_id=?", (grant_id,)):
                raise ConflictError("授权编号已存在")
            self.store.connection.execute(
                "INSERT INTO grants VALUES(?,?,?,?,?,?,?)",
                (grant_id, org_id, exhibit_id, start, end, "active", self.clock()))
            self.store.emit(exhibit_id, "grant.issued",
                            {"grant_id": grant_id, "org_id": org_id,
                             "valid_from": start, "valid_until": end})
        return self.get_grant(actor, grant_id)

    def get_grant(self, actor, grant_id):
        row = self._grant_row(grant_id)
        me = self._org(actor)
        if me["role"] != "center" and row["org_id"] != actor:
            raise ForbiddenError("无权查看该授权")
        return self._to_grant(row)

    def revoke_grant(self, actor, grant_id):
        """撤回授权;撤回后不能再据此生成发布包。重复撤回幂等。"""
        self._require_center(actor)
        with self.store.transaction():
            row = self._grant_row(grant_id)
            if row["state"] != "revoked":
                self.store.connection.execute(
                    "UPDATE grants SET state='revoked' WHERE grant_id=?",
                    (grant_id,))
                self.store.emit(row["exhibit_id"], "grant.revoked",
                                {"grant_id": grant_id})
        return self.get_grant(actor, grant_id)

    def authorization_overview(self, actor, org_id=None):
        """授权总览:哪些展项已获授权、哪些修改正在等待对方确认。"""
        me = self._org(actor)
        if me["role"] == "center":
            target = org_id
        else:
            if org_id is not None and org_id != actor:
                raise ForbiddenError("只能查看本机构的授权")
            target = actor
        sql = ("SELECT g.*, e.title, e.city FROM grants g"
               " JOIN exhibits e ON e.exhibit_id=g.exhibit_id")
        args = []
        if target:
            sql += " WHERE g.org_id=?"
            args.append(target)
        now = self.clock()
        items = []
        for row in self._query(sql + " ORDER BY g.created_at", args):
            pending = [self._to_change(r).__dict__ for r in self._query(
                "SELECT * FROM change_confirmations WHERE exhibit_id=?"
                " AND org_id=? AND state='pending' ORDER BY created_at",
                (row["exhibit_id"], row["org_id"]))]
            items.append({
                **self._to_grant(row).__dict__,
                "title": row["title"], "city": row["city"],
                "status": self._grant_status(row, now),
                "pending_changes": pending,
            })
        return {"org_id": target, "generated_at": now, "items": items}

    # ---- 修改确认 ----

    def list_changes(self, actor, state=None):
        """待确认修改列表:主办方看全部,合作机构看发给自己的。"""
        me = self._org(actor)
        sql = "SELECT * FROM change_confirmations"
        conditions, args = [], []
        if me["role"] != "center":
            conditions.append("org_id=?")
            args.append(actor)
        if state is not None:
            conditions.append("state=?")
            args.append(state)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        return [self._to_change(r) for r in
                self._query(sql + " ORDER BY created_at", args)]

    def _resolve_change(self, actor, change_id, target):
        with self.store.transaction():
            row = self._one(
                "SELECT * FROM change_confirmations WHERE change_id=?",
                (change_id,))
            if row is None:
                raise NotFoundError("修改确认不存在")
            if row["org_id"] != actor:
                raise ForbiddenError("只能处理发给本机构的修改确认")
            if row["state"] != "pending":
                raise StateError("该修改已处理")
            self.store.connection.execute(
                "UPDATE change_confirmations SET state=?,resolved_at=?"
                " WHERE change_id=?",
                (target, self.clock(), change_id))
            self.store.emit(row["exhibit_id"], f"change.{target}",
                            {"change_id": change_id, "org_id": actor})
        return self._to_change(self._one(
            "SELECT * FROM change_confirmations WHERE change_id=?",
            (change_id,)))

    def confirm_change(self, actor, change_id):
        """被授权方确认展项修改,确认后可继续生成发布包。"""
        return self._resolve_change(actor, change_id, "confirmed")

    def reject_change(self, actor, change_id):
        """被授权方拒绝展项修改;拒绝同样阻止发布,需线下协商。"""
        return self._resolve_change(actor, change_id, "rejected")

    # ---- 发布窗口 ----

    def open_window(self, actor, window_id, city, opens_at, closes_at):
        """主办方为某城市打开发布窗口。"""
        self._require_center(actor)
        if city not in CITIES:
            raise ServiceError("城市不在江苏十三市范围内")
        start, end = parse_ts(opens_at), parse_ts(closes_at)
        if start >= end:
            raise ServiceError("发布窗口时间无效")
        with self.store.transaction():
            if self._one("SELECT 1 FROM windows WHERE window_id=?", (window_id,)):
                raise ConflictError("发布窗口编号已存在")
            self.store.connection.execute(
                "INSERT INTO windows VALUES(?,?,?,?,?)",
                (window_id, city, start, end, "open"))
            self.store.emit(window_id, "window.opened",
                            {"city": city, "opens_at": start, "closes_at": end})
        return self._to_window(self._one(
            "SELECT * FROM windows WHERE window_id=?", (window_id,)))

    def close_window(self, actor, window_id):
        """提前关闭发布窗口。重复关闭幂等。"""
        self._require_center(actor)
        with self.store.transaction():
            row = self._one("SELECT * FROM windows WHERE window_id=?",
                            (window_id,))
            if row is None:
                raise NotFoundError("发布窗口不存在")
            if row["state"] != "closed":
                self.store.connection.execute(
                    "UPDATE windows SET state='closed' WHERE window_id=?",
                    (window_id,))
                self.store.emit(window_id, "window.closed", {})
        return self._to_window(self._one(
            "SELECT * FROM windows WHERE window_id=?", (window_id,)))

    @staticmethod
    def _window_usable(window, now):
        return (window["state"] == "open"
                and window["opens_at"] <= now <= window["closes_at"])

    # ---- 配额 ----

    def set_quota(self, actor, org_id, window_id, total):
        """主办方为机构在指定窗口设置发布配额(按发布条目计)。"""
        self._require_center(actor)
        self._org(org_id)
        if self._one("SELECT 1 FROM windows WHERE window_id=?", (window_id,)) is None:
            raise NotFoundError("发布窗口不存在")
        if not isinstance(total, int) or total < 0:
            raise ServiceError("配额必须是非负整数")
        with self.store.transaction():
            row = self._one(
                "SELECT * FROM quotas WHERE org_id=? AND window_id=?",
                (org_id, window_id))
            if row is None:
                self.store.connection.execute(
                    "INSERT INTO quotas VALUES(?,?,?,0)",
                    (org_id, window_id, total))
            else:
                if row["used"] > total:
                    raise StateError("新配额低于已用量")
                self.store.connection.execute(
                    "UPDATE quotas SET total=? WHERE org_id=? AND window_id=?",
                    (total, org_id, window_id))
            self.store.emit(window_id, "quota.set",
                            {"org_id": org_id, "total": total})
        return self.get_quota(actor, org_id, window_id)

    def get_quota(self, actor, org_id, window_id):
        me = self._org(actor)
        if me["role"] != "center" and actor != org_id:
            raise ForbiddenError("只能查看本机构的配额")
        row = self._one(
            "SELECT * FROM quotas WHERE org_id=? AND window_id=?",
            (org_id, window_id))
        if row is None:
            raise NotFoundError("配额未设置")
        return {"org_id": row["org_id"], "window_id": row["window_id"],
                "total": row["total"], "used": row["used"]}

    # ---- 发布包 ----

    def issue_package(self, actor, window_id, batch_key, exhibit_ids, languages):
        """生成发布包:校验窗口、授权、待确认修改与译稿状态后,在同一事务里
        扣减配额并落库。相同批次键重放直接返回原结果,不重复扣减配额。"""
        self._org(actor)
        if not batch_key:
            raise ServiceError("缺少批次键")
        if not exhibit_ids or not languages:
            raise ServiceError("发布内容不能为空")
        for language in languages:
            if language not in LANGUAGES:
                raise ServiceError(f"语言代码无效: {language}")
        exhibit_ids = list(dict.fromkeys(exhibit_ids))
        languages = list(dict.fromkeys(languages))
        request_hash = content_checksum({
            "window_id": window_id,
            "exhibit_ids": sorted(exhibit_ids),
            "languages": sorted(languages)})
        with self.store.transaction():
            duplicate = self._one(
                "SELECT * FROM packages WHERE org_id=? AND batch_key=?",
                (actor, batch_key))
            if duplicate:
                if duplicate["request_hash"] != request_hash:
                    raise ConflictError("相同批次键对应不同的发布请求")
                return self._package_payload(duplicate)
            window = self._one("SELECT * FROM windows WHERE window_id=?",
                               (window_id,))
            if window is None:
                raise NotFoundError("发布窗口不存在")
            now = self.clock()
            if not self._window_usable(window, now):
                raise StateError("发布窗口未开放或已关闭")
            items = []
            for exhibit_id in exhibit_ids:
                items.extend(self._build_items(
                    actor, window, exhibit_id, languages, now))
            needed = len(items)
            quota = self._one(
                "SELECT * FROM quotas WHERE org_id=? AND window_id=?",
                (actor, window_id))
            total = quota["total"] if quota else 0
            used = quota["used"] if quota else 0
            if used + needed > total:
                raise StateError(f"配额不足:需要 {needed},剩余 {total - used}")
            self.store.connection.execute(
                "UPDATE quotas SET used=? WHERE org_id=? AND window_id=?",
                (used + needed, actor, window_id))
            package_id = "pkg-" + content_checksum(
                {"org_id": actor, "batch_key": batch_key})[:16]
            self.store.connection.execute(
                "INSERT INTO packages VALUES(?,?,?,?,?,?,?)",
                (package_id, actor, window_id, batch_key, request_hash,
                 "issued", now))
            for item in items:
                self.store.connection.execute(
                    "INSERT INTO package_items VALUES(?,?,?,?,?,?)",
                    (package_id, item["exhibit_id"], item["language"],
                     item["exhibit_version"], item["translation_version"],
                     item["checksum"]))
            self.store.emit(package_id, "package.issued",
                            {"org_id": actor, "window_id": window_id,
                             "items": needed})
            return self._package_payload(self._one(
                "SELECT * FROM packages WHERE package_id=?", (package_id,)))

    def _build_items(self, actor, window, exhibit_id, languages, now):
        """校验单个展项的可发布性并生成带校验和的发布条目。"""
        row = self._exhibit_row(exhibit_id)
        if row["city"] != window["city"]:
            raise StateError(f"展项 {exhibit_id} 不属于窗口城市 {window['city']}")
        if row["state"] != "approved":
            raise StateError(f"展项 {exhibit_id} 未审定")
        grants = self._query(
            "SELECT * FROM grants WHERE exhibit_id=? AND org_id=?",
            (exhibit_id, actor))
        if not grants:
            raise ForbiddenError(f"展项 {exhibit_id} 未授权给本机构")
        if not any(self._grant_status(g, now) == "active" for g in grants):
            if any(g["state"] == "revoked" for g in grants):
                raise ForbiddenError(f"展项 {exhibit_id} 的授权已撤回")
            if any(self._grant_status(g, now) == "expired" for g in grants):
                raise ForbiddenError(f"展项 {exhibit_id} 的授权已过期")
            raise ForbiddenError(f"展项 {exhibit_id} 的授权尚未生效")
        blocking = self._one(
            "SELECT 1 FROM change_confirmations WHERE exhibit_id=?"
            " AND org_id=? AND state IN ('pending','rejected')",
            (exhibit_id, actor))
        if blocking:
            raise StateError(f"展项 {exhibit_id} 存在待确认的修改")
        items = []
        for language in languages:
            if language == BASE_LANGUAGE:
                text, translation_version = row["body"], 0
            else:
                translation = self._translation_row(exhibit_id, language)
                if translation is None:
                    raise StateError(f"展项 {exhibit_id} 缺少 {language} 翻译稿")
                if translation["state"] != "confirmed":
                    raise StateError(
                        f"展项 {exhibit_id} 的 {language} 翻译稿未确认")
                text = translation["text"]
                translation_version = translation["version"]
            items.append({
                "exhibit_id": exhibit_id,
                "language": language,
                "exhibit_version": row["version"],
                "translation_version": translation_version,
                "checksum": content_checksum({
                    "exhibit_id": exhibit_id,
                    "language": language,
                    "title": row["title"],
                    "text": text,
                    "exhibit_version": row["version"],
                    "translation_version": translation_version}),
            })
        return items

    def _package_payload(self, package):
        items = [dict(r) for r in self._query(
            "SELECT exhibit_id,language,exhibit_version,translation_version,"
            "checksum FROM package_items WHERE package_id=?"
            " ORDER BY exhibit_id,language", (package["package_id"],))]
        return {"package_id": package["package_id"], "org_id": package["org_id"],
                "window_id": package["window_id"], "batch_key": package["batch_key"],
                "state": package["state"], "created_at": package["created_at"],
                "items": items}

    def get_package(self, actor, package_id):
        package = self._one("SELECT * FROM packages WHERE package_id=?",
                            (package_id,))
        if package is None:
            raise NotFoundError("发布包不存在")
        me = self._org(actor)
        if me["role"] != "center" and package["org_id"] != actor:
            raise ForbiddenError("无权查看该发布包")
        return self._package_payload(package)

    # ---- 交接 ----

    def create_handoff(self, actor, handoff_id, org_id, items):
        """主办方为合作机构建立交接单,逐条签收,支持断线后续传。"""
        self._require_center(actor)
        self._org(org_id)
        if not items:
            raise ServiceError("交接内容不能为空")
        with self.store.transaction():
            if self._one("SELECT 1 FROM handoffs WHERE handoff_id=?",
                         (handoff_id,)):
                raise ConflictError("交接编号已存在")
            now = self.clock()
            self.store.connection.execute(
                "INSERT INTO handoffs VALUES(?,?,?,?,?)",
                (handoff_id, org_id, "open", now, now))
            seen = set()
            for item in items:
                exhibit_id = item.get("exhibit_id")
                language = item.get("language")
                if language not in LANGUAGES:
                    raise ServiceError(f"语言代码无效: {language}")
                self._exhibit_row(exhibit_id)
                if language != BASE_LANGUAGE and \
                        self._translation_row(exhibit_id, language) is None:
                    raise StateError(f"展项 {exhibit_id} 缺少 {language} 翻译稿")
                key = (exhibit_id, language)
                if key in seen:
                    raise ServiceError("交接条目重复")
                seen.add(key)
                self.store.connection.execute(
                    "INSERT INTO handoff_items VALUES(?,?,?,?,?)",
                    (handoff_id, exhibit_id, language, "pending", now))
            self.store.emit(handoff_id, "handoff.created",
                            {"org_id": org_id, "items": len(seen)})
        return self.resume_handoff(actor, handoff_id)

    def _handoff_row(self, handoff_id):
        row = self._one("SELECT * FROM handoffs WHERE handoff_id=?",
                        (handoff_id,))
        if row is None:
            raise NotFoundError("交接单不存在")
        return row

    def ack_handoff_item(self, actor, handoff_id, exhibit_id, language):
        """合作方签收一个交接条目;重复签收幂等,全部签收后交接完成。"""
        with self.store.transaction():
            handoff = self._handoff_row(handoff_id)
            if handoff["org_id"] != actor:
                raise ForbiddenError("只能签收本机构的交接")
            item = self._one(
                "SELECT * FROM handoff_items WHERE handoff_id=?"
                " AND exhibit_id=? AND language=?",
                (handoff_id, exhibit_id, language))
            if item is None:
                raise NotFoundError("交接条目不存在")
            now = self.clock()
            if item["state"] != "received":
                self.store.connection.execute(
                    "UPDATE handoff_items SET state='received',updated_at=?"
                    " WHERE handoff_id=? AND exhibit_id=? AND language=?",
                    (now, handoff_id, exhibit_id, language))
                remaining = self._one(
                    "SELECT COUNT(*) AS n FROM handoff_items WHERE handoff_id=?"
                    " AND state='pending'", (handoff_id,))["n"]
                state = "completed" if remaining == 0 else handoff["state"]
                self.store.connection.execute(
                    "UPDATE handoffs SET state=?,updated_at=? WHERE handoff_id=?",
                    (state, now, handoff_id))
                self.store.emit(handoff_id, "handoff.acked",
                                {"exhibit_id": exhibit_id, "language": language})
        return self.resume_handoff(actor, handoff_id)

    def resume_handoff(self, actor, handoff_id):
        """恢复交接进度:合作方离线后重新上线时,从这里拿到未完成条目。"""
        handoff = self._handoff_row(handoff_id)
        me = self._org(actor)
        if me["role"] != "center" and handoff["org_id"] != actor:
            raise ForbiddenError("无权查看该交接")
        pending, received = [], []
        for row in self._query(
                "SELECT * FROM handoff_items WHERE handoff_id=?"
                " ORDER BY exhibit_id,language", (handoff_id,)):
            entry = {"exhibit_id": row["exhibit_id"],
                     "language": row["language"]}
            (received if row["state"] == "received" else pending).append(entry)
        return {"handoff_id": handoff["handoff_id"], "org_id": handoff["org_id"],
                "state": handoff["state"], "updated_at": handoff["updated_at"],
                "pending": pending, "received": received}

    def list_handoffs(self, actor):
        """交接单一览:主办方看全部,合作机构看自己的。"""
        me = self._org(actor)
        if me["role"] == "center":
            rows = self._query("SELECT * FROM handoffs ORDER BY created_at")
        else:
            rows = self._query(
                "SELECT * FROM handoffs WHERE org_id=? ORDER BY created_at",
                (actor,))
        result = []
        for row in rows:
            pending = self._one(
                "SELECT COUNT(*) AS n FROM handoff_items WHERE handoff_id=?"
                " AND state='pending'", (row["handoff_id"],))["n"]
            total = self._one(
                "SELECT COUNT(*) AS n FROM handoff_items WHERE handoff_id=?",
                (row["handoff_id"],))["n"]
            result.append({"handoff_id": row["handoff_id"],
                           "org_id": row["org_id"], "state": row["state"],
                           "updated_at": row["updated_at"],
                           "pending_count": pending, "total_count": total})
        return result

    # ---- 清单与事件 ----

    def manifest(self, actor, city=None, language=None):
        """按城市和语言给出最终发布清单,附整体摘要供对方核验。"""
        me = self._org(actor)
        if city is not None and city not in CITIES:
            raise ServiceError("城市不在江苏十三市范围内")
        if language is not None and language not in LANGUAGES:
            raise ServiceError("语言代码无效")
        sql = ("SELECT pi.exhibit_id,pi.language,pi.exhibit_version,"
               " pi.translation_version,pi.checksum,p.package_id,p.org_id,"
               " p.created_at,e.city,e.title FROM package_items pi"
               " JOIN packages p ON p.package_id=pi.package_id"
               " JOIN exhibits e ON e.exhibit_id=pi.exhibit_id"
               " WHERE p.state='issued'")
        args = []
        if me["role"] != "center":
            sql += " AND p.org_id=?"
            args.append(actor)
        if city is not None:
            sql += " AND e.city=?"
            args.append(city)
        if language is not None:
            sql += " AND pi.language=?"
            args.append(language)
        # 同一机构同一展项同一语种可能多次发布,清单只保留最新一次
        latest = {}
        for row in self._query(sql + " ORDER BY p.created_at,p.rowid", args):
            key = (row["org_id"], row["exhibit_id"], row["language"])
            latest[key] = row
        entries = [{
            "city": row["city"], "exhibit_id": row["exhibit_id"],
            "title": row["title"], "language": row["language"],
            "org_id": row["org_id"], "package_id": row["package_id"],
            "exhibit_version": row["exhibit_version"],
            "translation_version": row["translation_version"],
            "checksum": row["checksum"],
        } for row in sorted(latest.values(),
                            key=lambda r: (r["city"], r["exhibit_id"],
                                           r["language"], r["org_id"]))]
        lines = sorted(f"{e['org_id']}|{e['exhibit_id']}|{e['language']}|{e['checksum']}"
                       for e in entries)
        digest = content_checksum({"lines": lines})
        return {"city": city, "language": language, "count": len(entries),
                "digest": digest, "entries": entries}

    def verify_manifest(self, actor, city, language, digest):
        """重新计算清单摘要并与对方持有的摘要比对。"""
        current = self.manifest(actor, city, language)
        return {"match": current["digest"] == digest,
                "digest": current["digest"], "count": current["count"]}

    def list_events(self, actor, entity_id):
        """某实体的事件流水,供主办方核对状态变化过程。"""
        self._require_center(actor)
        return [{"kind": row["kind"], "body": json.loads(row["body"]),
                 "created_at": row["created_at"]}
                for row in self._query(
                    "SELECT * FROM events WHERE record_id=?"
                    " ORDER BY created_at,event_id", (entity_id,))]
