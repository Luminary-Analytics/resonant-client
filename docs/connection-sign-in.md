# Signing in to enterprise model endpoints

A custom connection (**Settings > Connections > Custom connections**) normally
sends a key. Company gateways and Azure OpenAI often want a sign-in instead,
and some require a client certificate. Lumi supports three methods, chosen with
the connection's **Authentication** setting (`auth` in `settings.json`). Code:
`lumi/auth_tokens.py`, used by `sign_in()` in `lumi/connections.py`.

| Method | Connection types | What Lumi sends to the model endpoint |
|---|---|---|
| **OAuth client credentials** (`oauth`) | OpenAI-compatible, OpenAI and Anthropic proxies, Azure OpenAI | `Authorization: Bearer <access token>` |
| **Microsoft Entra ID** (`entra`) | Azure OpenAI | `Authorization: Bearer <Entra token>`, instead of `api-key` |
| **Client certificate** (mTLS) | Every type except Bedrock and Vertex AI, with any sign-in | The certificate, during the TLS handshake |

## OAuth client credentials

For gateways behind an identity provider (Okta, Auth0, Keycloak, Entra ID,
Ping and others) that accepts the OAuth 2.0 client-credentials grant
(RFC 6749 §4.4).

| Field | Meaning |
|---|---|
| **Token URL** | The identity provider's token endpoint, for example `https://login.example.com/oauth2/token`. HTTPS, except on localhost and private networks. |
| **Client id** | The application registered for Lumi. |
| **Client secret** | Kept in the credential store with your other keys (`api_keys.conn_<id>`), never shown again or returned to the page. |
| **Scope**, **Audience** (optional) | Sent when set. Some providers need one or the other. |

Lumi posts the client id and secret to the token URL, then sends the access
token to the model endpoint. The secret goes only to the token URL, never to
the model endpoint. Tokens are kept in memory and fetched again a minute
before they expire.

## Microsoft Entra ID (Azure OpenAI)

Choose **Microsoft Entra ID** on an Azure OpenAI connection. There are two ways
to sign in:

- **An app registration:** enter the **Tenant id** (a GUID or
  `contoso.onmicrosoft.com`), the **Client id**, and its **Client secret**. Lumi
  signs in at `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`.
  If the client id has no secret, sign-in fails; Lumi never uses whoever is
  signed in on the computer instead.
- **This computer's sign-in:** leave the client id blank. Lumi uses
  `azure-identity` when it is installed (`pip install azure-identity`), which
  covers managed identities, environment credentials and developer tools. Else
  it runs the Azure CLI: `az account get-access-token`, with `--tenant` when a
  tenant is set. Run `az login` first.

The scope defaults to `https://cognitiveservices.azure.com/.default`. The
role assignment on the Azure OpenAI resource (for example *Cognitive Services
OpenAI User*) decides what the identity can do.

## Client certificates

Gateways that require mutual TLS take a **Client certificate** (a PEM file)
and a **Client key** when the key isn't in the same file. Lumi reads the files
where they are; it doesn't copy them. Keys protected by a passphrase aren't
supported yet; Lumi says so instead of prompting for one.

The server's certificate is still checked against the trust store Lumi uses,
including the operating system's certificates when **Settings > Connections >
Network > Use the system certificate store** is on.

## Checking and troubleshooting

**Test connection** signs in before it lists models, so a refusal shows the
identity provider's own reason, for example *Sign-in was refused: invalid
client secret*. An unreadable certificate reports *The client certificate
couldn't be loaded*. Lumi doesn't put the secret or the token in its messages.

| Message | Check |
|---|---|
| *Sign-in was refused: …* | The client id, secret, scope or audience, as the identity provider reports. |
| *The token endpoint didn't answer* | The token URL, proxy settings, and whether the endpoint is reachable from this computer. |
| *No Entra ID sign-in is available* | Run `az login` (with the right tenant), or install `azure-identity`, or use an app registration. |
| *…needs its client secret* | Enter the client secret, or clear the client id to use the computer's sign-in. |
| The model endpoint returns 401 or 403 | The token is valid but lacks access: the role assignment, the audience or the scope. |

## What has been verified

These methods were tested against simulated token endpoints, a simulated
Azure CLI, and a local HTTPS server that requires a client certificate
(a real TLS handshake with certificates generated for the test). They haven't
yet been tried against a real identity provider, an Entra tenant or a company
mTLS gateway. Please report differences you find.
