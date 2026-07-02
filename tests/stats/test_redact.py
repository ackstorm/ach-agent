from ach_agent.stats.redact import redact_task


def test_redact_truncates_to_80_chars():
    long = "x" * 200
    out = redact_task(long)
    assert len(out) <= 80


def test_redact_scrubs_ek_bearer():
    out = redact_task("please use ek_live_abc123DEF456 to auth")
    assert "ek_live_abc123DEF456" not in out
    assert "ek_" not in out


def test_redact_scrubs_sk_key():
    out = redact_task("key sk-proj-ABCDEF0123456789 here")
    assert "sk-proj-ABCDEF0123456789" not in out


def test_redact_passes_clean_text():
    assert redact_task("Review merge request !7") == "Review merge request !7"


def test_redact_scrubs_before_truncating():
    # A secret past char 80 must still be gone (scrub first, then truncate).
    text = ("a" * 90) + " ek_secretVALUE1234"
    out = redact_task(text)
    assert "ek_secretVALUE1234" not in out
