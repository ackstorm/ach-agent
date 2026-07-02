# SPDX-License-Identifier: Apache-2.0
import os

# Preflight (security/preflight.py) fails closed on an unsafe host. CI containers
# often run as root, which is a hard gate. Enable degraded mode for the whole suite
# so main()-invoking tests exercise wiring without sys.exit(1). Gate LOGIC is tested
# directly via evaluate_gates() in tests/security/.
os.environ.setdefault("ACH_INSECURE_ALLOW_DEGRADED", "1")
