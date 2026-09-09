"""Unit tests for friends/party pure helpers (no DB)."""
from datetime import datetime, timedelta, timezone

from server.routes.friends import invite_expiry, is_invite_live, is_valid_code


def test_code_validation():
    assert is_valid_code("pbs05") == "PBS05"
    assert is_valid_code(" PBS05 ") == "PBS05"
    assert is_valid_code("abcd") is None
    assert is_valid_code("abcdef") is None
    assert is_valid_code("ab!05") is None
    assert is_valid_code(None) is None
    assert is_valid_code("") is None


def test_invite_expiry_and_liveness():
    now = datetime.now(timezone.utc)
    exp = invite_expiry(now)
    exp_dt = datetime.fromisoformat(exp)
    assert timedelta(seconds=290) < (exp_dt - now) < timedelta(seconds=310)
    assert is_invite_live({"expires_at": exp}, now) is True
    past = (now - timedelta(seconds=1)).isoformat()
    assert is_invite_live({"expires_at": past}, now) is False
    assert is_invite_live({}, now) is False
    assert is_invite_live(None, now) is False
    assert is_invite_live({"expires_at": "not-a-date"}, now) is False
