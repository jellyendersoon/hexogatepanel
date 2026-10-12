"""White-label brands for tenants that live on this panel.

A brand gives one tenant (a reseller with their own admins) their own name and
logo on the dashboard, the subscription page and the home page. It is chosen by
the request host first (the tenant's own panel/subscription domain) and, for a
subscription, by the admin that owns the user.

The brands file is runtime data and never part of the source tree. Its path
comes from ``BRANDS_PATH`` (or ``PASARGUARD_BRANDS_PATH``); logo files live next
to it in ``brands/<key>/``. The file is re-read when it changes, so adding or
editing a brand needs no panel restart. Every error is availability-safe: the
default (Hexogate) presentation is used.

    {
      "version": 1,
      "brands": {
        "acme": {
          "name": "Acme",
          "hosts": ["panel.acme.example", "sub.acme.example"],
          "admins": ["acme", "acme_*"],
          "logo": "logo.png",
          "logo_on_light": "logo-dark.png",
          "favicon": "favicon.ico"
        }
      }
    }

``logo`` is the mark for dark surfaces, ``logo_on_light`` the one for light
surfaces (defaults to ``logo``). ``admins`` entries are usernames or fnmatch
patterns.

An optional ``theme`` recolours the dashboard on the brand's hosts::

    "theme": {
      "default_mode": "dark",
      "theme_color": "#0a0a0a",
      "dark":  {"--primary": "355 86% 47%", "--background": "0 0% 4%"},
      "light": {"--primary": "155 57% 16%"}
    }

``dark`` / ``light`` are CSS custom properties laid over the default palette
(same ``H S% L%`` values the dashboard themes use).
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from config import brand_settings

LOGGER = logging.getLogger("hexogate.brand")

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ASSET_FIELDS = ("logo", "logo_on_light", "favicon")
_CSS_VAR_RE = re.compile(r"^--[a-z0-9-]{1,40}$")
_CSS_VALUE_RE = re.compile(r"^[0-9a-zA-Z.%, ()#-]{1,60}$")
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_MAX_ASSET_BYTES = 1024 * 1024


class BrandError(ValueError):
    pass


@dataclass(frozen=True)
class Brand:
    key: str
    name: str
    directory: Path
    hosts: frozenset[str] = frozenset()
    admins: tuple[str, ...] = ()
    logo: str | None = None
    logo_on_light: str | None = None
    favicon: str | None = None
    theme: dict[str, Any] | None = field(default=None, compare=False)
    _data_uris: dict[str, str] = field(default_factory=dict, compare=False, repr=False)

    @property
    def files(self) -> frozenset[str]:
        return frozenset(name for name in (self.logo, self.logo_on_light, self.favicon) if name)

    def asset_path(self, file_name: str) -> Path | None:
        """Path of a configured asset, or None for anything not named in the brand."""
        if file_name not in self.files:
            return None
        path = self.directory / file_name
        return path if path.is_file() else None

    def asset_url(self, file_name: str | None) -> str | None:
        if not file_name:
            return None
        path = self.asset_path(file_name)
        if path is None:
            return None
        return f"/api/brand/asset/{file_name}?v={int(path.stat().st_mtime)}"

    def data_uri(self, file_name: str | None) -> str | None:
        """Inline form of an asset, for pages that must not depend on a second request."""
        if not file_name:
            return None
        cached = self._data_uris.get(file_name)
        if cached:
            return cached
        path = self.asset_path(file_name)
        if path is None or path.stat().st_size > _MAX_ASSET_BYTES:
            return None
        media_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        uri = f"data:{media_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        self._data_uris[file_name] = uri
        return uri

    def public(self) -> dict[str, Any]:
        """What the dashboard gets (also injected into its index.html)."""
        logo = self.asset_url(self.logo)
        return {
            "key": self.key,
            "name": self.name,
            "logo": logo,
            "logoOnLight": self.asset_url(self.logo_on_light) or logo,
            "favicon": self.asset_url(self.favicon),
            "theme": self.theme,
        }

    def template_context(self) -> dict[str, Any]:
        """What the HTML templates get."""
        logo = self.data_uri(self.logo)
        return {
            "key": self.key,
            "name": self.name,
            "logo": logo,
            "logo_on_light": self.data_uri(self.logo_on_light) or logo,
            "favicon": self.data_uri(self.favicon),
        }


def normalize_host(value: str | None) -> str:
    """Lower-case host without port (``Panel.Acme.example:443`` -> ``panel.acme.example``)."""
    host = (value or "").split(",")[0].strip().lower()
    if host.startswith("["):
        return host.partition("]")[0] + "]" if "]" in host else host
    return host.rpartition(":")[0] if ":" in host else host


def _validate_theme(key: str, raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"default_mode", "theme_color", "dark", "light"}:
        raise BrandError(f"brand {key}: theme accepts default_mode, theme_color, dark, light")
    default_mode = raw.get("default_mode")
    if default_mode not in (None, "dark", "light"):
        raise BrandError(f"brand {key}: theme.default_mode must be dark or light")
    theme_color = raw.get("theme_color")
    if theme_color is not None and not (isinstance(theme_color, str) and _HEX_COLOR_RE.match(theme_color)):
        raise BrandError(f"brand {key}: theme.theme_color must look like #rrggbb")
    theme: dict[str, Any] = {"defaultMode": default_mode, "themeColor": theme_color}
    for mode in ("dark", "light"):
        variables = raw.get(mode, {})
        if not isinstance(variables, dict):
            raise BrandError(f"brand {key}: theme.{mode} must be an object")
        for name, value in variables.items():
            if not (isinstance(name, str) and _CSS_VAR_RE.match(name)):
                raise BrandError(f"brand {key}: theme.{mode} has a bad variable name {name!r}")
            if not (isinstance(value, str) and _CSS_VALUE_RE.match(value)):
                raise BrandError(f"brand {key}: theme.{mode} {name} has a bad value")
        theme[mode] = dict(variables)
    return theme


def validate_brands(data: Any, base_dir: Path) -> dict[str, Brand]:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise BrandError("brands version must be 1")
    raw_brands = data.get("brands")
    if not isinstance(raw_brands, dict):
        raise BrandError("brands must be an object")

    brands: dict[str, Brand] = {}
    seen_hosts: dict[str, str] = {}
    for key, raw in raw_brands.items():
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise BrandError(f"brand key {key!r} must match {_KEY_RE.pattern}")
        if not isinstance(raw, dict):
            raise BrandError(f"brand {key} must be an object")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 64:
            raise BrandError(f"brand {key}: name must be 1-64 characters")

        hosts = raw.get("hosts", [])
        admins = raw.get("admins", [])
        for label, values in (("hosts", hosts), ("admins", admins)):
            if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
                raise BrandError(f"brand {key}: {label} must be a list of non-empty strings")
        normalized_hosts = frozenset(normalize_host(host) for host in hosts)
        for host in normalized_hosts:
            if host in seen_hosts:
                raise BrandError(f"host {host} is used by both {seen_hosts[host]} and {key}")
            seen_hosts[host] = key

        assets: dict[str, str | None] = {}
        for asset_field in _ASSET_FIELDS:
            file_name = raw.get(asset_field)
            if file_name is not None and (not isinstance(file_name, str) or not _FILE_RE.match(file_name)):
                raise BrandError(f"brand {key}: {asset_field} must be a plain file name")
            assets[asset_field] = file_name

        brands[key] = Brand(
            key=key,
            name=name.strip(),
            directory=base_dir / "brands" / key,
            hosts=normalized_hosts,
            admins=tuple(admin.strip() for admin in admins),
            theme=_validate_theme(key, raw.get("theme")),
            **assets,
        )
    return brands


def validate_brands_file(path: str | Path) -> dict[str, Brand]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        return validate_brands(json.load(handle), path.parent)


_cache_stamp: tuple[str, int, int] | None = None
_cache_brands: dict[str, Brand] = {}


def _brands() -> dict[str, Brand]:
    """Current brands; re-read when the file changes, last good set kept on a bad edit."""
    global _cache_stamp, _cache_brands
    path = Path(brand_settings.path)
    try:
        stat = path.stat()
    except OSError:
        _cache_stamp, _cache_brands = None, {}
        return _cache_brands

    stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    if stamp != _cache_stamp:
        _cache_stamp = stamp
        try:
            _cache_brands = validate_brands_file(path)
            LOGGER.info("brands loaded: %s", ", ".join(sorted(_cache_brands)) or "none")
        except (OSError, ValueError) as exc:
            LOGGER.error("brands file %s rejected, keeping the previous set: %s", path, exc)
    return _cache_brands


def clear_brand_cache() -> None:
    global _cache_stamp, _cache_brands
    _cache_stamp, _cache_brands = None, {}


def brand_for_host(host: str | None) -> Brand | None:
    host = normalize_host(host)
    if not host:
        return None
    for brand in _brands().values():
        if host in brand.hosts:
            return brand
    return None


def brand_for_admin(username: str | None) -> Brand | None:
    if not username:
        return None
    for brand in _brands().values():
        if any(fnmatchcase(username, pattern) for pattern in brand.admins):
            return brand
    return None


def brand_for_headers(headers: Any) -> Brand | None:
    """Brand of a request: the forwarded host when a front proxy set one, else Host."""
    return brand_for_host(headers.get("x-forwarded-host")) or brand_for_host(headers.get("host"))


def brand_for_url(url: str | None) -> Brand | None:
    try:
        return brand_for_host(urlsplit(url or "").hostname)
    except ValueError:
        return None


def brand_for_subscription(admin_username: str | None, request_url: str | None) -> Brand | None:
    """A user's brand follows their admin; the request host covers admins not listed."""
    return brand_for_admin(admin_username) or brand_for_url(request_url)
