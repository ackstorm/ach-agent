# SPDX-License-Identifier: Apache-2.0
import os

# Preflight (security/preflight.py) fails closed on an unsafe host. CI containers
# often run as root, which is a hard gate. Enable degraded mode for the whole suite
# so main()-invoking tests exercise wiring without sys.exit(1). Gate LOGIC is tested
# directly via evaluate_gates() in tests/security/.
os.environ.setdefault("ACH_INSECURE_ALLOW_DEGRADED", "1")

# configure_logging() runs at ach_agent.main import time and binds structlog's wrapper_class
# to the LOG_LEVEL filter (default INFO). capture_logs swaps processors but NOT wrapper_class,
# so a log.debug assertion passes in a scoped run and fails once any test has imported main.
# Pin debug for the suite so those assertions are order-independent.
os.environ.setdefault("LOG_LEVEL", "debug")
