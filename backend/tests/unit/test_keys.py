from jbrain.auth import keys


def test_owner_key_shape() -> None:
    key = keys.generate_owner_key()
    assert key.startswith("jb1-")
    # 256 bits of base32 → 52 chars; transcription groups of 4.
    assert len(keys.normalize_key(key)) == 52


def test_keys_are_unique() -> None:
    assert keys.generate_owner_key() != keys.generate_owner_key()


def test_normalize_tolerates_transcription_noise() -> None:
    key = keys.generate_owner_key()
    mangled = " " + key.lower().replace("-", " ") + "\n"
    assert keys.normalize_key(mangled) == keys.normalize_key(key)
    assert keys.hash_key(mangled) == keys.hash_key(key)


def test_verify_roundtrip() -> None:
    key = keys.generate_owner_key()
    assert keys.verify_key(key, keys.hash_key(key))
    assert not keys.verify_key(keys.generate_owner_key(), keys.hash_key(key))


def test_session_token_hash_is_stable_and_distinct() -> None:
    token = keys.generate_session_token()
    assert keys.hash_token(token) == keys.hash_token(token)
    assert keys.hash_token(token) != keys.hash_token(keys.generate_session_token())
