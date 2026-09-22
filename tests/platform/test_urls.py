import pytest

from server.adapters.platforms import normalize_url, source_locator
from server.errors import IngestError

ID = "dQw4w9WgXcQ"
BV = "BV1eZdzBGEvE"


@pytest.mark.parametrize("url", [
    f"https://youtube.com/watch?v={ID}",
    f"https://www.youtube.com/watch?v={ID}&list=PL123&index=3&si=tracking",
    f"http://m.youtube.com/watch?v={ID}",
    f"https://www.youtube.com/shorts/{ID}",
    f"https://www.youtube.com/embed/{ID}",
    f"https://youtu.be/{ID}?si=tracking",
])
def test_youtube_variants_have_one_identity(url):
    source = normalize_url(url)
    assert source.identity == f"youtube:{ID}:1"
    assert source.canonical_url == f"https://www.youtube.com/watch?v={ID}"


@pytest.mark.parametrize("url, expected_ms", [
    (f"https://youtu.be/{ID}?t=1h2m3s", 3723000),
    (f"https://youtube.com/watch?v={ID}&start=72", 72000),
    (f"https://youtube.com/embed/{ID}#t=12.5", 12500),
])
def test_playback_focus_does_not_change_cache_identity(url, expected_ms):
    source = normalize_url(url)
    assert source.focus_timestamp_ms == expected_ms
    assert source.identity == normalize_url(f"https://youtu.be/{ID}").identity
    assert "t=" not in source.canonical_url


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/playlist?list=PL123",
    "https://www.youtube.com/watch?list=PL123",
    f"https://www.youtube.com/watch?v={ID}&v=abcdefghijk",
    f"https://www.youtube.com/watch?v={ID}&v={ID}",
    f"https://www.youtube.com/embed/{ID}?v=abcdefghijk",
    f"https://www.youtube.com/watch?v={ID}&t=1&start=2",
    "https://www.youtube.com/watch?v=notvalid",
    f"https://www.youtube.com/live/{ID}",
    f"https://youtu.be//{ID}",
    f"https://www.bilibili.com/video/{BV}?p=0",
    f"https://www.bilibili.com/video/{BV}?p=1&p=2",
    f"https://www.bilibili.com/video/{BV}?p=-1",
    "https://www.bilibili.com/bangumi/play/ep1",
])
def test_ambiguous_or_unsupported_source_scope_fails(url):
    with pytest.raises(IngestError) as err:
        normalize_url(url)
    assert err.value.code == "UNSUPPORTED_URL"


@pytest.mark.parametrize("url", [
    f"https://youtube.com.evil.example/watch?v={ID}",
    f"https://evil.example/watch?url=https://youtube.com/watch?v={ID}",
    f"https://notyoutube.com/watch?v={ID}",
    f"https://evil.youtube.com/watch?v={ID}",
])
def test_exact_hostname_matching(url):
    with pytest.raises(IngestError) as err:
        normalize_url(url)
    assert err.value.code == "UNSUPPORTED_PLATFORM"


@pytest.mark.parametrize("url", [
    f"https://youtube.com@127.0.0.1/watch?v={ID}",
    f"https://user:password@youtube.com/watch?v={ID}",
    f"https://youtube.com:8443/watch?v={ID}",
    f"https://youtube.com./watch?v={ID}",
    f"https://youtube.com\\@evil.example/watch?v={ID}",
    f" https://youtube.com/watch?v={ID}",
    f"https://youtube.com/watch?v={ID}\n",
    "file:///etc/passwd",
])
def test_unsafe_url_syntax(url):
    with pytest.raises(IngestError) as err:
        normalize_url(url)
    assert err.value.code == "UNSAFE_URL"


def test_bilibili_parts_and_locators():
    first = normalize_url(f"https://www.bilibili.com/video/{BV}?spm_id_from=abc")
    second = normalize_url(f"https://m.bilibili.com/video/{BV}/?p=2&t=12.5")
    assert first.identity != second.identity
    assert second.identity == normalize_url(f"https://bilibili.com/video/{BV}", part=2).identity
    assert second.focus_timestamp_ms == 12500
    assert source_locator(second, 12599) == f"https://www.bilibili.com/video/{BV}?p=2&t=12"
    assert source_locator(normalize_url(f"https://youtu.be/{ID}"), 1000).endswith("&t=1s")
    assert normalize_url("https://www.bilibili.com/video/av170001").source_id == "av170001"


def test_conflicting_part_selection_fails():
    with pytest.raises(IngestError):
        normalize_url(f"https://bilibili.com/video/{BV}?p=2", part=3)
    with pytest.raises(IngestError):
        normalize_url(f"https://youtu.be/{ID}", part=2)
    with pytest.raises(IngestError):
        normalize_url(f"https://bilibili.com/video/{BV}", part=True)


def test_b23_uses_guarded_resolver_and_preserves_destination_identity():
    class FakeClient:
        def resolve_redirect(self, url):
            assert url == "https://b23.tv/abc123"
            return f"https://www.bilibili.com/video/{BV}?p=3&t=24"
    source = normalize_url("https://b23.tv/abc123", client=FakeClient())
    assert source.identity == f"bilibili:{BV}:3"
    assert source.focus_timestamp_ms == 24000


@pytest.mark.parametrize("destination", [f"https://youtube.com/watch?v={ID}", "https://b23.tv/again", "https://127.0.0.1/"])
def test_b23_rejects_non_bilibili_destinations(destination):
    class FakeClient:
        def resolve_redirect(self, url):
            return destination
    with pytest.raises(IngestError):
        normalize_url("https://b23.tv/abc123", client=FakeClient())


def test_b23_preserves_short_link_focus_and_part():
    class FakeClient:
        def resolve_redirect(self, url):
            return f"https://www.bilibili.com/video/{BV}"
    source = normalize_url("https://b23.tv/abc123?p=2&t=24", client=FakeClient())
    assert source.part_id == "2"
    assert source.focus_timestamp_ms == 24000


def test_b23_conflicting_focus_rejected():
    class FakeClient:
        def resolve_redirect(self, url):
            return f"https://www.bilibili.com/video/{BV}?t=40"
    with pytest.raises(IngestError):
        normalize_url("https://b23.tv/abc123?t=24", client=FakeClient())
