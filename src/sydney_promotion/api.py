"""悉尼文旅推介协作库的轻量 HTTP 边界。

调用方通过 ``X-Org-Id`` 头表明机构身份（默认为主办方 center）；服务端按
身份做权限隔离。写请求支持携带 ``request_key`` 做幂等重放。

路由概览：
  POST   /records                                 建展项
  GET    /records/{id}                            查展项
  POST   /records/{id}/transition                 状态迁移
  POST   /records/{id}/edits                      带版本校验的并行编辑
  PUT    /records/{id}/translations/{lang}        提交/重交翻译稿
  POST   /records/{id}/translations/{lang}/review 主办方确认/退回
  POST   /grants                                  签发授权
  POST   /grants/{id}/revoke                      撤回授权
  GET    /grants?record_id=&org_id=               查授权
  POST   /quotas                                  设置配额
  POST   /releases                                生成发布包（幂等扣配额）
  GET    /packages[/{id}]                         查发布包
  POST   /handshakes                              打开交接
  GET    /handshakes/{id}                         拉取交接事件
  POST   /handshakes/{id}/ack                     确认游标
  POST   /handshakes/{id}/close                   关闭交接
  GET    /checklist?city=&language=&at=           最终可核验清单
"""
import json
import os
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .domain import ServiceError
from .service import CENTER_ID, DomainStore


def _dc(value):
    return asdict(value) if hasattr(value, "__dataclass_fields__") else value


class Handler(BaseHTTPRequestHandler):
    store = DomainStore()

    # -- HTTP 基础 ---------------------------------------------------------

    def _reply(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            raise ServiceError("请求体不是合法 JSON", 400)
        if not isinstance(data, dict):
            raise ServiceError("请求体必须是 JSON 对象", 400)
        return data

    def _actor(self, body):
        return self.headers.get("X-Org-Id") or body.pop("_actor", None) or CENTER_ID

    def _query(self):
        return {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}

    def log_message(self, *_):
        return

    # -- 路由 --------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path.strip("/").split("/")
        try:
            if len(path) == 2 and path[0] == "records":
                self._reply(200, _dc(self.store.get(path[1])))
            elif path[:1] == ["grants"]:
                q = self._query()
                grants = self.store.list_grants(q.get("record_id"), q.get("org_id"))
                self._reply(200, [_dc(g) for g in grants])
            elif path[:1] == ["packages"]:
                actor = self._actor({})
                if len(path) == 2:
                    pkg = self.store.get_package(path[1])
                    if pkg.org_id != actor and actor != CENTER_ID:
                        raise ServiceError("无权查看该发布包", 403)
                    self._reply(200, pkg.to_dict())
                else:
                    self._reply(200, [p.to_dict() for p in self.store.list_packages(actor)])
            elif len(path) == 2 and path[0] == "handshakes":
                q = self._query()
                self._reply(200, self.store.pull_handshake(
                    path[1], self._actor({}),
                    limit=int(q.get("limit", 100)), at=q.get("at")))
            elif path[:1] == ["checklist"]:
                q = self._query()
                self._reply(200, self.store.checklist(
                    q.get("city"), q.get("language"), q.get("at")))
            else:
                self._reply(404, {"error": "未知路径"})
        except ServiceError as exc:
            self._reply(exc.code, {"error": str(exc)})

    def do_POST(self):
        self._dispatch_write(put=False)

    def do_PUT(self):
        self._dispatch_write(put=True)

    def _dispatch_write(self, put):
        path = urlparse(self.path).path.strip("/").split("/")
        try:
            body = self._body()
            actor = self._actor(body)

            if path == ["records"]:
                rec = self.store.create(
                    body["record_id"], body.get("owner_id", actor),
                    payload=body.get("payload"), city=body.get("city"),
                    kind=body.get("kind", "exhibit"), title=body.get("title", ""))
                self._reply(201, _dc(rec))

            elif len(path) == 3 and path[0] == "records" and path[2] == "transition":
                self._reply(200, self.store.transition(
                    path[1], actor, body["target"], body["request_key"],
                    body.get("expected_version")))

            elif len(path) == 3 and path[0] == "records" and path[2] == "edits":
                self._reply(200, self.store.edit_record(
                    path[1], actor, body["changes"], body["expected_version"],
                    body.get("request_key")))

            elif (len(path) == 4 and path[0] == "records"
                  and path[2] == "translations" and put):
                self._reply(200, self.store.submit_translation(
                    path[1], path[3], actor, body["content"],
                    body.get("expected_version"), body.get("request_key")))

            elif (len(path) == 5 and path[0] == "records"
                  and path[2] == "translations" and path[4] == "review"):
                self._reply(200, self.store.review_translation(
                    path[1], path[3], actor, bool(body["approve"]),
                    body.get("note"), body.get("request_key")))

            elif path == ["grants"]:
                self._reply(201, self.store.issue_grant(
                    body["grant_id"], body["record_id"], body["org_id"],
                    body["scope"], body["valid_from"], body["valid_until"],
                    issuer_id=actor, request_key=body.get("request_key")))

            elif len(path) == 3 and path[0] == "grants" and path[2] == "revoke":
                self._reply(200, self.store.revoke_grant(
                    path[1], actor, body.get("request_key")))

            elif path == ["quotas"]:
                self.store.set_quota(body["org_id"], int(body["limit"]), actor)
                self._reply(200, {"org_id": body["org_id"], "limit": body["limit"]})

            elif path == ["releases"]:
                pkg = self.store.create_release(
                    body["request_key"], actor, body["items"],
                    at=body.get("at"), channel=body.get("channel", "web"))
                self._reply(201, pkg.to_dict())

            elif path == ["handshakes"]:
                self._reply(201, self.store.open_handshake(body["org_id"], actor))

            elif (len(path) == 3 and path[0] == "handshakes"
                  and path[2] == "ack"):
                self._reply(200, self.store.ack_handshake(
                    path[1], actor, int(body["cursor_seq"])))

            elif (len(path) == 3 and path[0] == "handshakes"
                  and path[2] == "close"):
                self._reply(200, self.store.close_handshake(path[1], actor))

            else:
                self._reply(404, {"error": "未知路径"})
        except ServiceError as exc:
            self._reply(exc.code, {"error": str(exc)})
        except KeyError as exc:
            self._reply(400, {"error": f"缺少字段: {exc.args[0]}"})


def serve(host="127.0.0.1", port=8080, database=None):
    database = database or os.environ.get("SYDNEY_DB", ":memory:")
    Handler.store = DomainStore(database)
    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    serve()
