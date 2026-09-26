import os
import tempfile
import unittest

from sydney_promotion.domain import (
    ConflictError, ForbiddenError, NotFoundError, StateError,
)
from sydney_promotion.service import DomainStore, PromotionService, ServiceError


class StoreTests(unittest.TestCase):
    """早期骨架的通用记录能力保持不变。"""

    def setUp(self):
        self.store = DomainStore()

    def tearDown(self):
        self.store.close()

    def test_idempotent_version(self):
        self.store.create("r1", "u1", {"topic": "悉尼文旅推介协作库"})
        a = self.store.transition("r1", "u1", "pending", "req-1", 1)
        b = self.store.transition("r1", "u1", "pending", "req-1", 1)
        self.assertEqual(a, b)
        with self.assertRaises(ServiceError):
            self.store.transition("r1", "u1", "approved", "req-2", 1)

    def test_permission_state(self):
        self.store.create("r2", "u1")
        with self.assertRaises(ServiceError):
            self.store.transition("r2", "u2", "pending", "req-3")
        with self.assertRaises(ServiceError):
            self.store.transition("r2", "u1", "closed", "req-4")


class FakeClock:
    """可推进的时间源,用于授权过期与发布窗口测试。"""

    def __init__(self, start="2026-09-10T09:00:00+00:00"):
        self.now = start

    def __call__(self):
        return self.now


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.service = PromotionService(clock=self.clock)
        self.service.register_org("center", "悉尼中国文化中心", "center")
        self.service.register_org("su", "苏州文旅", "partner")
        self.service.register_org("nj", "南京文旅", "partner")
        self.service.register_org("sydney", "悉尼合作机构", "partner")
        self.service.register_org("melb", "墨尔本合作机构", "partner")

    def tearDown(self):
        self.service.close()

    # ---- 常用准备 ----

    def approved_exhibit(self, owner="su", exhibit_id="ex1", city="苏州",
                         kind="heritage", title="苏绣", body="苏绣介绍"):
        self.service.create_exhibit(owner, exhibit_id, city, kind, title, body)
        self.service.change_exhibit_state(owner, exhibit_id, "submit")
        self.service.change_exhibit_state("center", exhibit_id, "approve")
        return exhibit_id

    def grant(self, org="sydney", exhibit_id="ex1",
              valid_from="2026-08-01", valid_until="2026-12-31"):
        grant_id = f"g-{exhibit_id}-{org}"
        self.service.issue_grant("center", grant_id, org, exhibit_id,
                                 valid_from, valid_until)
        return grant_id

    def window_quota(self, org="sydney", city="苏州", total=10,
                     opens="2026-09-01", closes="2026-10-01", window_id=None):
        window_id = window_id or f"w-{city}"
        self.service.open_window("center", window_id, city, opens, closes)
        self.service.set_quota("center", org, window_id, total)
        return window_id

    def confirmed_translation(self, exhibit_id="ex1", language="en",
                              text="Suzhou embroidery", owner="su"):
        self.service.put_translation(owner, exhibit_id, language, text)
        self.service.submit_translation(owner, exhibit_id, language)
        self.service.confirm_translation("center", exhibit_id, language)

    # ---- 机构隔离 ----

    def test_org_isolation(self):
        self.service.create_exhibit("su", "ex1", "苏州", "heritage", "苏绣", "介绍")
        with self.assertRaises(ForbiddenError):
            self.service.update_exhibit("nj", "ex1", 1, {"title": "篡改"})
        with self.assertRaises(ForbiddenError):
            self.service.put_translation("nj", "ex1", "en", "x")
        with self.assertRaises(ForbiddenError):
            self.service.change_exhibit_state("nj", "ex1", "submit")
        with self.assertRaises(ForbiddenError):
            self.service.get_exhibit("nj", "ex1")
        # 授权后对方可见
        self.service.change_exhibit_state("su", "ex1", "submit")
        self.service.change_exhibit_state("center", "ex1", "approve")
        self.grant(org="nj")
        self.assertEqual(self.service.get_exhibit("nj", "ex1").title, "苏绣")

    def test_center_only_operations(self):
        self.service.create_exhibit("su", "ex1", "苏州", "heritage", "苏绣", "介绍")
        with self.assertRaises(ForbiddenError):
            self.service.open_window("su", "w1", "苏州", "2026-09-01", "2026-10-01")
        with self.assertRaises(ForbiddenError):
            self.service.issue_grant("su", "g1", "sydney", "ex1",
                                     "2026-08-01", "2026-12-31")
        with self.assertRaises(ForbiddenError):
            self.service.change_exhibit_state("su", "ex1", "approve")
        self.service.change_exhibit_state("su", "ex1", "submit")
        self.service.change_exhibit_state("center", "ex1", "approve")
        self.service.put_translation("su", "ex1", "en", "embroidery")
        self.service.submit_translation("su", "ex1", "en")
        with self.assertRaises(ForbiddenError):
            self.service.confirm_translation("su", "ex1", "en")

    def test_unknown_actor_rejected(self):
        with self.assertRaises(NotFoundError):
            self.service.create_exhibit("ghost", "ex1", "苏州", "heritage", "t", "b")

    # ---- 版本校验:合并或拒绝 ----

    def test_version_merge_and_conflict(self):
        self.service.create_exhibit("su", "ex1", "苏州", "heritage", "苏绣", "介绍")
        updated = self.service.update_exhibit("su", "ex1", 1, {"title": "苏绣精品"})
        self.assertEqual(updated.version, 2)
        # 基于旧版本修改不相交字段,自动合并到最新版本
        merged = self.service.update_exhibit("su", "ex1", 1, {"body": "新介绍"})
        self.assertEqual(merged.version, 3)
        self.assertEqual(merged.title, "苏绣精品")
        self.assertEqual(merged.body, "新介绍")
        # 基于旧版本修改已被改过的字段,拒绝
        with self.assertRaises(ConflictError):
            self.service.update_exhibit("su", "ex1", 1, {"title": "再次改名"})
        with self.assertRaises(ConflictError):
            self.service.update_exhibit("su", "ex1", 2, {"body": "又改介绍"})
        # 版本号超前同样拒绝
        with self.assertRaises(ConflictError):
            self.service.update_exhibit("su", "ex1", 99, {"title": "x"})

    def test_translation_version_conflict(self):
        self.service.create_exhibit("su", "ex1", "苏州", "heritage", "苏绣", "介绍")
        self.service.put_translation("su", "ex1", "en", "first")
        self.service.put_translation("su", "ex1", "en", "second",
                                     expected_version=1)
        with self.assertRaises(ConflictError):
            self.service.put_translation("su", "ex1", "en", "third",
                                         expected_version=1)
        with self.assertRaises(ServiceError):
            self.service.put_translation("su", "ex1", "zh", "中文请改正文")

    def test_confirmed_translation_reset_on_edit(self):
        self.approved_exhibit()
        self.confirmed_translation()
        current = self.service.get_translation("su", "ex1", "en")
        edited = self.service.put_translation(
            "su", "ex1", "en", "revised", expected_version=current.version)
        self.assertEqual(edited.state, "draft")

    # ---- 翻译稿先后到达 ----

    def test_translation_arrives_late(self):
        self.approved_exhibit()
        self.grant()
        window_id = self.window_quota()
        # 英文译稿尚未到达,只能先出中文
        with self.assertRaises(StateError):
            self.service.issue_package("sydney", window_id, "b0",
                                       ["ex1"], ["zh", "en"])
        package = self.service.issue_package("sydney", window_id, "b1",
                                             ["ex1"], ["zh"])
        self.assertEqual(len(package["items"]), 1)
        # 译稿到达但未确认仍不可发布
        self.service.put_translation("su", "ex1", "en", "embroidery")
        self.service.submit_translation("su", "ex1", "en")
        with self.assertRaises(StateError):
            self.service.issue_package("sydney", window_id, "b2",
                                       ["ex1"], ["en"])
        self.service.confirm_translation("center", "ex1", "en")
        package = self.service.issue_package("sydney", window_id, "b2",
                                             ["ex1"], ["en"])
        self.assertEqual(package["items"][0]["language"], "en")

    # ---- 授权有效期与撤回 ----

    def test_expired_grant_blocks_package(self):
        self.approved_exhibit()
        self.grant(valid_until="2026-09-01")
        window_id = self.window_quota()
        with self.assertRaisesRegex(ForbiddenError, "已过期"):
            self.service.issue_package("sydney", window_id, "b1",
                                       ["ex1"], ["zh"])

    def test_revoked_grant_blocks_package(self):
        self.approved_exhibit()
        grant_id = self.grant()
        window_id = self.window_quota()
        self.service.revoke_grant("center", grant_id)
        with self.assertRaisesRegex(ForbiddenError, "已撤回"):
            self.service.issue_package("sydney", window_id, "b1",
                                       ["ex1"], ["zh"])
        # 重复撤回幂等
        self.assertEqual(self.service.revoke_grant("center", grant_id).state,
                         "revoked")

    def test_grant_requires_approved_exhibit(self):
        self.service.create_exhibit("su", "ex1", "苏州", "heritage", "苏绣", "介绍")
        with self.assertRaises(StateError):
            self.grant()

    # ---- 发布窗口 ----

    def test_window_enforcement(self):
        self.approved_exhibit()
        self.grant()
        # 窗口尚未开始
        early = self.window_quota(opens="2026-09-20", closes="2026-10-01",
                                  window_id="w-early")
        with self.assertRaises(StateError):
            self.service.issue_package("sydney", early, "b1", ["ex1"], ["zh"])
        # 窗口被提前关闭
        window_id = self.window_quota()
        self.service.close_window("center", window_id)
        with self.assertRaises(StateError):
            self.service.issue_package("sydney", window_id, "b2",
                                       ["ex1"], ["zh"])
        # 窗口已自然结束
        self.clock.now = "2026-10-02T00:00:00+00:00"
        late = self.window_quota(opens="2026-09-01", closes="2026-10-01",
                                 window_id="w-late")
        with self.assertRaises(StateError):
            self.service.issue_package("sydney", late, "b3", ["ex1"], ["zh"])

    def test_window_city_mismatch(self):
        self.approved_exhibit(city="苏州")
        self.grant()
        self.service.open_window("center", "w-南京", "南京",
                                 "2026-09-01", "2026-10-01")
        self.service.set_quota("center", "sydney", "w-南京", 5)
        with self.assertRaises(StateError):
            self.service.issue_package("sydney", "w-南京", "b1",
                                       ["ex1"], ["zh"])

    # ---- 幂等配额 ----

    def test_idempotent_replay_keeps_quota(self):
        self.approved_exhibit()
        self.grant()
        window_id = self.window_quota(total=3)
        self.confirmed_translation()
        first = self.service.issue_package("sydney", window_id, "b1",
                                           ["ex1"], ["zh", "en"])
        replay = self.service.issue_package("sydney", window_id, "b1",
                                            ["ex1"], ["zh", "en"])
        self.assertEqual(first["package_id"], replay["package_id"])
        quota = self.service.get_quota("center", "sydney", window_id)
        self.assertEqual(quota["used"], 2)
        # 同一批次键对应不同请求,拒绝
        with self.assertRaises(ConflictError):
            self.service.issue_package("sydney", window_id, "b1",
                                       ["ex1"], ["zh"])
        # 新批次键正常扣减,余量不足时拒绝
        self.service.issue_package("sydney", window_id, "b2", ["ex1"], ["zh"])
        with self.assertRaisesRegex(StateError, "配额不足"):
            self.service.issue_package("sydney", window_id, "b3",
                                       ["ex1"], ["zh"])

    def test_failed_batch_rolls_back_quota(self):
        self.approved_exhibit()
        self.grant()
        window_id = self.window_quota(total=1)
        self.confirmed_translation()
        with self.assertRaises(StateError):
            self.service.issue_package("sydney", window_id, "b1",
                                       ["ex1"], ["zh", "en"])
        quota = self.service.get_quota("center", "sydney", window_id)
        self.assertEqual(quota["used"], 0)
        # 失败的批次不占用批次键,修正后可重新提交
        package = self.service.issue_package("sydney", window_id, "b1",
                                             ["ex1"], ["zh"])
        self.assertEqual(len(package["items"]), 1)

    # ---- 修改待确认 ----

    def test_pending_change_blocks_package_until_confirmed(self):
        self.approved_exhibit()
        self.grant()
        window_id = self.window_quota()
        self.service.issue_package("sydney", window_id, "b1", ["ex1"], ["zh"])
        # 负责机构修改了已授权展项
        self.service.update_exhibit("su", "ex1", 3, {"body": "修改后的介绍"})
        changes = self.service.list_changes("sydney", state="pending")
        self.assertEqual(len(changes), 1)
        with self.assertRaisesRegex(StateError, "待确认"):
            self.service.issue_package("sydney", window_id, "b2",
                                       ["ex1"], ["zh"])
        # 其他机构不能代为确认
        with self.assertRaises(ForbiddenError):
            self.service.confirm_change("nj", changes[0].change_id)
        self.service.confirm_change("sydney", changes[0].change_id)
        package = self.service.issue_package("sydney", window_id, "b2",
                                             ["ex1"], ["zh"])
        self.assertEqual(package["items"][0]["exhibit_version"], 4)

    def test_rejected_change_also_blocks(self):
        self.approved_exhibit()
        self.grant()
        window_id = self.window_quota()
        self.service.update_exhibit("su", "ex1", 3, {"body": "修改后的介绍"})
        change = self.service.list_changes("sydney", state="pending")[0]
        self.service.reject_change("sydney", change.change_id)
        with self.assertRaises(StateError):
            self.service.issue_package("sydney", window_id, "b1",
                                       ["ex1"], ["zh"])

    def test_authorization_overview(self):
        self.approved_exhibit()
        self.grant()
        self.service.update_exhibit("su", "ex1", 3, {"body": "修改后的介绍"})
        overview = self.service.authorization_overview("center")
        self.assertEqual(len(overview["items"]), 1)
        item = overview["items"][0]
        self.assertEqual(item["status"], "active")
        self.assertEqual(len(item["pending_changes"]), 1)
        # 合作机构只能看自己的授权
        mine = self.service.authorization_overview("sydney")
        self.assertEqual(mine["items"][0]["org_id"], "sydney")
        with self.assertRaises(ForbiddenError):
            self.service.authorization_overview("su", org_id="sydney")
        # 授权过期后状态可见
        self.clock.now = "2027-01-01T00:00:00+00:00"
        expired = self.service.authorization_overview("center")
        self.assertEqual(expired["items"][0]["status"], "expired")

    # ---- 交接断线恢复 ----

    def test_handoff_resume_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "service.db")
            service = PromotionService(path, clock=self.clock)
            service.register_org("center", "悉尼中国文化中心", "center")
            service.register_org("sydney", "悉尼合作机构", "partner")
            service.create_exhibit("center", "ex1", "苏州", "image",
                                   "苏州影像", "影像说明")
            service.put_translation("center", "ex1", "en", "Suzhou images")
            service.create_handoff("center", "h1", "sydney", [
                {"exhibit_id": "ex1", "language": "zh"},
                {"exhibit_id": "ex1", "language": "en"},
            ])
            service.ack_handoff_item("sydney", "h1", "ex1", "zh")
            service.close()
            # 合作方离线一段时间后重新上线,服务进程也已重启
            resumed_service = PromotionService(path, clock=self.clock)
            try:
                state = resumed_service.resume_handoff("sydney", "h1")
                self.assertEqual(state["state"], "open")
                self.assertEqual(state["pending"],
                                 [{"exhibit_id": "ex1", "language": "en"}])
                self.assertEqual(state["received"],
                                 [{"exhibit_id": "ex1", "language": "zh"}])
                done = resumed_service.ack_handoff_item("sydney", "h1",
                                                        "ex1", "en")
                self.assertEqual(done["state"], "completed")
                # 重复签收幂等
                again = resumed_service.ack_handoff_item("sydney", "h1",
                                                         "ex1", "en")
                self.assertEqual(again["state"], "completed")
            finally:
                resumed_service.close()

    def test_handoff_permissions(self):
        self.approved_exhibit()
        self.service.create_handoff("center", "h1", "sydney", [
            {"exhibit_id": "ex1", "language": "zh"},
        ])
        with self.assertRaises(ForbiddenError):
            self.service.ack_handoff_item("nj", "h1", "ex1", "zh")
        with self.assertRaises(ForbiddenError):
            self.service.resume_handoff("nj", "h1")
        # 主办方可以查看任意交接进度
        self.assertEqual(
            self.service.resume_handoff("center", "h1")["state"], "open")
        handoffs = self.service.list_handoffs("sydney")
        self.assertEqual(handoffs[0]["pending_count"], 1)

    def test_handoff_requires_translation_present(self):
        self.approved_exhibit()
        with self.assertRaises(StateError):
            self.service.create_handoff("center", "h1", "sydney", [
                {"exhibit_id": "ex1", "language": "en"},
            ])

    # ---- 清单核验 ----

    def test_manifest_digest_and_filters(self):
        self.approved_exhibit(owner="su", exhibit_id="ex1", city="苏州")
        self.approved_exhibit(owner="nj", exhibit_id="ex2", city="南京",
                              title="云锦", body="云锦介绍")
        self.grant(exhibit_id="ex1")
        self.grant(exhibit_id="ex2")
        w1 = self.window_quota(city="苏州")
        w2 = self.window_quota(city="南京")
        self.confirmed_translation("ex1", "en", "Suzhou embroidery", "su")
        self.service.issue_package("sydney", w1, "b1", ["ex1"], ["zh", "en"])
        self.service.issue_package("sydney", w2, "b2", ["ex2"], ["zh"])

        suzhou = self.service.manifest("center", city="苏州")
        self.assertEqual(suzhou["count"], 2)
        self.assertTrue(all(e["city"] == "苏州" for e in suzhou["entries"]))
        english = self.service.manifest("center", language="en")
        self.assertEqual(english["count"], 1)
        self.assertEqual(english["entries"][0]["language"], "en")

        verified = self.service.verify_manifest("center", "苏州", None,
                                                suzhou["digest"])
        self.assertTrue(verified["match"])
        tampered = self.service.verify_manifest("center", "苏州", None, "0" * 64)
        self.assertFalse(tampered["match"])

    def test_manifest_scoped_to_partner(self):
        self.approved_exhibit()
        self.grant(org="sydney")
        self.grant(org="melb")
        window_id = self.window_quota(org="sydney")
        self.service.set_quota("center", "melb", window_id, 5)
        self.service.issue_package("sydney", window_id, "b1", ["ex1"], ["zh"])
        self.service.issue_package("melb", window_id, "b2", ["ex1"], ["zh"])
        center_view = self.service.manifest("center")
        self.assertEqual(center_view["count"], 2)
        sydney_view = self.service.manifest("sydney")
        self.assertEqual(sydney_view["count"], 1)
        self.assertEqual(sydney_view["entries"][0]["org_id"], "sydney")

    def test_manifest_keeps_latest_release(self):
        self.approved_exhibit()
        self.grant()
        window_id = self.window_quota()
        first = self.service.issue_package("sydney", window_id, "b1",
                                           ["ex1"], ["zh"])
        self.service.update_exhibit("su", "ex1", 3, {"body": "修订版介绍"})
        change = self.service.list_changes("sydney", state="pending")[0]
        self.service.confirm_change("sydney", change.change_id)
        second = self.service.issue_package("sydney", window_id, "b2",
                                            ["ex1"], ["zh"])
        manifest = self.service.manifest("center")
        self.assertEqual(manifest["count"], 1)
        entry = manifest["entries"][0]
        self.assertEqual(entry["package_id"], second["package_id"])
        self.assertNotEqual(entry["checksum"], first["items"][0]["checksum"])

    # ---- 事件流水 ----

    def test_event_trail(self):
        self.approved_exhibit()
        events = self.service.list_events("center", "ex1")
        kinds = [event["kind"] for event in events]
        self.assertIn("exhibit.created", kinds)
        self.assertIn("exhibit.state", kinds)
        with self.assertRaises(ForbiddenError):
            self.service.list_events("su", "ex1")


if __name__ == "__main__":
    unittest.main()
