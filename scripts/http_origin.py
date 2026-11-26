"""Validate an explicitly configured HTTPS origin without contacting it."""
import re
from urllib.parse import urlsplit


HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
HTTPS_ORIGIN = re.compile(
    rf"https://(?:{HOST_LABEL}(?:\.{HOST_LABEL})*\.?|\[[0-9a-f:.]+\])(?::[0-9]+)?",
    re.IGNORECASE | re.ASCII,
)
ERROR = "expected an HTTPS origin with a valid port and no credentials, path, query or fragment"


def parse_https_origin(value):
    if not isinstance(value, str):
        raise ValueError(ERROR)
    origin = value.removesuffix("/")
    if not HTTPS_ORIGIN.fullmatch(origin):
        raise ValueError(ERROR)
    try:
        port = urlsplit(origin).port
    except ValueError:
        raise ValueError(ERROR) from None
    if port == 0:
        raise ValueError(ERROR)
    return origin
