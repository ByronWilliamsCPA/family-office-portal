# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""One check for a configured service URL, shared by the settings modules.

Client libraries raise on a malformed URL with a message that can echo it,
userinfo included, so each settings module runs this check first and reports
only the variable name.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# ASCII control characters are the codes below the space and DEL.
_SPACE = 0x20
_DELETE = 0x7F


def is_http_url(url: str) -> bool:
    """Say whether a URL is http or https with a host and a usable port.

    ASCII control characters are rejected before parsing because ``urlsplit``
    silently drops tabs and newlines, so a value such as ``ht<newline>tp://host``
    would otherwise be accepted as ``http://host``.

    Args:
        url (str): The configured URL.

    Returns:
        bool: False if the value contains an ASCII control character, the
        scheme is not http or https, the host is missing, or the port is not
        a number from 0 to 65535; True otherwise.
    """
    if any(ord(char) < _SPACE or ord(char) == _DELETE for char in url):
        return False
    try:
        parts = urlsplit(url.strip())
        _ = parts.port  # raises ValueError for a port that is not 0-65535
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.hostname)
