"""Hashing, canonical JSON and secret encryption."""

from __future__ import annotations

import base64
import hashlib
import json
import random
from typing import Any

import pytest
from cryptography.fernet import Fernet
from hypothesis import given
from hypothesis import strategies as st

from app.core.errors import ConfigurationError
from app.core.security import (
    SecretBox,
    canonical_json,
    compute_payload_hash,
    constant_time_equals,
    generate_fernet_key,
    generate_token,
    hash_token,
    sha256_hex,
)
from tests.conftest import TEST_FERNET_KEY, make_settings

json_scalars = st.none() | st.booleans() | st.integers() | st.text(max_size=20)
json_values = st.recursive(
    json_scalars,
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4),
    max_leaves=12,
)
payloads = st.dictionaries(st.text(min_size=1, max_size=10), json_values, min_size=1, max_size=6)


def _reorder(value: Any, rnd: random.Random) -> Any:
    if isinstance(value, dict):
        items = list(value.items())
        rnd.shuffle(items)
        return {k: _reorder(v, rnd) for k, v in items}
    if isinstance(value, list):
        return [_reorder(v, rnd) for v in value]
    return value


@given(payloads, st.randoms())
def test_canonical_json_is_key_order_independent(payload: dict[str, Any], rnd: random.Random) -> None:
    assert canonical_json(payload) == canonical_json(_reorder(payload, rnd))
    assert json.loads(canonical_json(payload)) == payload


def test_canonical_json_format() -> None:
    assert canonical_json({"b": 1, "a": [1, 2], "é": "ü"}) == '{"a":[1,2],"b":1,"é":"ü"}'
    # Non-JSON values are stringified rather than failing.
    assert canonical_json({"x": b"x"}) == """{"x":"b'x'"}"""


@given(payloads, st.randoms())
def test_payload_hash_order_independent(payload: dict[str, Any], rnd: random.Random) -> None:
    assert compute_payload_hash("t", "x", payload) == compute_payload_hash("t", "x", _reorder(payload, rnd))


@given(payloads, st.data())
def test_payload_hash_changes_on_any_field_change(payload: dict[str, Any], data: st.DataObject) -> None:
    original = compute_payload_hash("type", "target", payload)
    key = data.draw(st.sampled_from(sorted(payload)))
    new_value = data.draw(json_values.filter(lambda v: v != payload[key] or type(v) is not type(payload[key])))
    changed = {**payload, key: new_value}
    if canonical_json(changed) != canonical_json(payload):
        assert compute_payload_hash("type", "target", changed) != original
    added = {**payload, f"{key}-extra": 1}
    assert compute_payload_hash("type", "target", added) != original
    removed = {k: v for k, v in payload.items() if k != key}
    assert compute_payload_hash("type", "target", removed) != original


def test_payload_hash_binds_action_type_and_target() -> None:
    payload = {"a": 1}
    base = compute_payload_hash("create_azure_devops_work_item", "azure_devops", payload)
    assert base != compute_payload_hash("create_follow_up_task", "azure_devops", payload)
    assert base != compute_payload_hash("create_azure_devops_work_item", "internal_tasks", payload)
    assert len(base) == 64
    expected = hashlib.sha256(
        canonical_json(
            {"action_type": "create_azure_devops_work_item", "target": "azure_devops", "payload": payload}
        ).encode()
    ).hexdigest()
    assert base == expected


def test_hash_token_and_sha256_hex() -> None:
    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()
    assert sha256_hex("abc") == hash_token("abc")
    assert hash_token("abc") != hash_token("abd")


def test_generate_token_is_random_and_urlsafe() -> None:
    tokens = {generate_token() for _ in range(20)}
    assert len(tokens) == 20
    for token in tokens:
        assert len(token) >= 40
        assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert len(generate_token(8)) < len(generate_token(64))


@given(st.text(max_size=40), st.text(max_size=40))
def test_constant_time_equals_matches_equality(a: str, b: str) -> None:
    assert constant_time_equals(a, b) is (a == b)
    assert constant_time_equals(a, a) is True


# --------------------------------------------------------------------------- SecretBox


@given(st.text(max_size=200))
def test_secret_box_roundtrip(plaintext: str) -> None:
    box = SecretBox([TEST_FERNET_KEY])
    token = box.encrypt_str(plaintext)
    assert isinstance(token, bytes)
    assert box.decrypt_str(token) == plaintext
    assert box.decrypt(box.encrypt(plaintext.encode())) == plaintext.encode()


def test_secret_box_ciphertext_is_not_plaintext_and_is_randomized() -> None:
    box = SecretBox([TEST_FERNET_KEY])
    a, b = box.encrypt_str("refresh-token"), box.encrypt_str("refresh-token")
    assert a != b
    assert b"refresh-token" not in a


def test_secret_box_rotation_encrypts_with_first_key() -> None:
    old, new = TEST_FERNET_KEY, Fernet.generate_key().decode()
    legacy = SecretBox([old]).encrypt_str("secret")
    rotated = SecretBox([new, old])
    assert rotated.decrypt_str(legacy) == "secret"
    fresh = rotated.encrypt_str("secret")
    assert Fernet(new.encode()).decrypt(fresh) == b"secret"
    with pytest.raises(ConfigurationError):
        SecretBox([old]).decrypt_str(fresh)


def test_secret_box_wrong_key_raises_configuration_error() -> None:
    token = SecretBox([TEST_FERNET_KEY]).encrypt_str("x")
    with pytest.raises(ConfigurationError, match="cannot be decrypted"):
        SecretBox([Fernet.generate_key().decode()]).decrypt(token)
    with pytest.raises(ConfigurationError):
        SecretBox([TEST_FERNET_KEY]).decrypt(b"garbage")


@pytest.mark.parametrize("bad", ["not-a-key", base64.urlsafe_b64encode(b"short").decode(), "é"])
def test_secret_box_invalid_key(bad: str) -> None:
    with pytest.raises(ConfigurationError, match="invalid Fernet key"):
        SecretBox([bad])


def test_secret_box_requires_keys() -> None:
    with pytest.raises(ConfigurationError, match="not configured"):
        SecretBox([])


def test_secret_box_from_settings() -> None:
    other = Fernet.generate_key().decode()
    settings = make_settings("sqlite+aiosqlite:///x.db", token_encryption_keys=f" {other} , ,{TEST_FERNET_KEY}")
    box = SecretBox.from_settings(settings)
    legacy = SecretBox([TEST_FERNET_KEY]).encrypt_str("v")
    assert box.decrypt_str(legacy) == "v"
    assert Fernet(other.encode()).decrypt(box.encrypt_str("v")) == b"v"


def test_secret_box_from_settings_without_keys() -> None:
    settings = make_settings("sqlite+aiosqlite:///x.db", token_encryption_keys=None)
    with pytest.raises(ConfigurationError):
        SecretBox.from_settings(settings)


def test_secret_box_from_default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = make_settings("sqlite+aiosqlite:///x.db")
    monkeypatch.setattr("app.core.security.get_settings", lambda: sentinel)
    assert SecretBox.from_settings().decrypt_str(SecretBox([TEST_FERNET_KEY]).encrypt_str("ok")) == "ok"


def test_generate_fernet_key_is_valid() -> None:
    key = generate_fernet_key()
    assert isinstance(key, str)
    assert SecretBox([key]).decrypt_str(SecretBox([key]).encrypt_str("k")) == "k"
    assert generate_fernet_key() != key
