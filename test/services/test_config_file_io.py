import errno
from pathlib import Path

import pytest
import toml

from app.config import config


@pytest.mark.parametrize("bom_count", [0, 1, 2, 3])
def test_load_config_accepts_repeated_bom_without_rewriting(tmp_path, monkeypatch, bom_count):
    config_path = tmp_path / "config.toml"
    contents = ("\ufeff" * bom_count + '[app]\nlabel = "Türkçe"\n').encode("utf-8")
    config_path.write_bytes(contents)
    monkeypatch.setattr(config, "config_file", str(config_path))

    loaded = config.load_config()

    assert loaded["app"]["label"] == "Türkçe"
    assert config_path.read_bytes() == contents


def test_load_config_does_not_log_invalid_config_contents(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    contents = b"\xef\xbb\xbf\xef\xbb\xbf[app]\napi_key = dummy-private-marker\n"
    config_path.write_bytes(contents)
    monkeypatch.setattr(config, "config_file", str(config_path))
    messages = []
    handler_id = config.logger.add(messages.append, format="{message}")
    try:
        with pytest.raises(toml.TomlDecodeError):
            config.load_config()
    finally:
        config.logger.remove(handler_id)

    assert "dummy-private-marker" not in "".join(messages)
    assert config_path.read_bytes() == contents


def test_load_config_does_not_retry_filesystem_errors(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[app]\nlabel = "unchanged"\n', encoding="utf-8")
    monkeypatch.setattr(config, "config_file", str(config_path))

    def deny_read(_path):
        raise PermissionError(errno.EACCES, "configuration access denied")

    monkeypatch.setattr(config.toml, "load", deny_read)
    with pytest.raises(PermissionError, match="configuration access denied"):
        config.load_config()


@pytest.fixture
def temporary_config(tmp_path, monkeypatch):
    initial = {
        "app": {"label": "old"},
        "whisper": {"language": "tr"},
        "custom": {"enabled": True},
    }
    config_path = tmp_path / "config.toml"
    config_path.write_text(toml.dumps(initial), encoding="utf-8")
    monkeypatch.setattr(config, "root_dir", str(tmp_path))
    monkeypatch.setattr(config, "config_file", str(config_path))
    monkeypatch.setattr(config, "_cfg", initial)
    monkeypatch.setattr(config, "app", {"label": "new"})
    for section in ("azure", "siliconflow", "chatterbox", "ui"):
        monkeypatch.setattr(config, section, {})
    monkeypatch.setattr(config, "elevenlabs", {"api_key": "dummy-test-key"})
    return config_path


def test_save_config_updates_bind_mount_without_losing_sections(temporary_config, monkeypatch):
    original_inode = temporary_config.stat().st_ino

    def mounted_file(_source, _target):
        raise OSError(errno.EBUSY, "mounted file cannot be replaced")

    monkeypatch.setattr(config.os, "replace", mounted_file)

    config.save_config()

    loaded = toml.loads(temporary_config.read_text(encoding="utf-8"))
    assert loaded["app"]["label"] == "new"
    assert loaded["whisper"]["language"] == "tr"
    assert loaded["custom"]["enabled"] is True
    assert loaded["elevenlabs"]["api_key"] == "dummy-test-key"
    assert config._cfg == loaded
    assert temporary_config.stat().st_ino == original_inode
    assert list(temporary_config.parent.glob(".config-*.toml.tmp")) == []

    def unexpected_write(**_kwargs):
        raise AssertionError("unchanged settings should not require a file write")

    monkeypatch.setattr(config.tempfile, "mkstemp", unexpected_write)
    config.save_config()


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.ENOSPC, errno.EXDEV])
def test_save_config_preserves_existing_file_on_other_replace_errors(
    temporary_config, monkeypatch, error_number
):
    original_bytes = temporary_config.read_bytes()
    original_config = dict(config._cfg)

    def fail_replace(_source, _target):
        raise OSError(error_number, "cannot replace configuration")

    monkeypatch.setattr(config.os, "replace", fail_replace)

    with pytest.raises(OSError) as error:
        config.save_config()

    assert error.value.errno == error_number
    assert temporary_config.read_bytes() == original_bytes
    assert config._cfg == original_config
    assert list(temporary_config.parent.glob(".config-*.toml.tmp")) == []


def test_failed_bind_mount_write_does_not_update_runtime_snapshot(temporary_config, monkeypatch):
    original_config = dict(config._cfg)
    original_bytes = temporary_config.read_bytes()
    real_open = open

    def mounted_file(_source, _target):
        raise OSError(errno.EBUSY, "mounted file cannot be replaced")

    def deny_write(path, *args, **kwargs):
        if Path(path) == temporary_config and kwargs.get("mode") == "w":
            raise PermissionError(errno.EACCES, "mounted file is not writable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(config.os, "replace", mounted_file)
    monkeypatch.setattr("builtins.open", deny_write)

    with pytest.raises(PermissionError, match="mounted file is not writable"):
        config.save_config()

    assert config._cfg == original_config
    assert temporary_config.read_bytes() == original_bytes
    assert list(temporary_config.parent.glob(".config-*.toml.tmp")) == []
