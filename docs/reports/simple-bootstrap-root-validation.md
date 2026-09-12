# Independent validation of agent-owned bootstrap

Root reviewed GPT-5.6-Luna's environment forwarding change `98393ad` and bootstrap
implementation `a58c43c` on `feat/phase1-split`.

## Focused tests

- Forwarding/role tests: 27 passed in 2.13 seconds. The generated name projection
  feeds a real native-child environment builder; DEBUG and an explicit custom token
  come from the engine-side environment, while ACH_TOKEN is excluded.
- Bootstrap/role tests at `a58c43c`: 39 passed in 6.44 seconds, including bounded
  bootstrap reads/waits and real subprocess engine-role startup.

## Live three-container bootstrap

Root ran a disposable Compose project, `ach-simple-root`, with three application
containers and a synthetic upstream fixture. The available older combined image
`testbed-agent` (`1932f6bf1984`) supplied native binaries and dependencies; current
source was mounted read-only over `/app/deps/ach_agent`. This is runtime integration
evidence, not verification of the final image entrypoint or packaging.

Only harness received full configuration (`/config.yaml`), ACH_BASE_URL and a
synthetic ACH_TOKEN. No manually generated channels/engine configuration, HMAC key,
internal URLs, role ports or agent-name environment were provided. Separate bootstrap
volumes were writable by harness and read-only in their respective consumer.
Volume ownership was initialized by test setup because the older image does not
precreate the new directories; final packaging must supply this without an
application initContainer.

The first event submitted through channels returned HTTP202 and completed with
`PHASE1_SPLIT_REPLY`. OpenCode recorded native session
`ses_f6b61bc60ffeIl7ov1brabLXJY` for the custom conversation.

## Harness process restart

An initial Docker harness-container restart was not a valid simulation of a
Kubernetes container restart inside a stable pod: Docker replaced the network
namespace owned by harness while channels/engine retained the previous one.
Observed namespace identities differed, and harness could no longer connect to
engine. This result is not reported as application recovery success.

Root then used a diagnostic supervisor to restart only the harness process while
keeping the shared network namespace and the other roles alive. The harness PID
changed from 7 to 23. This supervisor is test-only, not part of the shipped image.

- The generated HMAC key fingerprint stayed identical across restart.
- An event through the still-running channels completed successfully afterward.
- The native session reference remained `ses_f6b61bc60ffeIl7ov1brabLXJY`.
- All three application container restart counts remained zero.
- Engine had no ACH_TOKEN or HMAC environment variable, no full config mount and
  no channels bootstrap file; its public engine bootstrap was present.

The task-owned containers, network and seven volumes were cleaned up after the test.
Final packaged Compose validation and repository gate are recorded separately.
