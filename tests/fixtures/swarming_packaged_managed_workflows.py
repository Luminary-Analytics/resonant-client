"""External infrastructure for unmodified frozen managed writing and recovery."""
from dataclasses import asdict
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(SOURCE), str(SOURCE / "services/governance/src"), str(SOURCE / "services/governance/tests")]

from lumi.engine.swarming.managed_client import HostChannelClient  # noqa: E402
from lumi.engine.swarming.service import _READ_TOOLS, _WRITE_TOOLS  # noqa: E402
from sonn_governance.hosts import HostGovernance  # noqa: E402
from sonn_governance.managed_resources import ManagedResources  # noqa: E402
from sonn_governance.models import CommandEnvelope  # noqa: E402
from sonn_governance.monitoring import RunMonitoring  # noqa: E402
from swarming_frozen_managed_client import FrozenManagedClient  # noqa: E402
from test_host_http import actual_host, certificates as certificate_fixture  # noqa: E402
from test_managed_host_client import configuration  # noqa: E402
from test_store import Tenant, uid  # noqa: E402


def main(root, config, executable, *, writer):
    root.mkdir(exist_ok=True)
    if os.name == "nt":
        subprocess.run(["icacls", str(root), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:(OI)(CI)(F)",
            "*S-1-5-18:(OI)(CI)(F)", "*S-1-5-32-544:(OI)(CI)(F)"], check=True, capture_output=True, timeout=10)
    database = json.loads(config.read_text(encoding="utf-8-sig"))
    workspace, operator = root / "project", root / "operator"
    workspace.mkdir()
    operator.mkdir()
    (workspace / "fact.txt").write_text("PRIVATE managed fact: quoted CSV fields preserve commas.\n", encoding="utf-8")
    if writer:
        from tests.test_swarm_integration import git
        (workspace / "src").mkdir()
        (workspace / "src/value.txt").write_text("original\n")
        (workspace / "personal.txt").write_text("committed personal\n")
        (workspace / "verify_value.py").write_text("from pathlib import Path\n"
            "assert Path('src/value.txt').read_text() == 'verified change\\n'\nprint('Exact managed writer value verified')\n")
        git(workspace, "init", "-b", "main")
        git(workspace, "add", ".")
        git(workspace, "commit", "-m", "Frozen managed fixture baseline")
    certificates = certificate_fixture.__wrapped__(SimpleNamespace(mktemp=lambda _: operator))
    tenant = Tenant(database)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "metadata_read", "control_execute"])
    tenant.store.command(tenant.admin, tenant.command(allowed_models=[{"provider": "ollama", "model": "chosen"}],
        allowed_tools=sorted(_READ_TOOLS | (_WRITE_TOOLS if writer else frozenset())), request_limit=20,
        **({"policy_version": 2, "allowed_effects": ["writer_git", "candidate_git", "candidate_check", "checkout_apply"]} if writer else {})))
    hosts = HostGovernance(tenant.store, minimum_runner_protocol=2)
    monitoring = RunMonitoring(hosts)
    pending = hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, 0, "enroll_host",
        {"host_id": uid(), "certificate_sha256": certificates.first.fingerprint}))
    with actual_host(hosts, certificates, monitoring=monitoring, resources=ManagedResources(hosts)) as (url, _):
        transport = configuration(url, certificates, certificates.first)
        active = HostChannelClient(transport).activate(pending["challenge"])
        config_path = operator / "managed.json"
        config_path.write_text(json.dumps({"version": 1, "transport": asdict(transport), "tenant_id": tenant.id,
            "project_id": tenant.project, "host_id": active["host_id"], "host_generation": active["host_generation"],
            "owner_id": tenant.admin.actor_id, "local_owner_id": "local:" + getpass.getuser(),
            "workspace": str(workspace), "policy_revision": 1}))
        for file in operator.iterdir():
            file.chmod(0o600)

        def evidence():
            import psycopg
            from psycopg.rows import dict_row
            remote = monitoring.inspect(tenant.admin, tenant.id, tenant.project)
            with psycopg.connect(database["owner_dsn"], row_factory=dict_row) as connection:
                effects = [{key: str(value) for key, value in row.items()} for row in connection.execute(
                    "SELECT effect_id,kind,state,semantics_sha256 FROM sonn_governance.owner_effects WHERE tenant_id=%s", (tenant.id,))]
                requests = [{key: str(value) for key, value in row.items()} for row in connection.execute(
                    "SELECT request_id,state,consumed FROM sonn_governance.host_requests WHERE tenant_id=%s", (tenant.id,))]
            encoded = json.dumps(remote)
            return {"remote": remote, "owner_effects": effects, "remote_requests": requests,
                    "remote_contains_content": any(value in encoded or json.dumps(value)[1:-1] in encoded for value in ["PRIVATE", str(workspace)])}

        client = FrozenManagedClient(root, config_path, executable, writer=writer, evidence=evidence, allow_restart=not writer)
        if writer:
            client.gate.set()
        try:
            print(json.dumps({**client.info, "python": sys.executable}), flush=True)
            sys.stdin.read()
        finally:
            client.close()


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(strict=True), Path(sys.argv[3]).resolve(strict=True),
         writer="--writer" in sys.argv[4:])
