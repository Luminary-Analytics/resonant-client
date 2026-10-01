"""Separate tenant-bound SCIM transport; no human or host bearer fallback.

The current provisioning credential resolves tenant/issuer in the store. URLs,
documents, query fields and forwarded headers cannot supply either authority.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import json
import logging

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .identity import _unique_object
from .models import AccessDenied
from .scim import ScimError

_log = logging.getLogger(__name__)
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


@dataclass(frozen=True, slots=True)
class ScimTransportConfig:
    profile: str = "production"
    allow_test_loopback: bool = False

    def __post_init__(self):
        if (self.profile not in {"production", "test"} or type(self.allow_test_loopback) is not bool
                or self.profile == "test" and not self.allow_test_loopback
                or self.profile == "production" and self.allow_test_loopback):
            raise ValueError("SCIM test transport requires both explicit loopback switches")


def _error(status, detail, scim_type=None, *, authenticate=False):
    value = {"schemas": [_ERROR_SCHEMA], "status": str(status), "detail": detail}
    if scim_type is not None:
        value["scimType"] = scim_type
    headers = {**_HEADERS, **({"WWW-Authenticate": "Bearer"} if authenticate else {})}
    return JSONResponse(value, status_code=status, headers=headers, media_type="application/scim+json")


def create_scim_app(store, *, config: ScimTransportConfig | None = None) -> Starlette:
    """Build bounded SCIM Users/Groups endpoints backed by current DB authority."""
    config = config or ScimTransportConfig()

    async def principal(request):
        if config.profile == "test":
            try:
                allowed = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
            except ValueError:
                allowed = False
        else:
            allowed = request.url.scheme == "https"
        values = request.headers.getlist("authorization")
        if not allowed or len(values) != 1:
            raise AccessDenied("Provisioning authentication required")
        return await run_in_threadpool(store.authenticate, values[0])

    async def document(request):
        if (request.headers.get("content-type", "").split(";", 1)[0].strip() not in {
                "application/scim+json", "application/json"}
                or request.headers.get("content-encoding", "identity") != "identity"):
            raise ScimError(400, "invalidSyntax", "A SCIM JSON document is required")
        encoded = bytearray()
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(encoded) + len(chunk) > 262144:
                    raise ScimError(413, "tooLarge", "SCIM document exceeds its byte limit")
                encoded.extend(chunk)
        value = json.loads(encoded, object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
        if type(value) is not dict:
            raise ScimError(400, "invalidSyntax", "A SCIM object is required")
        return value

    async def empty_body(request):
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if chunk:
                    raise ScimError(400, "invalidSyntax", "This operation has no request body")

    def query(request, *, collection):
        pairs = list(request.query_params.multi_items())
        values = dict(pairs)
        if len(values) != len(pairs) or set(values) - ({"startIndex", "count", "filter"} if collection else set()):
            raise ScimError(400, "invalidSyntax", "Unsupported SCIM query")
        result = {"start_index": 1, "count": 100}
        for field, target in (("startIndex", "start_index"), ("count", "count")):
            if field in values:
                value = values[field]
                if not value.isascii() or not value.isdecimal() or len(value) > 9:
                    raise ScimError(400, "invalidValue", "Invalid SCIM page")
                result[target] = int(value)
        if "filter" in values:
            if len(values["filter"]) > 1024:
                raise ScimError(400, "invalidFilter", "SCIM filter exceeds its limit")
            result["filter"] = values["filter"]
        return result

    def match(request):
        values = request.headers.getlist("if-match")
        if len(values) != 1:
            raise ScimError(428, None, "An exact resource If-Match is required")
        return values[0]

    async def resources(request: Request):
        actor = await principal(request)
        resource_type = request.path_params["resource_type"]
        if resource_type not in {"Users", "Groups"}:
            raise ScimError(404, None, "Resource unavailable")
        resource_id = request.path_params.get("resource_id")
        fields = query(request, collection=request.method == "GET" and resource_id is None)
        status = 200
        if request.method == "GET":
            await empty_body(request)
            result = await run_in_threadpool(store.get, actor, resource_type, resource_id) if resource_id else (
                await run_in_threadpool(store.list, actor, resource_type, **fields))
        elif request.method == "POST" and resource_id is None:
            result = await run_in_threadpool(store.create, actor, resource_type, await document(request))
            status = 201
        elif request.method in {"PUT", "PATCH"} and resource_id is not None:
            if_match = match(request)
            target = store.replace if request.method == "PUT" else store.patch
            result = await run_in_threadpool(target, actor, resource_type, resource_id, await document(request), if_match)
        elif request.method == "DELETE" and resource_id is not None:
            await empty_body(request)
            await run_in_threadpool(store.delete, actor, resource_type, resource_id, match(request))
            return Response(status_code=204, headers=_HEADERS)
        else:
            raise ScimError(405, None, "Operation unavailable")
        headers = dict(_HEADERS)
        if "meta" in result and "version" in result["meta"]:
            headers["ETag"] = result["meta"]["version"]
        if status == 201:
            # Never reflect a caller-controlled Host or forwarded URL.
            headers["Location"] = f"/scim/v2/{resource_type}/{result['id']}"
        return JSONResponse(result, status_code=status, headers=headers, media_type="application/scim+json")

    async def respond(request):
        try:
            return await resources(request)
        except AccessDenied:
            return _error(401, "Provisioning authentication required", authenticate=True)
        except ScimError as exc:
            return _error(exc.status, exc.detail, exc.scim_type)
        except (ValueError, TypeError, TimeoutError):
            return _error(400, "Invalid SCIM request", "invalidSyntax")
        except Exception:
            _log.warning("SCIM operation unavailable")
            return _error(503, "Service unavailable")

    async def routing_error(request, exc):
        return _error(exc.status_code, "Operation unavailable")

    return Starlette(debug=False, routes=[
        Route("/scim/v2/{resource_type}", respond, methods=["GET", "POST"]),
        Route("/scim/v2/{resource_type}/{resource_id}", respond, methods=["GET", "PUT", "PATCH", "DELETE"]),
    ], exception_handlers={HTTPException: routing_error})
