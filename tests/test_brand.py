import json
from types import SimpleNamespace

import pytest

from app.utils import brand as brand_module
from app.utils.brand import (
    BrandError,
    brand_for_admin,
    brand_for_headers,
    brand_for_host,
    brand_for_subscription,
    clear_brand_cache,
    normalize_host,
    validate_brands,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _write(tmp_path, brands):
    path = tmp_path / "brands.json"
    path.write_text(json.dumps({"version": 1, "brands": brands}), encoding="utf-8")
    return path


@pytest.fixture
def brands_file(tmp_path, monkeypatch):
    assets = tmp_path / "brands" / "acme"
    assets.mkdir(parents=True)
    (assets / "logo.png").write_bytes(PNG)
    (assets / "secret.txt").write_text("not an asset")
    path = _write(
        tmp_path,
        {
            "acme": {
                "name": "Acme",
                "hosts": ["Panel.Acme.example", "sub.acme.example"],
                "admins": ["acme", "acme_*"],
                "logo": "logo.png",
            }
        },
    )
    monkeypatch.setattr(brand_module, "brand_settings", SimpleNamespace(path=str(path)))
    clear_brand_cache()
    yield path
    clear_brand_cache()


def test_normalize_host():
    assert normalize_host("Panel.Acme.example:8443") == "panel.acme.example"
    assert normalize_host("a.example, b.example") == "a.example"
    assert normalize_host("[2001:db8::1]:443") == "[2001:db8::1]"
    assert normalize_host(None) == ""


def test_no_file_means_no_brand(tmp_path, monkeypatch):
    monkeypatch.setattr(brand_module, "brand_settings", SimpleNamespace(path=str(tmp_path / "missing.json")))
    clear_brand_cache()
    assert brand_for_host("panel.acme.example") is None
    assert brand_for_admin("acme") is None


def test_brand_by_host_and_admin(brands_file):
    assert brand_for_host("panel.acme.example:443").name == "Acme"
    assert brand_for_host("panel.hexogate.example") is None
    assert brand_for_admin("acme").key == "acme"
    assert brand_for_admin("acme_seller2").key == "acme"
    assert brand_for_admin("WedBaZ") is None
    assert brand_for_headers({"host": "other.example", "x-forwarded-host": "sub.acme.example"}).key == "acme"
    assert brand_for_headers({"host": "other.example"}) is None


def test_subscription_brand_follows_admin_then_host(brands_file):
    assert brand_for_subscription("acme_seller2", "https://sub.hexogate.example/sub/t").key == "acme"
    assert brand_for_subscription("someone", "https://sub.acme.example/sub/t").key == "acme"
    assert brand_for_subscription("someone", "https://sub.hexogate.example/sub/t") is None
    assert brand_for_subscription(None, "") is None


def test_assets_are_limited_to_configured_files(brands_file):
    brand = brand_for_host("panel.acme.example")
    assert brand.asset_path("logo.png").is_file()
    assert brand.asset_path("secret.txt") is None
    assert brand.asset_path("../brands.json") is None
    public = brand.public()
    assert public["logo"].startswith("/api/brand/asset/logo.png?v=")
    assert public["logoOnLight"] == public["logo"]
    assert public["favicon"] is None
    assert brand.template_context()["logo"].startswith("data:image/png;base64,")


def test_edit_is_picked_up_and_bad_edit_keeps_last_good(brands_file):
    assert brand_for_host("new.acme.example") is None
    data = json.loads(brands_file.read_text())
    data["brands"]["acme"]["hosts"].append("new.acme.example")
    brands_file.write_text(json.dumps(data))
    assert brand_for_host("new.acme.example").key == "acme"

    brands_file.write_text("{ not json at all")
    assert brand_for_host("new.acme.example").key == "acme"


@pytest.mark.parametrize(
    "brands",
    [
        {"Bad Key": {"name": "x"}},
        {"a": {"name": ""}},
        {"a": {"name": "A", "hosts": "panel.a.example"}},
        {"a": {"name": "A", "logo": "../../etc/passwd"}},
        {"a": {"name": "A", "hosts": ["x.example"]}, "b": {"name": "B", "hosts": ["X.example"]}},
    ],
)
def test_validation_rejects(tmp_path, brands):
    with pytest.raises(BrandError):
        validate_brands({"version": 1, "brands": brands}, tmp_path)


def test_dashboard_shell_gets_the_brand(brands_file):
    from dashboard import apply_brand

    html = (
        '<html><head><link rel="icon" type="image/svg+xml" href="/statics/favicon/favicon.ico" />'
        '<link rel="apple-touch-icon" href="/statics/favicon/logo-pwa.png" />'
        '<link rel="manifest" href="/statics/favicon/site.webmanifest" />'
        "<title>Hexogate</title></head><body></body></html>"
    )
    out = apply_brand(html, brand_for_host("panel.acme.example"))
    assert "<title>Acme</title>" in out
    assert "Hexogate" not in out and "manifest" not in out and "apple-touch-icon" not in out
    assert 'rel="icon" href="/api/brand/asset/logo.png?v=' in out
    assert 'window.__HEXO_BRAND__={"key": "acme", "name": "Acme"' in out


def test_theme_is_validated_and_published(tmp_path):
    theme = {"default_mode": "dark", "theme_color": "#0a0a0a", "dark": {"--primary": "355 86% 47%"}, "light": {}}
    brand = validate_brands({"version": 1, "brands": {"a": {"name": "A", "theme": theme}}}, tmp_path)["a"]
    assert brand.public()["theme"] == {
        "defaultMode": "dark",
        "themeColor": "#0a0a0a",
        "dark": {"--primary": "355 86% 47%"},
        "light": {},
    }
    assert validate_brands({"version": 1, "brands": {"a": {"name": "A"}}}, tmp_path)["a"].public()["theme"] is None


@pytest.mark.parametrize(
    "theme",
    [
        {"default_mode": "sepia"},
        {"theme_color": "red"},
        {"dark": {"primary": "355 86% 47%"}},
        {"dark": {"--primary": "red;} body{display:none"}},
        {"dark": {"--primary": "url(javascript:alert(1))<"}},
        {"surprise": 1},
    ],
)
def test_theme_validation_rejects(tmp_path, theme):
    with pytest.raises(BrandError):
        validate_brands({"version": 1, "brands": {"a": {"name": "A", "theme": theme}}}, tmp_path)


def test_dashboard_shell_gets_the_theme_color(tmp_path):
    from dashboard import apply_brand

    theme = {"theme_color": "#0a0a0a", "dark": {"--background": "0 0% 4%"}}
    brand = validate_brands({"version": 1, "brands": {"a": {"name": "A", "theme": theme}}}, tmp_path)["a"]
    html = '<html><head><meta name="theme-color" content="#031d44" /><title>Hexogate</title></head></html>'
    out = apply_brand(html, brand)
    assert '<meta name="theme-color" content="#0a0a0a" />' in out and "#031d44" not in out
    assert '"--background": "0 0% 4%"' in out
