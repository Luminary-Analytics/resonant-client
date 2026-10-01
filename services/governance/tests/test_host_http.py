"""Real TLS clients prove the host boundary uses certificates, never headers."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import socket
import ssl
import threading
import time
from types import SimpleNamespace
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import httpx
import pytest

from sonn_governance.host_http import HostHTTPServer, HostTransportConfig, _HostHandler
from sonn_governance.models import AccessDenied


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("host-certificates")
    now = datetime.now(timezone.utc)
    def issue(name, *, issuer=None, server=False, expired=False):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        is_ca = issuer is None
        signing = key if is_ca else issuer.key
        builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject if is_ca else issuer.cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2)).not_valid_after(now + timedelta(days=-1 if expired else 2))
            .add_extension(x509.BasicConstraints(ca=is_ca, path_length=0 if is_ca else None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=not is_ca,
                data_encipherment=False, key_agreement=False, key_cert_sign=is_ca, crl_sign=is_ca,
                encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signing.public_key()), critical=False))
        if not is_ca:
            builder = builder.add_extension(x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        if server:
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        cert = builder.sign(signing, hashes.SHA256())
        cert_path, key_path = directory / f"{name}.pem", directory / f"{name}.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        return SimpleNamespace(key=key, cert=cert, certificate_file=str(cert_path), private_key_file=str(key_path),
            fingerprint=hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest())
    ca, other_ca = issue("fixture-ca"), issue("foreign-fixture-ca")
    return SimpleNamespace(ca=ca, server=issue("server", issuer=ca, server=True),
        first=issue("first", issuer=ca), second=issue("second", issuer=ca), unknown=issue("unenrolled", issuer=ca),
        foreign=issue("wrong-ca", issuer=other_ca), expired=issue("expired", issuer=ca, expired=True))


@contextmanager
def actual_host(governance, certificates, *, monitoring=None, resources=None):
    config = HostTransportConfig(host="127.0.0.1", port=0, profile="test",
        certificate_file=certificates.server.certificate_file, private_key_file=certificates.server.private_key_file,
        client_ca_file=certificates.ca.certificate_file)
    server = HostHTTPServer(config, governance, monitoring=monitoring, resources=resources)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def client_for(url, certificates, certificate):
    context = ssl.create_default_context(cafile=certificates.ca.certificate_file)
    context.keylog_filename = None
    if certificate is not None:
        context.load_cert_chain(certificate.certificate_file, certificate.private_key_file)
    return httpx.Client(base_url=url, verify=context, trust_env=False, timeout=5)


def test_two_independently_certified_clients_have_distinct_socket_identities(certificates):
    class Governance:
        def activate(self, principal, challenge):
            assert challenge == "fixture-challenge"
            return {"certificate_sha256": principal.certificate_sha256}
    with actual_host(Governance(), certificates) as (url, server):
        assert server._tls.verify_mode == ssl.CERT_REQUIRED and server._tls.keylog_filename is None
        for certificate in (certificates.first, certificates.second):
            with client_for(url, certificates, certificate) as client:
                response = client.post("/v1/activate", json={"challenge": "fixture-challenge"},
                    headers={"X-Client-Cert-SHA256": certificates.unknown.fingerprint,
                             "X-Forwarded-Client-Cert": "forged", "Authorization": "Bearer forged"})
                assert response.status_code == 200
                assert response.json()["certificate_sha256"] == certificate.fingerprint
                assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("certificate", ["foreign", "expired", None])
def test_wrong_ca_expired_and_missing_client_certificate_never_reach_store(certificates, certificate):
    class Governance:
        def activate(self, *args, **kwargs):
            pytest.fail("Unauthenticated TLS peer reached governance")
    with actual_host(Governance(), certificates) as (url, _):
        with client_for(url, certificates, getattr(certificates, certificate) if certificate else None) as client:
            with pytest.raises(httpx.TransportError):
                client.post("/v1/activate", json={"challenge": "fixture"},
                    headers={"X-Forwarded-Client-Cert": certificates.first.fingerprint})


def test_valid_unenrolled_certificate_cannot_borrow_another_host_header(certificates):
    class Governance:
        def lease(self, principal, **kwargs):
            if principal.certificate_sha256 != certificates.first.fingerprint:
                raise AccessDenied("synthetic-private-detail")
            return {"host_id": "first"}
    with actual_host(Governance(), certificates) as (url, _):
        with client_for(url, certificates, certificates.unknown) as client:
            response = client.post("/v1/leases", json={"command_id": str(uuid.uuid4()), "expected_policy_revision": 1},
                headers={"X-Client-Cert-SHA256": certificates.first.fingerprint})
            assert response.status_code == 403 and response.json() == {"error": "host_unavailable"}


@pytest.mark.parametrize("path,body", [
    ("/v1/activate", {"challenge": "fixture", "certificate_sha256": "forged"}),
    ("/v1/leases", {"command_id": str(uuid.uuid4()), "expected_policy_revision": 1, "tenant_id": "forged"}),
    ("/v1/requests/start", {"request_id": str(uuid.uuid4()), "host_id": "forged"}),
    ("/v1/arbitrary", {}),
    ("/v1/activate", {"challenge": "x" * 40000}),
])
def test_fixed_host_routes_reject_scope_injection_and_unbounded_body(certificates, path, body):
    class Governance:
        def __getattr__(self, name):
            pytest.fail("Invalid host request reached a store operation")
    with actual_host(Governance(), certificates) as (url, _):
        with client_for(url, certificates, certificates.first) as client:
            response = client.post(path, json=body)
            assert response.status_code == 400


def test_host_errors_do_not_log_challenge_or_database_credentials(certificates, capsys, caplog):
    secret = "synthetic-private-database-secret"
    class Governance:
        def activate(self, principal, challenge):
            raise RuntimeError(secret + challenge)
    with actual_host(Governance(), certificates) as (url, _):
        with client_for(url, certificates, certificates.first) as client:
            response = client.post("/v1/activate", json={"challenge": secret})
            assert response.status_code == 503 and response.json() == {"error": "service_unavailable"}
    output = capsys.readouterr()
    assert secret not in output.out + output.err + caplog.text


def test_host_json_duplicate_fields_are_rejected(certificates):
    with actual_host(object(), certificates) as (url, _):
        with client_for(url, certificates, certificates.first) as client:
            response = client.post("/v1/activate", content='{"challenge":"first","challenge":"second"}',
                headers={"Content-Type": "application/json"})
            assert response.status_code == 400


def test_oversized_body_arriving_after_headers_retains_complete_tls_error(certificates):
    """A rejection must survive bounded input arriving before its close."""
    class Governance:
        def __getattr__(self, name):
            pytest.fail("Oversized request reached governance")

    context = ssl.create_default_context(cafile=certificates.ca.certificate_file)
    context.load_cert_chain(certificates.first.certificate_file, certificates.first.private_key_file)
    body = json.dumps({"challenge": "x" * 40000}).encode()
    with actual_host(Governance(), certificates) as (_, server):
        with socket.create_connection(server.server_address, timeout=3) as raw:
            with context.wrap_socket(raw, server_hostname="127.0.0.1") as connection:
                connection.sendall(b"POST /v1/activate HTTP/1.1\r\nHost: localhost\r\n"
                    b"Content-Type: application/json\r\nContent-Length: " + str(len(body)).encode()
                    + b"\r\nConnection: close\r\n\r\n")
                # Keep the body in flight when the server rejects its headers.
                for offset in range(0, len(body), 4096):
                    time.sleep(.008)
                    connection.sendall(body[offset:offset + 4096])
                response = bytearray()
                while b"\r\n\r\n" not in response:
                    chunk = connection.recv(4096)
                    assert chunk, "TLS close discarded the rejection headers"
                    response.extend(chunk)
                    assert len(response) <= 16384
                headers, payload = bytes(response).split(b"\r\n\r\n", 1)
                length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:"))
                while len(payload) < length:
                    chunk = connection.recv(length - len(payload))
                    assert chunk, "TLS close discarded the rejection body"
                    payload += chunk
                assert headers.startswith(b"HTTP/1.1 400")
                assert json.loads(payload) == {"error": "invalid_request"}


@pytest.mark.parametrize("limited_by", ["bytes", "time"])
def test_rejection_transport_cleanup_has_fixed_byte_and_time_bounds(monkeypatch, limited_by):
    received, timeouts = [], []

    class Connection:
        def settimeout(self, timeout):
            timeouts.append(timeout)

        def recv(self, count):
            received.append(count)
            return b"x" * count

    handler = _HostHandler.__new__(_HostHandler)
    handler.request = Connection()
    if limited_by == "bytes":
        monkeypatch.setattr("sonn_governance.host_http.time.monotonic", lambda: 0)
    else:
        clock = iter((0, .05, .1, .3))
        monkeypatch.setattr("sonn_governance.host_http.time.monotonic", lambda: next(clock))
    handler._discard_rejected_input()
    assert all(0 < timeout <= .25 for timeout in timeouts)
    assert sum(received) == (65536 if limited_by == "bytes" else 8192)
