# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from ach_agent.engine.base.pool import EnginePool, _NamespacedSessionMap


def test_namespaced_map_prefixes_underlying_store() -> None:
    store: dict[str, str] = {}
    ns = _NamespacedSessionMap(store, "opencode")
    ns["k1"] = "ses_1"
    assert ns.get("k1") == "ses_1"        # transparent to the caller (bare key)
    assert store == {"opencode:k1": "ses_1"}  # prefixed in the underlying store
    assert ns.pop("k1") == "ses_1"
    assert store == {}


def test_pool_sessions_is_namespaced_by_driver_engine_type() -> None:
    store: dict[str, str] = {}

    class _Piish:
        engine_type = "pi"

    pool = EnginePool(driver=_Piish(), sessions_map=store)
    pool.sessions["c"] = "/sessions/abc.json"
    assert store == {"pi:c": "/sessions/abc.json"}
    assert pool.oc_sessions is pool.sessions   # back-compat alias


def test_pool_defaults_to_opencode_driver() -> None:
    pool = EnginePool()
    assert pool._driver.engine_type == "opencode"
