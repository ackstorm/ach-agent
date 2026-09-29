# ACH placements

Placements are `standalone` and `sandboxed`. The `distributed` placement (channels, harness and
engine as three containers over Unix sockets) was removed in v0.18.0, together with
`--role harness`, `--role channels` and the split Dockerfile targets.
`standalone` runs the mini-harness (`--role engine`) as a local child; `sandboxed` runs it in a
claimed agent-sandbox pod. See the `sandbox` section of [operator-contract.md](operator-contract.md).
