import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from urllib.parse import quote

from sydney_promotion.api import build_handler
from sydney_promotion.service import PromotionService


class ApiTests(unittest.TestCase):
    """HTTP 边界冒烟测试:路由、身份头与错误码映射。"""

    def setUp(self):
        self.service = PromotionService()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          build_handler(self.service))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.service.close()

    def call(self, method, path, body=None, actor=None):
        connection = HTTPConnection("127.0.0.1", self.port)
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Org-Id"] = actor
        connection.request(method, path,
                           json.dumps(body) if body is not None else None,
                           headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_full_flow(self):
        self.assertEqual(self.call("POST", "/orgs", {
            "org_id": "center", "name": "悉尼中国文化中心", "role": "center",
        })[0], 201)
        self.assertEqual(self.call("POST", "/orgs", {
            "org_id": "su", "name": "苏州文旅", "role": "partner",
        })[0], 201)

        # 缺少身份头
        status, _ = self.call("GET", "/exhibits")
        self.assertEqual(status, 401)

        status, exhibit = self.call("POST", "/exhibits", {
            "exhibit_id": "ex1", "city": "苏州", "kind": "heritage",
            "title": "苏绣", "body": "苏绣介绍",
        }, actor="su")
        self.assertEqual(status, 201)
        self.assertEqual(exhibit["version"], 1)

        # 其他机构修改被拦截
        self.call("POST", "/orgs", {"org_id": "nj", "name": "南京文旅"})
        status, error = self.call("POST", "/exhibits/ex1/update", {
            "expected_version": 1, "changes": {"title": "篡改"},
        }, actor="nj")
        self.assertEqual(status, 403)
        self.assertIn("error", error)

        # 版本冲突返回 409
        self.call("POST", "/exhibits/ex1/update", {
            "expected_version": 1, "changes": {"title": "苏绣精品"},
        }, actor="su")
        status, _ = self.call("POST", "/exhibits/ex1/update", {
            "expected_version": 1, "changes": {"title": "再次改名"},
        }, actor="su")
        self.assertEqual(status, 409)

        # 审定、授权、窗口、配额、译稿
        self.call("POST", "/exhibits/ex1/state", {"action": "submit"}, actor="su")
        self.call("POST", "/exhibits/ex1/state", {"action": "approve"},
                  actor="center")
        self.assertEqual(self.call("POST", "/grants", {
            "grant_id": "g1", "org_id": "su", "exhibit_id": "ex1",
            "valid_from": "2026-08-01", "valid_until": "2026-12-31",
        }, actor="center")[0], 201)
        self.call("POST", "/windows", {
            "window_id": "w1", "city": "苏州",
            "opens_at": "2026-09-01", "closes_at": "2026-10-01",
        }, actor="center")
        self.call("POST", "/quotas", {
            "org_id": "su", "window_id": "w1", "total": 4,
        }, actor="center")
        self.call("PUT", "/exhibits/ex1/translations/en",
                  {"text": "Suzhou embroidery"}, actor="su")
        self.call("POST", "/exhibits/ex1/translations/en/submit", actor="su")
        self.call("POST", "/exhibits/ex1/translations/en/confirm", actor="center")

        # 发布包幂等:同一批次键重放不重复扣配额
        body = {"window_id": "w1", "batch_key": "b1",
                "exhibit_ids": ["ex1"], "languages": ["zh", "en"]}
        status, first = self.call("POST", "/packages", body, actor="su")
        self.assertEqual(status, 201)
        status, replay = self.call("POST", "/packages", body, actor="su")
        self.assertEqual(first["package_id"], replay["package_id"])
        _, quota = self.call("GET", "/quotas?org_id=su&window_id=w1",
                             actor="center")
        self.assertEqual(quota["used"], 2)

        # 交接与断点恢复
        self.call("POST", "/handoffs", {
            "handoff_id": "h1", "org_id": "su",
            "items": [{"exhibit_id": "ex1", "language": "zh"}],
        }, actor="center")
        _, handoff = self.call("GET", "/handoffs/h1", actor="su")
        self.assertEqual(len(handoff["pending"]), 1)
        _, done = self.call("POST", "/handoffs/h1/ack", {
            "exhibit_id": "ex1", "language": "zh",
        }, actor="su")
        self.assertEqual(done["state"], "completed")

        # 清单可核验
        _, manifest = self.call("GET", "/manifest?city=" + quote("苏州"),
                                actor="center")
        self.assertEqual(manifest["count"], 2)
        _, check = self.call(
            "GET", "/manifest/verify?city=" + quote("苏州")
            + f"&digest={manifest['digest']}",
            actor="center")
        self.assertTrue(check["match"])

    def test_unknown_route(self):
        status, _ = self.call("GET", "/nope", actor="center")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
