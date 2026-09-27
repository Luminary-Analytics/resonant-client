"""A permanent, operator-created quarantine outside the normal schema version."""

from .models import AccessDenied


RESTORE_LOCK = 734025015


def require_available(connection):
    """Serialize admission with offline sealing, including already-created stores."""
    connection.execute("SELECT pg_advisory_xact_lock_shared(%s)", (RESTORE_LOCK,))
    # pg_namespace is readable without granting runtime access to private restore
    # receipts. Even a malformed/partial marker must close admission.
    if connection.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='sonn_restore') AS sealed").fetchone()["sealed"]:
        raise AccessDenied("database is quarantined for offline recovery")
