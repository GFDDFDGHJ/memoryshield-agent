"""Operator-controlled connection settings and memory namespace."""
from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import ip_address
import math
import os
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    base_url: str
    user_id: str
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 240

    def __post_init__(self) -> None:
        try:
            parts = urlsplit(self.base_url)
            parts.port
        except ValueError:
            raise ValueError("MEMORYSHIELD_URL must be a valid HTTP(S) base URL") from None
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment):
            raise ValueError("MEMORYSHIELD_URL must have a host and no credentials, query, or fragment")
        try:
            local = ip_address(parts.hostname).is_loopback
        except ValueError:
            local = parts.hostname.lower() == "localhost"
        if not local and (parts.scheme != "https" or not self.api_key):
            raise ValueError("Remote backends require HTTPS and MEMORYSHIELD_API_KEY")
        if not self.user_id.strip() or len(self.user_id) > 200:
            raise ValueError("Set MEMORYSHIELD_USER_ID to a nonempty namespace of at most 200 characters")
        if not math.isfinite(self.timeout_seconds) or not 1 <= self.timeout_seconds <= 1800:
            raise ValueError("MEMORYSHIELD_TIMEOUT_SECONDS must be between 1 and 1800")
        if any(c in self.api_key for c in "\r\n"):
            raise ValueError("MEMORYSHIELD_API_KEY must be a single line")

    @classmethod
    def from_env(cls) -> Settings:
        token = os.environ.get("MEMORYSHIELD_API_KEY", "").strip()
        token_file = os.environ.get("MEMORYSHIELD_API_KEY_FILE", "")
        if not token and token_file:
            try:
                token = Path(token_file).expanduser().read_text(encoding="utf-8-sig").strip()
            except OSError:
                raise ValueError("Cannot read MEMORYSHIELD_API_KEY_FILE") from None
        try:
            timeout = float(os.environ.get("MEMORYSHIELD_TIMEOUT_SECONDS", "240"))
        except ValueError:
            raise ValueError("MEMORYSHIELD_TIMEOUT_SECONDS must be a number") from None
        return cls(
            base_url=os.environ.get("MEMORYSHIELD_URL", "http://127.0.0.1:18080").rstrip("/"),
            user_id=os.environ.get("MEMORYSHIELD_USER_ID", ""),
            api_key=token,
            timeout_seconds=timeout,
        )
