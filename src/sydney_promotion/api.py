"""悉尼文旅推介协作库的轻量 HTTP 边界。

调用方身份取自 X-Org-Id 请求头;业务错误按 ServiceError.status 映射。
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .service import DomainStore, PromotionService, ServiceError


def _jsonable(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def build_handler(service):
    """把业务服务暴露为本地 JSON 接口。"""
    routes = []

    def route(method, pattern, public=False):
        def decorator(handler):
            routes.append((method, pattern.strip("/").split("/"),
                           public, handler))
            return handler
        return decorator

    @route("POST", "/orgs", public=True)
    def _(actor, params, body):
        return 201, service.register_org(
            body.get("org_id"), body.get("name"), body.get("role", "partner"))

    @route("POST", "/exhibits")
    def _(actor, params, body):
        return 201, service.create_exhibit(
            actor, body.get("exhibit_id"), body.get("city"), body.get("kind"),
            body.get("title"), body.get("body"))

    @route("GET", "/exhibits")
    def _(actor, params, body):
        return 200, service.list_exhibits(actor, params.get("city"))

    @route("GET", "/exhibits/{exhibit_id}")
    def _(actor, params, body):
        return 200, service.exhibit_detail(actor, params["exhibit_id"])

    @route("POST", "/exhibits/{exhibit_id}/update")
    def _(actor, params, body):
        return 200, service.update_exhibit(
            actor, params["exhibit_id"], body.get("expected_version"),
            body.get("changes"))

    @route("POST", "/exhibits/{exhibit_id}/state")
    def _(actor, params, body):
        return 200, service.change_exhibit_state(
            actor, params["exhibit_id"], body.get("action"),
            body.get("expected_version"))

    @route("PUT", "/exhibits/{exhibit_id}/translations/{language}")
    def _(actor, params, body):
        return 200, service.put_translation(
            actor, params["exhibit_id"], params["language"], body.get("text"),
            body.get("expected_version"))

    @route("GET", "/exhibits/{exhibit_id}/translations/{language}")
    def _(actor, params, body):
        return 200, service.get_translation(
            actor, params["exhibit_id"], params["language"])

    @route("POST", "/exhibits/{exhibit_id}/translations/{language}/submit")
    def _(actor, params, body):
        return 200, service.submit_translation(
            actor, params["exhibit_id"], params["language"])

    @route("POST", "/exhibits/{exhibit_id}/translations/{language}/confirm")
    def _(actor, params, body):
        return 200, service.confirm_translation(
            actor, params["exhibit_id"], params["language"])

    @route("POST", "/grants")
    def _(actor, params, body):
        return 201, service.issue_grant(
            actor, body.get("grant_id"), body.get("org_id"),
            body.get("exhibit_id"), body.get("valid_from"),
            body.get("valid_until"))

    @route("GET", "/grants/{grant_id}")
    def _(actor, params, body):
        return 200, service.get_grant(actor, params["grant_id"])

    @route("POST", "/grants/{grant_id}/revoke")
    def _(actor, params, body):
        return 200, service.revoke_grant(actor, params["grant_id"])

    @route("GET", "/authorizations")
    def _(actor, params, body):
        return 200, service.authorization_overview(actor, params.get("org_id"))

    @route("GET", "/changes")
    def _(actor, params, body):
        return 200, service.list_changes(actor, params.get("state"))

    @route("POST", "/changes/{change_id}/confirm")
    def _(actor, params, body):
        return 200, service.confirm_change(actor, params["change_id"])

    @route("POST", "/changes/{change_id}/reject")
    def _(actor, params, body):
        return 200, service.reject_change(actor, params["change_id"])

    @route("POST", "/windows")
    def _(actor, params, body):
        return 201, service.open_window(
            actor, body.get("window_id"), body.get("city"),
            body.get("opens_at"), body.get("closes_at"))

    @route("POST", "/windows/{window_id}/close")
    def _(actor, params, body):
        return 200, service.close_window(actor, params["window_id"])

    @route("POST", "/quotas")
    def _(actor, params, body):
        return 200, service.set_quota(
            actor, body.get("org_id"), body.get("window_id"), body.get("total"))

    @route("GET", "/quotas")
    def _(actor, params, body):
        return 200, service.get_quota(
            actor, params.get("org_id"), params.get("window_id"))

    @route("POST", "/packages")
    def _(actor, params, body):
        return 201, service.issue_package(
            actor, body.get("window_id"), body.get("batch_key"),
            body.get("exhibit_ids"), body.get("languages"))

    @route("GET", "/packages/{package_id}")
    def _(actor, params, body):
        return 200, service.get_package(actor, params["package_id"])

    @route("POST", "/handoffs")
    def _(actor, params, body):
        return 201, service.create_handoff(
            actor, body.get("handoff_id"), body.get("org_id"),
            body.get("items"))

    @route("GET", "/handoffs")
    def _(actor, params, body):
        return 200, service.list_handoffs(actor)

    @route("GET", "/handoffs/{handoff_id}")
    def _(actor, params, body):
        return 200, service.resume_handoff(actor, params["handoff_id"])

    @route("POST", "/handoffs/{handoff_id}/ack")
    def _(actor, params, body):
        return 200, service.ack_handoff_item(
            actor, params["handoff_id"], body.get("exhibit_id"),
            body.get("language"))

    @route("GET", "/manifest")
    def _(actor, params, body):
        return 200, service.manifest(
            actor, params.get("city"), params.get("language"))

    @route("GET", "/manifest/verify")
    def _(actor, params, body):
        return 200, service.verify_manifest(
            actor, params.get("city"), params.get("language"),
            params.get("digest"))

    @route("GET", "/events/{entity_id}")
    def _(actor, params, body):
        return 200, service.list_events(actor, params["entity_id"])

    @route("GET", "/records/{record_id}")
    def _(actor, params, body):
        return 200, service.store.get(params["record_id"])

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, payload):
            data = json.dumps(_jsonable(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            segments = [s for s in parsed.path.strip("/").split("/") if s]
            query = {key: values[0]
                     for key, values in parse_qs(parsed.query).items()}
            body = {}
            if method in ("POST", "PUT"):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
            actor = self.headers.get("X-Org-Id", "")
            for route_method, pattern, public, handler in routes:
                if route_method != method or len(pattern) != len(segments):
                    continue
                params = dict(query)
                matched = True
                for want, got in zip(pattern, segments):
                    if want.startswith("{") and want.endswith("}"):
                        params[want[1:-1]] = got
                    elif want != got:
                        matched = False
                        break
                if not matched:
                    continue
                if not public and not actor:
                    return self._reply(401, {"error": "缺少 X-Org-Id 请求头"})
                try:
                    code, payload = handler(actor, params, body)
                    return self._reply(code, payload)
                except ServiceError as exc:
                    return self._reply(exc.status, {"error": str(exc)})
                except (KeyError, ValueError, TypeError, AttributeError) as exc:
                    return self._reply(400, {"error": f"请求参数无效: {exc}"})
            return self._reply(404, {"error": "接口不存在"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def log_message(self, *_):
            return

    return Handler


def serve(host="127.0.0.1", port=8080, database="sydney_promotion.db"):
    """以文件库启动服务,重启后记录与交接进度不丢失。"""
    service = PromotionService(database)
    ThreadingHTTPServer((host, port), build_handler(service)).serve_forever()
