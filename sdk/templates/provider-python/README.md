# My provider

A Lumi capability pack that adds a model provider. Made from Lumi's
provider template (Extension SDK v1).

- `lumi-pack.json`: the manifest. `providers` says how Lumi starts
  `provider.py` and which models it offers.
- `provider.py`: the provider. `echo` works offline; `remote` forwards to
  an OpenAI-compatible endpoint.
- `lumi_extension/`: the SDK, copied in so the pack needs nothing installed
  but Python.
- `test_provider.py`: tests that run the provider as Lumi does.

## Try it

```sh
python -m pytest            # the provider's own tests
lumi extension check .      # the manifest and providers, as Lumi sees them
```

Then copy this folder into `~/.lumi/packs/` (or push it to a repository and
use Install from Git in Settings > Capability packs), approve it there, and
add a connection of type **A provider from a capability pack** in Settings >
Connections.

Any change to a file here withdraws the approval until you approve the pack
again, so Lumi never runs code you haven't seen.
