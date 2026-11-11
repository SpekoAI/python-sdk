"""Package identity for HTTP requests to Speko services."""

from importlib.metadata import PackageNotFoundError, version

try:
    _PKG_VERSION = version("spekoai")
except PackageNotFoundError:
    _PKG_VERSION = "0.0.0+unknown"

USER_AGENT = f"spekoai-python/{_PKG_VERSION}"
