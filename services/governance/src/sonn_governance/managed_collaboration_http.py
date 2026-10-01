"""Fixed collaboration routes for the existing certificate-authenticated server."""

from .models import InvalidRequest


ROUTES = {
    "/v1/sharing/offer": ("offer", {"lease_id", "command_id", "origin_binding", "receiver_binding", "terms"}, set()),
    "/v1/sharing/approve": ("approve", {"lease_id", "command_id", "grant_id", "terms_sha256"}, set()),
    "/v1/sharing/revoke": ("revoke", {"lease_id", "command_id", "grant_id"}, set()),
    "/v1/sharing/send": ("send", {"lease_id", "command_id", "grant_id", "kind", "data_class", "body"}, {"parent_id"}),
    "/v1/sharing/deliver": ("deliver", {"lease_id", "command_id", "message_id"}, set()),
    "/v1/sharing/accept": ("accept_work", {"lease_id", "command_id", "message_id", "worker_id", "request_limit", "contract_sha256"}, set()),
    "/v1/sharing/inspect": ("inspect", {"lease_id", "binding_id"}, {"before_grant_id", "limit"}),
    "/v1/sharing/terms": ("inspect_terms", {"lease_id", "grant_id"}, set()),
    "/v1/sharing/messages": ("inspect_messages", {"lease_id", "grant_id"}, {"before_message_id", "limit"}),
}


def dispatch_collaboration(collaboration, principal, path, payload):
    """Caller supplies the verified mTLS principal, never JSON identity fields."""
    route = ROUTES.get(path)
    if not route or type(payload) is not dict:
        raise InvalidRequest("unknown sharing operation")
    method, required, optional = route
    if not required <= set(payload) or set(payload) - required - optional:
        raise InvalidRequest("invalid sharing operation fields")
    return getattr(collaboration, method)(principal, **payload)
