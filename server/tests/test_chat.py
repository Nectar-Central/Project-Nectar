"""Unit tests for chat pure helpers (no DB)."""
from server.routes.chat import clean_body


def test_clean_body_ok():
    assert clean_body("  hi  ") == "hi"


def test_clean_body_rejects():
    assert clean_body(None) is None
    assert clean_body("") is None
    assert clean_body("   ") is None
    assert clean_body("x" * 501) is None
    assert clean_body("x" * 500) == "x" * 500
    assert clean_body(123) == "123"
