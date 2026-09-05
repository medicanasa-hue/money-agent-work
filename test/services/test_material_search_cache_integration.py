"""Exercise provider parsing on repeated HTTP searches without external calls."""

from unittest.mock import Mock, patch

import pytest

from app.config import config
from app.models.schema import VideoAspect
from app.services import material, material_cache
from app.services.providers import pexels, pixabay


@pytest.fixture(autouse=True)
def isolated_search_config():
    settings = {
        "pexels_api_keys": "pexels-fixture",
        "pixabay_api_keys": "pixabay-fixture",
        "material_search_max_page": 1,
        "material_search_cache_enabled": True,
    }
    with patch.object(config, "app", settings), patch.object(config, "proxy", {}):
        material_cache.clear_material_search_cache()
        yield
        material_cache.clear_material_search_cache()


def _payload(provider):
    if provider == "pexels":
        return {"videos": [{
            "duration": 12.5,
            "url": "https://www.pexels.com/video/city-skyline-123456/",
            "image": "https://example.com/preview.jpg",
            "video_files": [{
                "link": "https://example.com/video.mp4",
                "width": 1080, "height": 1920,
            }],
        }]}
    return {"hits": [{
        "duration": 12.5, "tags": "city, skyline",
        "videos": {"large": {
            "url": "https://example.com/video.mp4",
            "width": 1080, "height": 1920,
            "thumbnail": "https://example.com/preview.jpg",
        }},
    }]}


def _search(provider, mode):
    if mode == "legacy":
        return getattr(material, f"search_videos_{provider}"), material
    module = pexels if provider == "pexels" else pixabay
    instance = module.PexelsProvider() if provider == "pexels" else module.PixabayProvider()
    return instance.search, module


@pytest.mark.parametrize("provider", ["pexels", "pixabay"])
@pytest.mark.parametrize("mode", ["legacy", "provider"])
def test_repeated_search_reuses_http_response_and_reapplies_filters(provider, mode):
    search, module = _search(provider, mode)
    response = Mock(status_code=200)
    response.json.return_value = _payload(provider)
    with patch.object(module.requests, "get", return_value=response) as get:
        first = search("city skyline", 5, VideoAspect.portrait)
        # Task ranking may enrich/mutate returned objects; cache hits must be fresh.
        first[0].tags.append("task-only")
        first[0].preview_quality_score = 0.9
        filtered = search("city skyline", 15, VideoAspect.portrait)
        repeated = search("city skyline", 5, VideoAspect.portrait)

    assert get.call_count == 1
    assert filtered == []
    assert repeated[0].duration == 12.5
    assert repeated[0].search_query == "city skyline"
    assert "task-only" not in repeated[0].tags
    assert repeated[0].preview_quality_score is None
    assert repeated[0].preview_url == "https://example.com/preview.jpg"
    response.close.assert_called_once()


@pytest.mark.parametrize("provider", ["pexels", "pixabay"])
@pytest.mark.parametrize("mode", ["legacy", "provider"])
def test_randomized_page_is_selected_before_cache_lookup(provider, mode):
    search, module = _search(provider, mode)
    response = Mock(status_code=200)
    response.json.return_value = _payload(provider)
    with patch.object(module, "get_search_page", side_effect=[1, 2, 1]), patch.object(
        module.requests, "get", return_value=response
    ) as get:
        for _ in range(3):
            assert search("city skyline", 5, VideoAspect.portrait)
    assert get.call_count == 2
    assert "page=1" in get.call_args_list[0].args[0]
    assert "page=2" in get.call_args_list[1].args[0]


@pytest.mark.parametrize("provider", ["pexels", "pixabay"])
def test_missing_credentials_do_not_reuse_previous_results(provider):
    search, module = _search(provider, "provider")
    response = Mock(status_code=200)
    response.json.return_value = _payload(provider)
    with patch.object(module.requests, "get", return_value=response) as get:
        assert search("city skyline", 5, VideoAspect.portrait)
        with patch.object(config, "app", {"material_search_cache_enabled": True}):
            assert search("city skyline", 5, VideoAspect.portrait) == []
    assert get.call_count == 1
