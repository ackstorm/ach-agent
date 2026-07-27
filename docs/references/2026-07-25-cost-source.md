# Cost-source evidence record

**Date:** 2026-07-25
**Status:** Reserved; acceptance and release evidence is pending.

This note is the reserved record for the cost-source price-path and streaming-wire
evidence. Unit tests and mocked price responses do not discharge either gate.

## P0-v2 price-path output

To be filled by Phase 3 Task 3.1 after the real ACH price path is exercised. Record the
observed request and response shape, including the location of the pricing fields in the
paginated `/v2/model/info?model=<name>` envelope, the `x-ach-key` authentication result,
and the exact command/output used to accept the path.

**Status:** Pending P0-v2.

## B.7 streaming payloads

To be filled with the real OpenAI and Gemini streaming payloads used to establish usage
semantics. Record the final OpenAI usage chunk, Gemini's cumulative usage chunks, and the
evidence supporting the billable-input/cache and thinking-token treatment.

**Status:** Pending B.7.

## Related

- [`docs/configuration.md`](../configuration.md) — operator-facing source semantics and A.5 failure table.
- [`docs/schemas/operator-contract.md`](../schemas/operator-contract.md) — rendered contract addendum.
