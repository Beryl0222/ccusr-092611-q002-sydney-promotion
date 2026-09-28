"""展项协作、授权窗口、版本合并、幂等配额、离线恢复与最终清单测试。"""
import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import quote

from sydney_promotion import api as api_mod
from sydney_promotion.domain import ServiceError
from sydney_promotion.service import CENTER_ID, DomainStore, canonical_json, content_hash

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start=T0):
        self.t = start

    def __call__(self):
        return self.t.isoformat()

    def advance(self, **delta):
        self.t += timedelta(**delta)
        return self.t.isoformat()


def make_ready(store, rid="r1", org="org-a", city="南京", quota=10):
    """造一条可发布的展项：已批准、英文翻译已确认、授权在窗口内、有配额。"""
    store.create(rid, org, {"desc": "初稿"}, city=city, kind="video", title="云锦")
    store.transition(rid, org, "pending", f"{rid}:submit")
    store.transition(rid, CENTER_ID, "approved", f"{rid}:approve")
    store.submit_translation(rid, "en", org, {"body": "Cloud Brocade"})
    store.review_translation(rid, "en", CENTER_ID, True)
    store.issue_grant(
        f"g-{rid}", rid, org, ["web"],
        T0.isoformat(), (T0 + timedelta(days=30)).isoformat())
    store.set_quota(org, quota)


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.store = DomainStore(clock=Clock())

    def tearDown(self):
        self.store.close()

    def test_org_touching_others_record_rejected(self):
        self.store.create("r1", "org-a", city="苏州", title="苏绣")
        with self.assertRaises(ServiceError) as cm:
            self.store.edit_record("r1", "org-b", {"desc": "x"}, 1)
        self.assertEqual(cm.exception.code, 403)
        with self.assertRaises(ServiceError):
            self.store.submit_translation("r1", "en", "org-b", {"body": "x"})
        with self.assertRaises(ServiceError):
            self.store.transition("r1", "org-b", "pending", "k1")

    def test_only_center_issues_grants_and_reviews(self):
        self.store.create("r1", "org-a", city="南京")
        with self.assertRaises(ServiceError):
            self.store.issue_grant("g1", "r1", "org-a", ["web"],
                                   T0.isoformat(),
                                   (T0 + timedelta(days=1)).isoformat(),
                                   issuer_id="org-a")
        with self.assertRaises(ServiceError):
            self.store.set_quota("org-a", 5, issuer_id="org-a")

    def test_org_cannot_open_handshake_for_other_org(self):
        with self.assertRaises(ServiceError):
            self.store.open_handshake("org-a", actor="org-b")


class VersionMergeTests(unittest.TestCase):
    def setUp(self):
        self.store = DomainStore(clock=Clock())
        self.store.create("r1", "org-a", {"a": 1, "b": 1}, city="扬州")

    def tearDown(self):
        self.store.close()

    def test_stale_version_without_rebase_rejected(self):
        self.store.edit_record("r1", "org-a", {"a": 2}, 1, "k1")
        with self.assertRaises(ServiceError) as cm:
            self.store.edit_record("r1", "org-a", {"a": 3}, 1, "k2")
        self.assertEqual(cm.exception.code, 409)

    def test_disjoint_field_edits_auto_merge(self):
        # 双方都从 v1 出发，改不同字段：第二笔自动三方合并成 v3
        first = self.store.edit_record("r1", "org-a", {"a": 2}, 1, "k1")
        self.assertEqual(first["version"], 2)
        second = self.store.edit_record("r1", "org-a", {"b": 9}, 1, "k2")
        self.assertEqual(second["version"], 3)
        self.assertTrue(second["merged"])
        self.assertEqual(second["payload"], {"a": 2, "b": 9})

    def test_same_field_different_value_conflicts(self):
        self.store.edit_record("r1", "org-a", {"a": 2}, 1, "k1")
        with self.assertRaises(ServiceError):
            self.store.edit_record("r1", "org-a", {"a": 3}, 1, "k2")

    def test_same_value_change_is_not_conflict(self):
        self.store.edit_record("r1", "org-a", {"a": 2}, 1, "k1")
        self.store.edit_record("r1", "org-a", {"a": 2}, 1, "k2")
        rec = self.store.get("r1")
        self.assertEqual(rec.payload, {"a": 2, "b": 1})

    def test_idempotent_edit_replay_returns_same_result(self):
        a = self.store.edit_record("r1", "org-a", {"a": 7}, 1, "dup")
        b = self.store.edit_record("r1", "org-a", {"a": 7}, 1, "dup")
        self.assertEqual(a, b)
        self.assertEqual(self.store.get("r1").version, 2)

    def test_edit_after_approval_reopens_pending(self):
        self.store.transition("r1", "org-a", "pending", "t1")
        self.store.transition("r1", CENTER_ID, "approved", "t2")
        out = self.store.edit_record("r1", "org-a", {"a": 5}, 3, "t3")
        self.assertEqual(out["state"], "pending")


class TranslationFlowTests(unittest.TestCase):
    def setUp(self):
        self.store = DomainStore(clock=Clock())
        self.store.create("r1", "org-a", city="南京")

    def tearDown(self):
        self.store.close()

    def test_review_and_resubmit_cycle(self):
        self.store.submit_translation("r1", "fr", "org-a", {"body": "v1"})
        rej = self.store.review_translation("r1", "fr", CENTER_ID, False,
                                            note="术语待改")
        self.assertEqual(rej["state"], "changes_requested")
        with self.assertRaises(ServiceError):
            self.store.submit_translation("r1", "fr", "org-a", {"body": "v2"},
                                          expected_version=1)
        ok = self.store.submit_translation("r1", "fr", "org-a", {"body": "v2"},
                                           expected_version=2)
        self.assertEqual(ok["version"], 3)
        appr = self.store.review_translation("r1", "fr", CENTER_ID, True)
        self.assertEqual(appr["state"], "confirmed")


class GrantWindowTests(unittest.TestCase):
    def setUp(self):
        self.store = DomainStore(clock=Clock())
        make_ready(self.store)

    def tearDown(self):
        self.store.close()

    def _items(self):
        return [{"record_id": "r1", "language": "en", "channel": "web"}]

    def test_released_within_window(self):
        pkg = self.store.create_release("rel-1", "org-a", self._items())
        self.assertEqual(pkg.quota_charged, 1)

    def test_expired_grant_blocks_release(self):
        future = (T0 + timedelta(days=31)).isoformat()
        with self.assertRaises(ServiceError) as cm:
            self.store.create_release("rel-x", "org-a", self._items(), at=future)
        self.assertEqual(cm.exception.code, 403)
        grant = self.store.get_grant("g-r1")
        self.assertEqual(grant.state, "expired")

    def test_before_valid_from_blocks_release(self):
        with self.assertRaises(ServiceError):
            self.store.create_release(
                "rel-y", "org-a", self._items(),
                at=(T0 - timedelta(minutes=1)).isoformat())

    def test_revoked_grant_blocks_release(self):
        self.store.revoke_grant("g-r1")
        with self.assertRaises(ServiceError):
            self.store.create_release("rel-z", "org-a", self._items())

    def test_scope_mismatch_blocks_release(self):
        with self.assertRaises(ServiceError) as cm:
            self.store.create_release(
                "rel-p", "org-a",
                [{"record_id": "r1", "language": "en", "channel": "print"}])
        self.assertEqual(cm.exception.code, 403)

    def test_unconfirmed_translation_blocks_release(self):
        self.store.submit_translation("r1", "en", "org-a",
                                      {"body": "changed"}, expected_version=2)
        with self.assertRaises(ServiceError):
            self.store.create_release("rel-q", "org-a", self._items())

    def test_org_without_grant_cannot_release(self):
        make_ready(self.store, rid="r2", org="org-b", city="苏州")
        with self.assertRaises(ServiceError):
            self.store.create_release(
                "rel-o", "org-b",
                [{"record_id": "r1", "language": "en", "channel": "web"}])


class QuotaIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.store = DomainStore(clock=Clock())
        make_ready(self.store, quota=2)

    def tearDown(self):
        self.store.close()

    def test_retry_same_batch_charges_once(self):
        items = [{"record_id": "r1", "language": "en", "channel": "web"}]
        first = self.store.create_release("batch-1", "org-a", items)
        second = self.store.create_release("batch-1", "org-a", items)
        third = self.store.create_release("batch-1", "org-a", items)
        self.assertEqual(first.package_id, second.package_id)
        self.assertEqual(second.package_id, third.package_id)
        self.assertEqual(third.quota_charged, 1)
        packages = self.store.list_packages("org-a")
        self.assertEqual(len(packages), 1)

    def test_quota_enforced_when_exhausted(self):
        items = [{"record_id": "r1", "language": "en", "channel": "web"}]
        self.store.create_release("a", "org-a", items)
        self.store.create_release("b", "org-a", items)
        with self.assertRaises(ServiceError) as cm:
            self.store.create_release("c", "org-a", items)
        self.assertEqual(cm.exception.code, 402)
        # 失败的整批不能留下半包或扣掉的配额
        self.assertEqual(len(self.store.list_packages("org-a")), 2)

    def test_request_key_cannot_be_shared_across_orgs(self):
        make_ready(self.store, rid="r2", org="org-b", city="苏州")
        items = [{"record_id": "r1", "language": "en", "channel": "web"}]
        self.store.create_release("shared", "org-a", items)
        with self.assertRaises(ServiceError):
            self.store.create_release(
                "shared", "org-b",
                [{"record_id": "r2", "language": "en", "channel": "web"}])


class HandshakeRecoveryTests(unittest.TestCase):
    def test_resume_after_offline_period_and_process_restart(self):
        path = tempfile.mktemp(suffix=".db")
        store = DomainStore(database=path, clock=Clock())
        make_ready(store)
        hs = store.open_handshake("org-a", actor="org-a")

        batch = self.store_pull(store, hs["handshake_id"], "org-a")
        self.assertTrue(batch["ops"])  # 建展项等事件已可拉取
        # 合作方处理了前 3 条后离线
        first3 = batch["ops"][:3]
        store.ack_handshake(hs["handshake_id"], "org-a", first3[-1]["seq"])

        # 离线期间：主办方撤回授权、又产生新事件
        store.revoke_grant("g-r1")
        store.close()

        # 服务/合作方重启后从未确认游标继续，撤回通知不丢
        store2 = DomainStore(database=path, clock=Clock())
        again = self.store_pull(store2, hs["handshake_id"], "org-a")
        ops = [o["op"] for o in again["ops"]]
        self.assertIn("grant_revoked", ops)
        seqs = [o["seq"] for o in again["ops"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertGreater(seqs[0], first3[-1]["seq"])
        last = again["next_cursor"]
        store2.ack_handshake(hs["handshake_id"], "org-a", last)

        # 再拉一次没有新内容，游标稳定（恢复完成）
        empty = self.store_pull(store2, hs["handshake_id"], "org-a")
        self.assertEqual(empty["ops"], [])
        self.assertEqual(empty["cursor_seq"], last)
        store2.close()

    @staticmethod
    def store_pull(store, hid, actor):
        return store.pull_handshake(hid, actor, limit=1000)

    def test_center_may_pull_any_org_handshake(self):
        store = DomainStore(clock=Clock())
        make_ready(store)
        hs = store.open_handshake("org-a", actor=CENTER_ID)
        pulled = store.pull_handshake(hs["handshake_id"], CENTER_ID)
        self.assertTrue(pulled["ops"])
        store.close()


class ChecklistTests(unittest.TestCase):
    def setUp(self):
        self.store = DomainStore(clock=Clock())
        make_ready(self.store, rid="r1", org="org-a", city="南京")
        # 徐州展项：有展项和翻译，但授权已过期 —— 不能算 final
        make_ready(self.store, rid="r2", org="org-b", city="徐州")
        # 徐州展项授权已失效 —— 不能算 final
        self.store.connection.execute(
            "UPDATE grants SET state='expired' WHERE grant_id='g-r2'")

    def tearDown(self):
        self.store.close()

    def test_filter_by_city_and_language_and_final_flag(self):
        manifest = self.store.checklist(city="南京", language="en")
        self.assertEqual(manifest["city"], "南京")
        (entry,) = manifest["entries"]
        self.assertTrue(entry["final"])
        self.assertIn("org-a", entry["authorized_orgs"])
        self.assertEqual(len(entry["content_hash"]), 64)

        xz = self.store.checklist(city="徐州", language="en")
        self.assertFalse(xz["entries"][0]["final"])
        self.assertEqual(xz["entries"][0]["authorized_orgs"], [])

    def test_manifest_hash_is_reproducible_and_tamper_evident(self):
        a = self.store.checklist()
        b = self.store.checklist()
        self.assertEqual(a["manifest_hash"], b["manifest_hash"])
        self.store.submit_translation("r1", "en", "org-a",
                                      {"body": "Cloud Brocade v2"},
                                      expected_version=2)
        c = self.store.checklist()
        self.assertNotEqual(a["manifest_hash"], c["manifest_hash"])

    def test_content_hash_matches_canonical_material(self):
        m = self.store.checklist(city="南京", language="en")
        rec = self.store.get("r1")
        tr = self.store.get_translation("r1", "en")
        material = {
            "record_id": "r1", "record_version": rec.version,
            "language": "en", "translation_version": tr.version,
            "content": {"body": "Cloud Brocade"},
        }
        self.assertEqual(m["entries"][0]["content_hash"],
                         content_hash(material))
        # 外部可用同样的规范序列化独立复算
        import hashlib
        rebuilt = hashlib.sha256(
            canonical_json(material).encode("utf-8")).hexdigest()
        self.assertEqual(rebuilt, m["entries"][0]["content_hash"])


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        api_mod.Handler.store = DomainStore(clock=Clock())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), api_mod.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _call(self, method, path, body=None, actor=CENTER_ID):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Org-Id", actor)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_flow_over_http(self):
        code, _ = self._call("POST", "/records", {
            "record_id": "h1", "owner_id": "org-a", "city": "泰州",
            "kind": "intangible_heritage", "title": "溱潼会船",
            "payload": {"desc": "x"}})
        self.assertEqual(code, 201)

        # 别的机构不能碰
        code, err = self._call("POST", "/records/h1/edits",
                               {"changes": {"desc": "y"}, "expected_version": 1},
                               actor="org-b")
        self.assertEqual(code, 403)

        code, _ = self._call("POST", "/records/h1/edits",
                             {"changes": {"desc": "y"}, "expected_version": 1},
                             actor="org-a")
        self.assertEqual(code, 200)
        code, _ = self._call("POST", "/records/h1/transition",
                             {"target": "pending", "request_key": "h1s1"},
                             actor="org-a")
        self.assertEqual(code, 200)
        code, _ = self._call("POST", "/records/h1/transition",
                             {"target": "approved", "request_key": "h1s2"})
        self.assertEqual(code, 200)
        code, _ = self._call("PUT", "/records/h1/translations/en",
                             {"content": {"body": "Boat Festival"}}, actor="org-a")
        self.assertEqual(code, 200)
        code, _ = self._call("POST", "/records/h1/translations/en/review",
                             {"approve": True})
        self.assertEqual(code, 200)
        code, _ = self._call("POST", "/grants", {
            "grant_id": "hg1", "record_id": "h1", "org_id": "org-a",
            "scope": ["web", "screen"],
            "valid_from": T0.isoformat(),
            "valid_until": (T0 + timedelta(days=10)).isoformat()})
        self.assertEqual(code, 201)
        code, _ = self._call("POST", "/quotas",
                             {"org_id": "org-a", "limit": 5})
        self.assertEqual(code, 200)

        body = {"request_key": "http-rel",
                "items": [{"record_id": "h1", "language": "en",
                           "channel": "screen"}]}
        code, first = self._call("POST", "/releases", body, actor="org-a")
        self.assertEqual(code, 201)
        code, second = self._call("POST", "/releases", body, actor="org-a")
        self.assertEqual(code, 201)
        self.assertEqual(first["package_id"], second["package_id"])

        code, manifest = self._call(
            "GET", f"/checklist?city={quote('泰州')}&language=en")
        self.assertEqual(code, 200)
        self.assertTrue(manifest["entries"][0]["final"])
        self.assertEqual(len(manifest["manifest_hash"]), 64)


if __name__ == "__main__":
    unittest.main()
