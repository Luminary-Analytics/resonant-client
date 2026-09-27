"""Small authenticated HTTP adapter for the separate governance store.

All current membership, authorization, revision, replay and audit decisions
belong to one store operation. This adapter never constructs database grants.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .identity import AuthenticationFailed, Identity, _unique_object
from .admin_http import routes as administration_routes
from .models import AccessDenied, CommandEnvelope, Conflict, InvalidRequest, Query, UnsupportedVersion

_log = logging.getLogger(__name__)
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def create_app(identity: Identity, store, *, hosts=None, content=None, monitoring=None, collaboration=None, sharing_retention=None) -> Starlette:
    """Construct the API with explicitly configured identity and transactional store.

    Production requires HTTPS as observed on the authenticated transport, not
    an untrusted forwarded header. Test profile is restricted to loopback peers.
    Server access logs must remain disabled because URLs can contain secrets.
    """

    async def principal(request: Request):
        if identity.config.profile == "test":
            try:
                local = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
            except ValueError:
                local = False
            if not local:
                raise AuthenticationFailed()
        elif request.url.scheme != "https":
            raise AuthenticationFailed()
        authorization = request.headers.getlist("authorization")
        if len(authorization) != 1:
            raise AuthenticationFailed()
        return await run_in_threadpool(identity.authenticate, authorization[0])

    async def query(request: Request):
        actor = await principal(request)
        if request.query_params or request.path_params["kind"] not in {"metadata", "policy"}:
            raise InvalidRequest("Query fields are not supported")
        value = Query(request.path_params["tenant_id"], request.path_params["project_id"],
                      request.path_params["kind"])
        value.validate()
        return await run_in_threadpool(store.query, actor, value)

    async def json_body(request: Request, maximum=32768):
        if (request.query_params or request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json"
                or request.headers.get("content-encoding", "identity") != "identity"):
            raise InvalidRequest("A JSON command is required")
        body = bytearray()
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(body) + len(chunk) > maximum:
                    raise InvalidRequest("Command exceeds its byte limit")
                body.extend(chunk)
        return json.loads(body, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))

    async def command(request: Request, *, host_command=False):
        actor = await principal(request)
        value = await json_body(request)
        allowed = {"enroll_host", "revoke_host"} if host_command else {"set_policy"}
        if (type(value) is not dict or set(value) != {"protocol_version", "command_id", "expected_revision", "operation", "payload"}
                or value["operation"] not in allowed):
            raise InvalidRequest("Unsupported command fields or operation")
        envelope = CommandEnvelope(tenant_id=request.path_params["tenant_id"], project_id=request.path_params["project_id"], **value)
        envelope.semantics()
        target = hosts.owner_command if host_command else store.command
        return await run_in_threadpool(target, actor, envelope)

    async def host_command(request: Request):
        return await command(request, host_command=True)

    def content_scope(request):
        return tuple(request.path_params[key] for key in ("tenant_id", "project_id", "content_id"))

    async def content_publish(request):
        actor = await principal(request)
        # Base64 expands one MiB to 1,398,104 bytes. The small extra allowance
        # covers the fixed envelope, not a second unbounded decoded allocation.
        value = await json_body(request, maximum=1398104 + 4096)
        if (type(value) is not dict or set(value) != {
                "command_id", "data_base64", "media_type", "retention_seconds"}
                or type(value["data_base64"]) is not str or len(value["data_base64"]) > 1398104):
            raise InvalidRequest("Invalid content upload")
        data = base64.b64decode(value["data_base64"], validate=True)
        if not 1 <= len(data) <= 1048576 or base64.b64encode(data).decode("ascii") != value["data_base64"]:
            raise InvalidRequest("Invalid content upload")
        return await run_in_threadpool(content.publish, actor, *content_scope(request),
            command_id=value["command_id"], content=data, media_type=value["media_type"],
            retention_seconds=value["retention_seconds"])

    async def content_inspect(request):
        actor = await principal(request)
        if request.query_params:
            raise InvalidRequest("Metadata query fields are not supported")
        return await run_in_threadpool(content.inspect, actor, *content_scope(request))

    async def content_read(request):
        actor = await principal(request)
        pairs = list(request.query_params.multi_items())
        if len(pairs) != len(dict(pairs)) or set(dict(pairs)) - {"offset", "limit"}:
            raise InvalidRequest("Invalid content page")
        values = {"offset": 0, "limit": 16384}
        for key, value in pairs:
            if not value.isascii() or not value.isdecimal() or len(value) > 10:
                raise InvalidRequest("Invalid content page")
            values[key] = int(value)
        result = await run_in_threadpool(content.read, actor, *content_scope(request), **values)
        return {**{key: value for key, value in result.items() if key != "data"},
                "data_base64": base64.b64encode(result["data"]).decode("ascii")}

    async def content_retain(request):
        actor = await principal(request)
        value = await json_body(request)
        if (type(value) is not dict or set(value) not in (
                {"command_id", "expected_revision", "operation"},
                {"command_id", "expected_revision", "operation", "reason"})):
            raise InvalidRequest("Invalid retention command")
        return await run_in_threadpool(content.retain, actor, *content_scope(request), **value)

    async def runs_inspect(request):
        actor = await principal(request)
        pairs = list(request.query_params.multi_items())
        values = dict(pairs)
        if len(pairs) != len(values) or set(values) - {"after", "limit"}:
            raise InvalidRequest("Invalid monitoring page")
        if "limit" in values:
            limit = values["limit"]
            if not limit.isascii() or not limit.isdecimal() or len(limit) > 2:
                raise InvalidRequest("Invalid monitoring page")
            values["limit"] = int(limit)
        return await run_in_threadpool(monitoring.inspect, actor, request.path_params["tenant_id"],
                                      request.path_params["project_id"], **values)

    async def run_control(request):
        actor = await principal(request)
        value = await json_body(request)
        if type(value) is not dict or set(value) != {"command_id", "expected_revision", "expected_epoch", "expected_local_revision", "operation"}:
            raise InvalidRequest("Invalid remote control")
        return await run_in_threadpool(monitoring.request_control, actor, request.path_params["tenant_id"], request.path_params["project_id"],
                                      binding_id=request.path_params["binding_id"], **value)

    async def sharing_policy(request):
        actor = await principal(request)
        scope = {key: request.path_params[key] for key in ("tenant_id", "project_id")}
        if request.method == "GET":
            if request.query_params:
                raise InvalidRequest("Unsupported sharing policy query")
            return await run_in_threadpool(collaboration.inspect_policy, actor, **scope)
        value = await json_body(request)
        if type(value) is not dict or set(value) != {"command_id", "expected_revision", "policy"}:
            raise InvalidRequest("Unsupported sharing policy fields")
        return await run_in_threadpool(collaboration.set_policy, actor, **scope, **value)

    async def sharing_content(request):
        actor = await principal(request)
        target = tuple(request.path_params[key] for key in ("tenant_id", "kind", "resource_id"))
        if request.method == "GET":
            if request.query_params:
                raise InvalidRequest("Unsupported sharing retention query")
            return await run_in_threadpool(sharing_retention.inspect, actor, *target)
        value = await json_body(request)
        if (type(value) is not dict or set(value) not in (
                {"command_id", "expected_revision", "operation"},
                {"command_id", "expected_revision", "operation", "reason"})):
            raise InvalidRequest("Invalid sharing retention command")
        return await run_in_threadpool(sharing_retention.retain, actor, *target, **value)

    def endpoint(handler):
        async def respond(request):
            try:
                result = await handler(request)
                return JSONResponse(result, headers=_HEADERS)
            except AuthenticationFailed:
                return JSONResponse({"error": "authentication_required"}, status_code=401,
                                    headers={**_HEADERS, "WWW-Authenticate": "Bearer"})
            except AccessDenied:
                return JSONResponse({"error": "resource_unavailable"}, status_code=403, headers=_HEADERS)
            except Conflict:
                return JSONResponse({"error": "command_conflict"}, status_code=409, headers=_HEADERS)
            except (InvalidRequest, UnsupportedVersion, ValueError, TypeError, TimeoutError):
                return JSONResponse({"error": "invalid_request"}, status_code=400, headers=_HEADERS)
            except Exception:
                # Never format an exception, token, DSN, body, or request URL.
                # Catch here so the ASGI server cannot log a secret traceback.
                _log.warning("Governance operation unavailable")
                return JSONResponse({"error": "service_unavailable"}, status_code=503, headers=_HEADERS)
        return respond

    routes = [
        Route("/v1/tenants/{tenant_id}/projects/{project_id}/{kind}", endpoint(query), methods=["GET"]),
        Route("/v1/tenants/{tenant_id}/projects/{project_id}/commands", endpoint(command), methods=["POST"]),
    ]
    if monitoring is not None:
        # The generic {kind} route must not consume the monitoring list path.
        routes.insert(0, Route("/v1/tenants/{tenant_id}/projects/{project_id}/runs", endpoint(runs_inspect), methods=["GET"]))
        routes.append(Route("/v1/tenants/{tenant_id}/projects/{project_id}/runs/{binding_id}/controls", endpoint(run_control), methods=["POST"]))
    if hosts is not None:
        routes.append(Route("/v1/tenants/{tenant_id}/projects/{project_id}/hosts/commands", endpoint(host_command), methods=["POST"]))
    if content is not None:
        path = "/v1/tenants/{tenant_id}/projects/{project_id}/content/{content_id}"
        routes.extend([
            Route(path, endpoint(content_publish), methods=["POST"]),
            Route(path, endpoint(content_inspect), methods=["GET"]),
            Route(path + "/bytes", endpoint(content_read), methods=["GET"]),
            Route(path + "/retention", endpoint(content_retain), methods=["POST"]),
        ])
    if collaboration is not None:
        routes.insert(0, Route("/v1/tenants/{tenant_id}/projects/{project_id}/sharing/policy", endpoint(sharing_policy), methods=["GET", "POST"]))
    if sharing_retention is not None:
        path = "/v1/tenants/{tenant_id}/sharing/content/{kind}/{resource_id}"
        routes.extend([Route(path, endpoint(sharing_content), methods=["GET"]),
                       Route(path + "/retention", endpoint(sharing_content), methods=["POST"])])
    # Specific administration/host/audit routes precede the generic {kind} view.
    routes = administration_routes(store, principal=principal, json_body=json_body, endpoint=endpoint, hosts=hosts) + routes
    return Starlette(debug=False, routes=routes)
