"""Enterprise model sign-in (lumi/auth_tokens.py): OAuth client credentials,
Microsoft Entra ID and client certificates for connections."""

from __future__ import annotations

import datetime
import json
import ssl
import subprocess
import sys
from urllib.parse import parse_qs

import httpx
import pytest

from lumi import auth_tokens
from lumi.auth_tokens import SignInError, client_credentials_token, entra_token, ssl_context
from lumi.connections import create_connection_backend, discover_models, normalize_connection

SECRET = "client-secret-for-tests-0123456789"


@pytest.fixture(autouse=True)
def fresh_cache():
    auth_tokens.clear_cache()
    yield
    auth_tokens.clear_cache()


class TokenServer:
    def __init__(self, token="tok-1", expires_in=3600, status=200, error=None):
        self.requests: list[httpx.Request] = []
        self.token, self.expires_in, self.status, self.error = token, expires_in, status, error

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/token"):
            if self.status >= 400:
                return httpx.Response(self.status, json=self.error or {"error": "invalid_client"})
            return httpx.Response(200, json={"access_token": self.token, "expires_in": self.expires_in})
        # Anything else is the model endpoint; report what it received.
        return httpx.Response(200, json={"data": [{"id": "gateway-model"}],
                                         "auth": request.headers.get("authorization", "")})

    def forms(self):
        return [parse_qs(r.content.decode()) for r in self.requests if r.url.path.endswith("/token")]


def cert_files(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lumi-test-client")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "client.pem", tmp_path / "client.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return str(cert_path), str(key_path)


class TestOAuth:
    def test_client_credentials_are_exchanged_and_cached(self, monkeypatch):
        server = TokenServer()
        transport = httpx.MockTransport(server)
        token = client_credentials_token("https://login.example.com/oauth2/token", "app-1", SECRET,
                                         scope="models.read", audience="gateway", transport=transport)
        assert token == "tok-1"
        assert client_credentials_token("https://login.example.com/oauth2/token", "app-1", SECRET,
                                        scope="models.read", audience="gateway", transport=transport) == "tok-1"
        [form] = server.forms()  # the second call used the cache
        assert form == {"grant_type": ["client_credentials"], "client_id": ["app-1"], "client_secret": [SECRET],
                        "scope": ["models.read"], "audience": ["gateway"]}
        # A token about to expire is fetched again.
        real_time = auth_tokens.time.time
        monkeypatch.setattr(auth_tokens.time, "time", lambda: real_time() + 3590)
        server.token = "tok-2"
        assert client_credentials_token("https://login.example.com/oauth2/token", "app-1", SECRET,
                                        scope="models.read", audience="gateway", transport=transport) == "tok-2"

    def test_a_refusal_names_the_problem_not_the_secret(self):
        server = TokenServer(status=401, error={"error": "invalid_client", "error_description": "Bad client secret"})
        with pytest.raises(SignInError, match="Bad client secret") as info:
            client_credentials_token("https://login.example.com/oauth2/token", "app-1", SECRET,
                                     transport=httpx.MockTransport(server))
        assert SECRET not in str(info.value)
        with pytest.raises(SignInError, match="token URL"):
            client_credentials_token("", "app-1", SECRET)


class TestEntra:
    def test_a_client_secret_uses_the_tenants_token_endpoint(self):
        server = TokenServer(token="entra-1")
        assert entra_token("contoso.onmicrosoft.com", client_id="app-2", client_secret=SECRET,
                           transport=httpx.MockTransport(server)) == "entra-1"
        [request] = server.requests
        assert str(request.url) == "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token"
        assert parse_qs(request.content.decode())["scope"] == ["https://cognitiveservices.azure.com/.default"]

    def test_without_a_client_id_the_azure_cli_signs_in(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "azure.identity", None)  # azure-identity isn't installed
        az = r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"  # a batch file on Windows
        monkeypatch.setattr(auth_tokens.shutil, "which", lambda name: az if name == "az" else None)
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(
                {"accessToken": "cli-token", "expires_on": 4_102_444_800}), stderr="")

        monkeypatch.setattr(auth_tokens.subprocess, "run", fake_run)
        assert entra_token("contoso") == "cli-token"
        command, kwargs = calls[0]
        assert command == [az, "account", "get-access-token", "--resource", "https://cognitiveservices.azure.com",
                           "--output", "json", "--tenant", "contoso"]
        assert "shell" not in kwargs  # and on Windows no console window flashes
        assert entra_token("contoso") == "cli-token" and len(calls) == 1

        def failing(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="ERROR: Please run 'az login'")

        auth_tokens.clear_cache()
        monkeypatch.setattr(auth_tokens.subprocess, "run", failing)
        with pytest.raises(SignInError, match="az login"):
            entra_token("contoso")
        # Without the Azure CLI, the error says how to sign in.
        monkeypatch.setattr(auth_tokens.shutil, "which", lambda name: None)
        with pytest.raises(SignInError, match="client id and secret"):
            entra_token("contoso")

    def test_a_client_id_without_its_secret_never_uses_this_computers_sign_in(self, monkeypatch):
        monkeypatch.setattr(auth_tokens.subprocess, "run", lambda *a, **k: pytest.fail("ran the Azure CLI"))
        with pytest.raises(SignInError, match="needs its client secret"):
            entra_token("contoso", client_id="app-2")

    @pytest.mark.parametrize(("tenant", "scope"), [
        ("contoso & calc", ""), ("%USERPROFILE%", ""), ("contoso", "https://x.example/.default & calc"),
        ("contoso", "api://app one/.default"),
    ])
    def test_arguments_for_the_azure_cli_are_plain(self, monkeypatch, tenant, scope):
        monkeypatch.setitem(sys.modules, "azure.identity", None)
        monkeypatch.setattr(auth_tokens.shutil, "which", lambda name: "az")
        monkeypatch.setattr(auth_tokens.subprocess, "run", lambda *a, **k: pytest.fail("ran the Azure CLI"))
        with pytest.raises(SignInError):
            entra_token(tenant, scope=scope)


class TestClientCertificates:
    def test_a_certificate_and_key_load(self, tmp_path):
        cert, key = cert_files(tmp_path)
        assert isinstance(ssl_context(cert, key), ssl.SSLContext)
        (tmp_path / "bad.pem").write_text("not a certificate", encoding="utf-8")
        with pytest.raises(SignInError, match="couldn't be loaded"):
            ssl_context(str(tmp_path / "bad.pem"))

    def test_a_key_with_a_passphrase_is_refused_without_prompting(self, tmp_path):
        from cryptography.hazmat.primitives import serialization

        cert, key = cert_files(tmp_path)
        private = serialization.load_pem_private_key((tmp_path / "client.key").read_bytes(), None)
        (tmp_path / "locked.key").write_bytes(private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"passphrase")))
        with pytest.raises(SignInError, match="passphrase"):
            ssl_context(cert, str(tmp_path / "locked.key"))


def tls_chain(tmp_path):
    """A certificate authority, a server certificate for localhost and a client certificate."""
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.timezone.utc)

    def issue(common_name, key, issuer_name, issuer_key, *, ca=False, san=None, usage=None):
        builder = (x509.CertificateBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
                   .issuer_name(issuer_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
        if ca:
            builder = builder.add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False,
                decipher_only=False), critical=True)
            builder = builder.add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                                            critical=False)
        else:
            builder = builder.add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
        if san:
            builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
        if usage:
            builder = builder.add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        return builder.sign(issuer_key, hashes.SHA256())

    def write(name, cert, key):
        (tmp_path / f"{name}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (tmp_path / f"{name}.key").write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        return str(tmp_path / f"{name}.pem"), str(tmp_path / f"{name}.key")

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Lumi test CA")])
    ca = issue("Lumi test CA", ca_key, ca_name, ca_key, ca=True)
    server_key, client_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    server = issue("localhost", server_key, ca_name, ca_key, usage=ExtendedKeyUsageOID.SERVER_AUTH,
                   san=[x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    client = issue("lumi-client", client_key, ca_name, ca_key, usage=ExtendedKeyUsageOID.CLIENT_AUTH)
    ca_file, _ = write("ca", ca, ca_key)
    return ca_file, write("server", server, server_key), write("client", client, client_key)


@pytest.fixture
def stdlib_ssl():
    """Use the stdlib ``ssl`` classes for this test.

    truststore may be injected, by Lumi's network settings in another test or
    at interpreter startup (pip's copy), possibly both. Its contexts verify
    only as clients, against the OS store, so each layer is removed and put
    back afterwards.
    """
    removed = []
    while ssl.SSLContext.__module__ != "ssl" and len(removed) < 5:
        truststore = sys.modules[ssl.SSLContext.__module__.rsplit(".", 1)[0]]
        truststore.extract_from_ssl()
        removed.append(truststore)
    if ssl.SSLContext.__module__ != "ssl":
        pytest.skip("ssl.SSLContext is replaced by something other than truststore")
    try:
        yield
    finally:
        for truststore in reversed(removed):
            truststore.inject_into_ssl()


class TestMutualTLS:
    def test_a_gateway_that_requires_a_client_certificate(self, tmp_path, monkeypatch, stdlib_ssl):
        """A real TLS handshake: the gateway only answers a caller presenting a certificate from its CA."""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        ca_file, (server_cert, server_key), (client_cert, client_key) = tls_chain(tmp_path)
        seen = []

        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                peer = self.connection.getpeercert() or {}
                seen.append(dict(item[0] for item in peer.get("subject", ())).get("commonName"))
                body = json.dumps({"data": [{"id": "mtls-model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(server_cert, server_key)
        context.load_verify_locations(ca_file)
        context.verify_mode = ssl.CERT_REQUIRED
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        # Trust the test CA the way an administrator's SSL_CERT_FILE would.
        monkeypatch.setenv("SSL_CERT_FILE", ca_file)
        base_url = f"https://127.0.0.1:{httpd.server_address[1]}/v1"  # verified by the IP in the certificate
        try:
            connection = normalize_connection({"name": "mTLS gateway", "type": "openai-compatible",
                                               "base_url": base_url, "auth": "none",
                                               "client_cert": client_cert, "client_key": client_key})
            assert discover_models(connection) == ["mtls-model"]
            assert seen == ["lumi-client"]
            # The same trust without the client certificate is turned away.
            with pytest.raises(httpx.HTTPError):
                with httpx.Client(verify=ssl.create_default_context(), timeout=5) as client:
                    client.get(f"{base_url}/models").raise_for_status()
            assert discover_models({**connection, "client_cert": "", "client_key": ""}) == []
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestConnections:
    def test_validation(self, tmp_path):
        cert, key = cert_files(tmp_path)
        oauth = normalize_connection({"name": "Gateway", "type": "openai-compatible",
                                      "base_url": "https://llm.example.com/v1", "auth": "oauth",
                                      "token_url": "https://login.example.com/oauth2/token", "client_id": "app-1",
                                      "scope": "models.read models.write", "client_cert": cert, "client_key": key})
        assert (oauth["auth"], oauth["scope"], oauth["client_cert"]) == ("oauth", "models.read models.write", cert)
        for raw, message in [
            ({"type": "openai-compatible", "base_url": "https://x.example.com/v1", "auth": "entra"}, "Azure OpenAI"),
            ({"type": "openai-compatible", "base_url": "https://x.example.com/v1", "auth": "oauth",
              "token_url": "https://login.example.com/t"}, "client id"),
            ({"type": "openai-compatible", "base_url": "https://x.example.com/v1", "auth": "oauth",
              "client_id": "a"}, "token URL"),
            ({"type": "anthropic-bedrock", "region": "us-east-1", "models": ["m"], "auth": "oauth"}, "HTTP gateways"),
            ({"type": "azure-openai", "base_url": "https://n.openai.azure.com/openai/v1", "models": ["d"],
              "auth": "entra", "client_id": "app"}, "tenant id"),
            ({"type": "openai-compatible", "base_url": "https://x.example.com/v1", "client_key": key}, "certificate"),
            ({"type": "openai-compatible", "base_url": "https://x.example.com/v1",
              "client_cert": str(tmp_path / "missing.pem")}, "doesn't exist"),
        ]:
            with pytest.raises(ValueError, match=message):
                normalize_connection({"name": "C", **raw})

    def test_a_gateway_sends_the_oauth_token(self, tmp_path):
        server = TokenServer(token="gw-token")
        transport = httpx.MockTransport(server)
        connection = normalize_connection({"name": "Gateway", "type": "openai-compatible",
                                           "base_url": "https://llm.example.com/v1", "auth": "oauth",
                                           "token_url": "https://login.example.com/oauth2/token", "client_id": "app-1"})
        assert discover_models(connection, SECRET, transport=transport) == ["gateway-model"]
        models_request = next(r for r in server.requests if r.url.path.endswith("/models"))
        assert models_request.headers["authorization"] == "Bearer gw-token"
        backend = create_connection_backend(connection, "gateway-model", SECRET, transport=transport)
        assert backend._request_headers()["Authorization"] == "Bearer gw-token"
        assert SECRET not in json.dumps(backend._request_headers())

    @pytest.mark.parametrize("kind", ["anthropic", "openai"])
    def test_a_proxy_that_signs_in_never_receives_the_secret(self, kind):
        server = TokenServer(token="proxy-token")
        transport = httpx.MockTransport(server)
        connection = normalize_connection({"name": "Proxy", "type": kind, "base_url": "https://llm.example.com",
                                           "auth": "oauth", "token_url": "https://login.example.com/oauth2/token",
                                           "client_id": "app-1"})
        discover_models(connection, SECRET, transport=transport)
        backend = create_connection_backend(connection, "m", SECRET, transport=transport)
        headers = backend._request_headers()
        [listing] = [r for r in server.requests if not r.url.path.endswith("/token")]
        for sent in (dict(listing.headers), headers):
            assert "Bearer proxy-token" in {str(v) for v in sent.values()}
            assert SECRET not in json.dumps({k: str(v) for k, v in sent.items()})

    def test_testing_a_connection_reports_a_refused_sign_in(self, tmp_path, monkeypatch):
        from lumi.gui.settings import SettingsManager
        from tests.test_connections import _command

        refusal = TokenServer(status=400, error={"error": "invalid_client", "error_description": "Unknown client"})
        real_client = httpx.Client
        monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(**{**kw, "transport": httpx.MockTransport(refusal)}))
        settings = SettingsManager(tmp_path / "settings.json")
        sent = _command(settings, "connection_test", api_key=SECRET, connection={
            "name": "Gateway", "type": "openai-compatible", "base_url": "https://llm.example.com/v1",
            "auth": "oauth", "token_url": "https://login.example.com/oauth2/token", "client_id": "app-1"})
        assert sent[0]["data"] == {"ok": False, "message": "Sign-in was refused: Unknown client"}
        assert SECRET not in json.dumps(sent)

    def test_azure_openai_with_entra_and_a_client_certificate(self, tmp_path, monkeypatch):
        cert, key = cert_files(tmp_path)
        connection = normalize_connection({"name": "Azure", "type": "azure-openai",
                                           "base_url": "https://n.openai.azure.com/openai/v1", "models": ["gpt-5.4"],
                                           "auth": "entra", "tenant_id": "contoso", "client_cert": cert,
                                           "client_key": key})
        monkeypatch.setattr(auth_tokens, "entra_token", lambda *a, **k: "entra-token")
        backend = create_connection_backend(connection, "gpt-5.4", "")  # no key needed
        assert backend._request_headers()["Authorization"] == "Bearer entra-token"
        assert "api-key" not in backend._request_headers()
        assert isinstance(backend._tls, ssl.SSLContext)
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"data": [{"id": "gpt-5.4"}]})

        backend._transport = httpx.MockTransport(handler)
        assert backend.health()["models"] == ["gpt-5.4"] and seen["auth"] == "Bearer entra-token"

    def test_the_tls_context_reaches_the_http_client(self, tmp_path):
        from lumi import net

        cert, key = cert_files(tmp_path)
        context = ssl_context(cert, key)
        assert net.client_options(timeout=5, verify=context)["verify"] is context
        connection = normalize_connection({"name": "Gateway", "type": "openai-compatible",
                                           "base_url": "https://llm.example.com/v1", "client_cert": cert,
                                           "client_key": key, "auth": "none"})
        backend = create_connection_backend(connection, "m")
        assert isinstance(backend._tls_options()["verify"], ssl.SSLContext)
