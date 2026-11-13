# SPDX-License-Identifier: Apache-2.0
"""ach-runtime: Channels + Harness for ACHAgent (contract docs/schemas/ach-runtime-contract-v3.md)."""

# A constant, not importlib.metadata.version(): that scans every installed distribution on
# each start of a sandbox. `make release-bump` sets it and `make release-check` checks it.
__version__ = "0.7.6"
