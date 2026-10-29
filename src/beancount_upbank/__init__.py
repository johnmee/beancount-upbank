"""Import Up Bank transactions into Beancount."""

from importlib.metadata import PackageNotFoundError, version

from .client import UpbankClient, UpbankError
from .importer import UpbankImporter

try:
    __version__ = version("beancount-upbank")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"

__all__ = ["UpbankClient", "UpbankError", "UpbankImporter", "__version__"]
