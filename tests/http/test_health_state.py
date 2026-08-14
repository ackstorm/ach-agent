# SPDX-License-Identifier: Apache-2.0
"""The health/drain flags are a typed object, not a stringly-keyed dict: /readyz must
return 503 until ready, and the inbound route must 503 while draining (HTTP-02, D-12)."""

from __future__ import annotations

from ach_agent.boot.health import HealthState
from ach_agent.config.schema import ChannelConfig
from ach_agent.http.app import create_app


def test_health_state_defaults_are_not_ready_not_draining() -> None:
    state = HealthState()
    assert state.ready is False
    assert state.draining is False


def test_app_exposes_the_typed_state() -> None:
    cfg = ChannelConfig.model_validate(
        {
            "name": "gitlab-mr-review",
            "type": "webhook",
            "source": "gitlab",
            "webhook": {
                "auth": {"type": "gitlab_token", "secret": {"env": "ACH_SECRET_TEST_HEALTH"}},
            },
        }
    )

    class FakeHandler:
        async def handle(self, event: object) -> object:
            raise NotImplementedError

    app = create_app([cfg], FakeHandler())
    assert isinstance(app.extra["state"], HealthState)
