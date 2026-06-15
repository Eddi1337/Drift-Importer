import os


def test_encrypt_roundtrip(tmp_path, monkeypatch):
    # Point the data dir at a temp location so we don't touch real keys.
    monkeypatch.setenv("DRIFT_DATA_DIR", str(tmp_path))
    # Reset cached settings/fernet that may have been created by earlier imports.
    from app import config, crypto

    config.get_settings.cache_clear()
    crypto._fernet.cache_clear()

    secret = "super-secret-password-123"
    token = crypto.encrypt(secret)
    assert token != secret
    assert crypto.decrypt(token) == secret
    assert crypto.decrypt("") == ""
    # key file written with restrictive perms
    keyfile = tmp_path / "secret.key"
    assert keyfile.exists()
    assert oct(os.stat(keyfile).st_mode)[-3:] == "600"
