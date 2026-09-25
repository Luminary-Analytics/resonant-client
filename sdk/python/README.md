# lumi-extension

The Python half of Lumi's Extension SDK v1: write a model provider that Lumi
runs as a separate process, and test it the way Lumi calls it. Standard
library only.

- `lumi_extension`: `Provider`, `serve`, and the events `text`,
  `tool_call`, `done` and `error`.
- `lumi_extension.testing`: `call` runs your provider like Lumi does;
  `check_models` and `check_stream` say what Lumi would reject.

Copy the `lumi_extension` folder into your pack, or install this folder with
`pip install ./sdk/python`. Start from `sdk/templates/provider-python`, and
see [docs/extensions.md](../../docs/extensions.md) for the manifest and the
protocol.
