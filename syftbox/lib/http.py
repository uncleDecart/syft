import platform

from syftbox import __version__

HEADER_SYFTBOX_VERSION = "x-syftbox-version"
HEADER_SYFTBOX_PYTHON = "x-syftbox-python"
HEADER_SYFTBOX_USER = "x-syftbox-user"
HEADER_OS_NAME = "x-os-name"
HEADER_OS_VERSION = "x-os-ver"
HEADER_OS_ARCH = "x-os-arch"

_PYTHON_VERSION = platform.python_version()

SYFTBOX_HEADERS = {
    HEADER_SYFTBOX_VERSION: str(__version__),
    HEADER_SYFTBOX_PYTHON: _PYTHON_VERSION,
    # todo - os
}
