from ach_agent.router.slots import SlotManager


def test_per_channel_semaphores_sized_from_map():
    sm = SlotManager(max_concurrent_invocations=3, channel_concurrency={"a": 1, "b": 2})
    assert sm.channel_sem("a")._value == 1
    assert sm.channel_sem("b")._value == 2


def test_channel_sem_is_stable_per_name():
    sm = SlotManager(max_concurrent_invocations=3, channel_concurrency={"a": 1})
    assert sm.channel_sem("a") is sm.channel_sem("a")


def test_unknown_channel_defaults_to_global_size():
    sm = SlotManager(max_concurrent_invocations=3, channel_concurrency={"a": 1})
    z = sm.channel_sem("zzz")
    assert z._value == 3  # no tighter-than-global cap for unknown channels
    assert sm.channel_sem("zzz") is z  # cached, stable
