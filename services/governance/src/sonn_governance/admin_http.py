"""Explicit human routes; authenticated commands reuse the transactional core."""

from starlette.concurrency import run_in_threadpool
from starlette.routing import Route

from .administration import Administration
from .grants import GrantApprovals
from .models import CommandEnvelope, InvalidRequest, Query
from .scim import ScimStore


def routes(store, *, principal, json_body, endpoint, hosts=None):
    administration, grants, scim = Administration(store), GrantApprovals(store), ScimStore(store)

    def page(request, *, uuid_cursor=False):
        pairs = list(request.query_params.multi_items())
        values = dict(pairs)
        if len(pairs) != len(values) or set(values) - {"after", "limit"}:
            raise InvalidRequest("invalid page")
        for key in ("limit",) if uuid_cursor else ("after", "limit"):
            if key in values:
                value = values[key]
                if not value.isascii() or not value.isdecimal() or len(value) > 19:
                    raise InvalidRequest("invalid page")
                values[key] = int(value)
        return values

    async def identity(request):
        actor = await principal(request)
        if request.query_params:
            raise InvalidRequest("identity query fields are unsupported")
        return {"actor_id": actor.actor_id, "expires_at": actor.expires_at}

    async def membership(request):
        actor = await principal(request)
        return await run_in_threadpool(administration.membership, actor, request.path_params["tenant_id"],
                                      request.path_params["actor_id"], **page(request, uuid_cursor=True))

    async def tenant_command(request):
        actor = await principal(request)
        value = await json_body(request)
        allowed = {"set_membership": store.command, "decide_grant": grants.command,
                   "revoke_grant": grants.command, "map_scim_group": scim.map_group}
        if (type(value) is not dict or set(value) != {"protocol_version", "command_id", "expected_revision", "operation", "payload"}
                or type(value["operation"]) is not str or value["operation"] not in allowed):
            raise InvalidRequest("unsupported administration command")
        envelope = CommandEnvelope(tenant_id=request.path_params["tenant_id"], project_id=None, **value)
        envelope.semantics()
        return await run_in_threadpool(allowed[value["operation"]], actor, envelope)

    async def grant(request):
        actor = await principal(request)
        if request.query_params:
            raise InvalidRequest("grant query fields are unsupported")
        return await run_in_threadpool(grants.inspect, actor, request.path_params["tenant_id"], request.path_params["request_id"])

    async def audit(request):
        actor = await principal(request)
        query = Query(request.path_params["tenant_id"], request.path_params.get("project_id"), "audit", **page(request))
        query.validate()
        return await run_in_threadpool(store.query, actor, query)

    async def host_list(request):
        actor = await principal(request)
        query = Query(request.path_params["tenant_id"], request.path_params["project_id"], "metadata", **page(request))
        query.validate()
        return await run_in_threadpool(hosts.inspect, actor, query)

    result = [
        Route("/v1/identity", endpoint(identity), methods=["GET"]),
        Route("/v1/tenants/{tenant_id}/members/{actor_id}", endpoint(membership), methods=["GET"]),
        Route("/v1/tenants/{tenant_id}/commands", endpoint(tenant_command), methods=["POST"]),
        Route("/v1/tenants/{tenant_id}/grants/{request_id}", endpoint(grant), methods=["GET"]),
        Route("/v1/tenants/{tenant_id}/audit", endpoint(audit), methods=["GET"]),
        Route("/v1/tenants/{tenant_id}/projects/{project_id}/audit", endpoint(audit), methods=["GET"]),
    ]
    if hosts is not None:
        result.append(Route("/v1/tenants/{tenant_id}/projects/{project_id}/hosts", endpoint(host_list), methods=["GET"]))
    return result
