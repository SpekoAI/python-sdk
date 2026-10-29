import importlib.metadata
import runpy

import spekoai._user_agent as user_agent_module
import spekoai.client as client_module
import spekoai.realtime as realtime_module


def test_rest_and_realtime_share_installed_package_identity():
    expected = f"spekoai-python/{importlib.metadata.version('spekoai')}"
    assert user_agent_module.USER_AGENT == expected
    assert client_module.USER_AGENT == expected
    assert realtime_module.USER_AGENT == expected
    assert client_module._default_headers("sk-test")["User-Agent"] == expected


def test_package_identity_uses_distribution_metadata(monkeypatch):
    def installed_version(distribution):
        assert distribution == "spekoai"
        return "1.2.3+test.7"

    monkeypatch.setattr(importlib.metadata, "version", installed_version)
    identity = runpy.run_path(user_agent_module.__file__)
    assert identity["USER_AGENT"] == "spekoai-python/1.2.3+test.7"


def test_package_identity_marks_missing_distribution_metadata(monkeypatch):
    def missing_version(distribution):
        raise importlib.metadata.PackageNotFoundError(distribution)

    monkeypatch.setattr(importlib.metadata, "version", missing_version)
    identity = runpy.run_path(user_agent_module.__file__)
    assert identity["USER_AGENT"] == "spekoai-python/0.0.0+unknown"
