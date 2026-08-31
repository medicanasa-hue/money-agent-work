import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch

import requests
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect
from app.services import material
from app.services import providers as provider_registry
from app.services.providers import loc as loc_provider
from app.services.providers import europeana as europeana_provider
from app.services.providers import museum as museum_provider
from app.services.providers import openverse as openverse_provider
from app.services.providers.coverr import CoverrProvider
from app.services.providers.archive_org import ArchiveOrgProvider
from app.services.providers.dvids import DVIDSProvider
from app.services.providers.loc import LibraryOfCongressProvider
from app.services.providers.nasa import NASAProvider
from app.services.providers.noaa_ocean import NOAOOceanExplorationProvider
from app.services.providers.pexels import PexelsProvider, _pexels_page_title
from app.services.providers.pixabay import PixabayProvider
from app.services.providers.vecteezy import (
    VecteezyProvider,
    resolve_vecteezy_download_url,
)
from app.services.providers.wikimedia import WikimediaProvider
from app.services.providers import utils as provider_utils


class TestMaterialTlsVerification(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)
        config.app["video_cooldown_enabled"] = False
        config.app["photo_fallback_enabled"] = False
        config.app["twelvelabs_material_rerank_enabled"] = False
        config.app["twelvelabs_visual_rerank_enabled"] = False
        config.app["twelvelabs_clip_qa_enabled"] = False

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    def test_material_tls_tests_disable_twelvelabs_clip_qa(self):
        """Unit tests must never spend TwelveLabs quota on fake media."""
        self.assertFalse(config.app.get("twelvelabs_clip_qa_enabled", False))

    def test_search_pexels_uses_tls_verification_by_default(self):
        """
        默认路径必须开启 TLS 校验，避免素材 API key 和返回的素材 URL
        在公共网络或不可信代理环境中被中间人攻击截获或篡改。
        """
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "duration": 8,
                        "image": "https://example.com/video-preview.jpg",
                        "video_files": [
                            {
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/video.mp4",
                            }
                        ],
                    }
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response) as get:
            results = material.search_videos_pexels("cat", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].width, 1080)
        self.assertEqual(results[0].height, 1920)
        self.assertEqual(results[0].search_query, "cat")
        self.assertEqual(results[0].title, "")
        self.assertEqual(results[0].description, "")
        self.assertEqual(results[0].tags, [])
        self.assertEqual(
            results[0].preview_url,
            "https://example.com/video-preview.jpg",
        )
        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_pexels_accepts_larger_matching_aspect_file(self):
        config.app["pexels_api_keys"] = ["pexels-key"]

        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "duration": 8,
                        "image": "https://example.com/provider-preview.jpg",
                        "video_files": [
                            {
                                "width": 2160,
                                "height": 3840,
                                "link": "https://example.com/video-4k.mp4",
                            }
                        ],
                    }
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response):
            results = material.search_videos_pexels("cat", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/video-4k.mp4")
        self.assertEqual(
            results[0].preview_url,
            "https://example.com/provider-preview.jpg",
        )
        self.assertEqual(results[0].width, 2160)
        self.assertEqual(results[0].height, 3840)

    def test_search_pixabay_allows_explicit_tls_disable_for_proxy(self):
        """
        少数企业代理会使用自签证书。该场景必须显式配置关闭 TLS 校验，
        不能再由代码硬编码默认关闭。
        """
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.app["tls_verify"] = False
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "duration": 8,
                        "tags": " cat, pet, animal ",
                        "videos": {
                            "large": {
                                "width": 1920,
                                "height": 1920,
                                "url": "https://example.com/video.mp4",
                            }
                        },
                    }
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response) as get:
            results = material.search_videos_pixabay("cat", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].width, 1920)
        self.assertEqual(results[0].height, 1920)
        self.assertEqual(results[0].search_query, "cat")
        self.assertEqual(results[0].tags, ["cat", "pet", "animal"])
        self.assertEqual(results[0].title, "")
        self.assertEqual(results[0].description, "")
        self.assertFalse(get.call_args.kwargs["verify"])

    def test_search_pixabay_uses_best_matching_video_variant(self):
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1080,
                                "height": 1920,
                                "url": "https://example.com/portrait-hd.mp4",
                                "thumbnail": "https://example.com/portrait-hd.jpg",
                            },
                            "fullHD": {
                                "width": 2160,
                                "height": 3840,
                                "url": "https://example.com/portrait-4k.mp4",
                                "thumbnail": "https://example.com/portrait-4k.jpg",
                            },
                        },
                    }
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response):
            results = material.search_videos_pixabay("cat", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/portrait-4k.mp4")
        self.assertEqual(results[0].preview_url, "https://example.com/portrait-4k.jpg")
        self.assertEqual(results[0].width, 2160)
        self.assertEqual(results[0].height, 3840)

    def test_search_pixabay_rejects_items_below_target_height(self):
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1920,
                                "height": 1080,
                                "url": "https://example.com/landscape.mp4",
                            }
                        },
                    },
                    {
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1080,
                                "height": 1920,
                                "url": "https://example.com/portrait.mp4",
                            }
                        },
                    },
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response):
            results = material.search_videos_pixabay("cat", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/portrait.mp4")
        self.assertEqual(results[0].width, 1080)
        self.assertEqual(results[0].height, 1920)

    def test_save_video_uses_tls_verification_by_default(self):
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(content=b"fake-video")

        class FakeVideoFileClip:
            duration = 1
            fps = 24

            def __init__(self, path):
                self.path = path

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "app.services.material.requests.get", return_value=fake_response
            ) as get, patch("app.services.material.VideoFileClip", FakeVideoFileClip):
                video_path = material.save_video(
                    "https://example.com/video.mp4?token=abc", save_dir=temp_dir
                )

            self.assertTrue(os.path.exists(video_path))
            self.assertTrue(get.call_args.kwargs["verify"])

    def test_save_video_uses_a_bounded_read_timeout(self):
        """A stalled stock host must not hold an entire render for several minutes."""
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(content=b"fake-video")

        class FakeVideoFileClip:
            duration = 1
            fps = 24

            def __init__(self, path):
                self.path = path

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "app.services.material.requests.get", return_value=fake_response
            ) as get, patch("app.services.material.VideoFileClip", FakeVideoFileClip):
                video_path = material.save_video(
                    "https://example.com/video.mp4", save_dir=temp_dir
                )

            self.assertTrue(os.path.exists(video_path))
            self.assertEqual(get.call_args.kwargs["timeout"], (30, 90))

    def test_save_video_stops_a_stream_that_exceeds_the_total_budget(self):
        """A slow but non-idle response must not bypass the read-timeout limit."""
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            content=b"fallback-content",
            iter_content=lambda chunk_size: iter((b"first", b"second")),
        )

        class FakeVideoFileClip:
            duration = 1
            fps = 24

            def __init__(self, path):
                self.path = path

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(
                    material.requests, "get", return_value=fake_response
                ) as get,
                patch.object(material, "VideoFileClip", FakeVideoFileClip),
                patch.object(
                    material,
                    "_VIDEO_DOWNLOAD_TOTAL_TIMEOUT_SECONDS",
                    -1,
                    create=True,
                ),
            ):
                video_path = material.save_video(
                    "https://example.com/slow.mp4", save_dir=temp_dir
                )

            self.assertEqual(video_path, "")
            self.assertEqual(os.listdir(temp_dir), [])
            self.assertTrue(get.call_args.kwargs["stream"])

    def test_save_video_rejects_declared_content_length_above_limit(self):
        close = Mock()

        def unexpected_stream(*, chunk_size):
            raise AssertionError("oversized response body must not be read")

        fake_response = SimpleNamespace(
            headers={"Content-Length": "6", "Content-Type": "video/mp4"},
            iter_content=unexpected_stream,
            close=close,
        )

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(material.requests, "get", return_value=fake_response),
            patch.object(material, "_MAX_VIDEO_DOWNLOAD_BYTES", 5, create=True),
        ):
            video_path = material.save_video(
                "https://example.com/oversized.mp4", save_dir=temp_dir
            )

            self.assertEqual(video_path, "")
            self.assertEqual(os.listdir(temp_dir), [])

        close.assert_called_once_with()

    def test_save_video_stops_stream_when_received_bytes_exceed_limit(self):
        close = Mock()
        fake_response = SimpleNamespace(
            headers={"Content-Type": "video/mp4"},
            iter_content=lambda chunk_size: iter((b"abc", b"def")),
            close=close,
        )

        class FakeVideoFileClip:
            duration = 1
            fps = 24

            def __init__(self, path):
                self.path = path

            def close(self):
                return None

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(material.requests, "get", return_value=fake_response),
            patch.object(material, "VideoFileClip", FakeVideoFileClip),
            patch.object(material, "_MAX_VIDEO_DOWNLOAD_BYTES", 5, create=True),
        ):
            video_path = material.save_video(
                "https://example.com/streamed-oversized.mp4", save_dir=temp_dir
            )

            self.assertEqual(video_path, "")
            self.assertEqual(os.listdir(temp_dir), [])

        close.assert_called_once_with()

    def test_save_video_closes_a_stream_when_writing_fails(self):
        close = Mock()
        fake_response = SimpleNamespace(content=b"fake-video", close=close)

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(material.requests, "get", return_value=fake_response),
            patch("builtins.open", side_effect=OSError("disk full")),
        ):
            self.assertEqual(
                material.save_video("https://example.com/video.mp4", temp_dir),
                "",
            )

        close.assert_called_once_with()

    def test_save_video_uses_same_cache_for_fragment_variants(self):
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(content=b"fake-video")

        class FakeVideoFileClip:
            duration = 1
            fps = 24

            def __init__(self, path):
                self.path = path

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "app.services.material.requests.get", return_value=fake_response
            ) as get, patch("app.services.material.VideoFileClip", FakeVideoFileClip):
                first_path = material.save_video(
                    "https://example.com/video.mp4#first", save_dir=temp_dir
                )
                second_path = material.save_video(
                    "https://example.com/video.mp4#second", save_dir=temp_dir
                )

        self.assertEqual(first_path, second_path)
        self.assertEqual(get.call_count, 1)

    def test_download_videos_accepts_plain_string_concat_mode(self):
        """
        download_videos 可能被服务层或测试直接传入字符串模式，而不是
        VideoConcatMode 枚举。这里用空搜索词避免真实网络请求，只验证
        字符串 "random" 不会再因为访问 `.value` 抛 AttributeError。
        """
        result = material.download_videos(
            task_id="string-concat-mode",
            search_terms=[],
            video_concat_mode="random",
        )

        self.assertEqual(result, [])

    def test_normalize_search_terms_skips_case_and_space_variants(self):
        result = material._normalize_search_terms(
            [
                " City street ",
                "city   street",
                "CITY STREET",
                "office workers",
            ]
        )

        self.assertEqual(result, ["City street", "office workers"])

    def test_download_videos_normalizes_search_terms_before_searching(self):
        requested_terms = []

        def fake_search(search_term, minimum_duration, video_aspect):
            requested_terms.append(search_term)
            return []

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
        ):
            result = material.download_videos(
                task_id="normalized-search-terms",
                search_terms=[" City street ", "city   street", "CITY STREET"],
                source="pexels",
                audio_duration=1,
                max_clip_duration=5,
            )

        self.assertEqual(result, [])
        self.assertEqual(requested_terms, ["City street"])

    def test_download_videos_can_round_robin_terms_in_script_order(self):
        """
        开启按文案顺序匹配素材后，不能让第一个关键词的多个候选先把
        音频时长填满。这里模拟两个关键词各有多个候选，验证下载顺序是
        term1-第1个、term2-第1个、term1-第2个，贴近脚本叙事顺序。
        """
        search_results = {
            "opening city": [
                material.MaterialInfo(provider="pexels", url="https://v.example/a1.mp4", duration=3),
                material.MaterialInfo(provider="pexels", url="https://v.example/a2.mp4", duration=3),
            ],
            "middle office": [
                material.MaterialInfo(provider="pexels", url="https://v.example/b1.mp4", duration=3),
                material.MaterialInfo(provider="pexels", url="https://v.example/b2.mp4", duration=3),
            ],
        }
        downloaded_urls = []

        def fake_search(search_term, minimum_duration, video_aspect):
            return search_results[search_term]

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            downloaded_urls.append(video_url)
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(material, "save_video", side_effect=fake_save_video),
        ):
            result = material.download_videos(
                task_id="ordered-materials",
                search_terms=["opening city", "middle office"],
                source="pexels",
                audio_duration=7,
                max_clip_duration=3,
                match_script_order=True,
            )

        self.assertEqual(
            downloaded_urls,
            [
                "https://v.example/a1.mp4",
                "https://v.example/b1.mp4",
                "https://v.example/a2.mp4",
            ],
        )
        self.assertEqual(result, ["/tmp/a1.mp4", "/tmp/b1.mp4", "/tmp/a2.mp4"])

    def test_download_videos_honors_minimum_unique_visual_count(self):
        video_items = [
            material.MaterialInfo(
                provider="pexels",
                url=f"https://videos.example/clip-{index}.mp4",
                duration=5,
            )
            for index in range(1, 4)
        ]
        downloaded_urls = []

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            downloaded_urls.append(video_url)
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(
                config.app,
                {"material_directory": "", "photo_fallback_enabled": False},
                clear=False,
            ),
            patch.object(material, "search_videos_pexels", return_value=video_items),
            patch.object(material, "save_video", side_effect=fake_save_video),
        ):
            result = material.download_videos(
                task_id="crossfade-materials",
                search_terms=["city street"],
                source="pexels",
                video_concat_mode="sequential",
                audio_duration=9,
                max_clip_duration=5,
                minimum_unique_visual_count=3,
            )

        self.assertEqual(downloaded_urls, [item.url for item in video_items])
        self.assertEqual(
            result,
            ["/tmp/clip-1.mp4", "/tmp/clip-2.mp4", "/tmp/clip-3.mp4"],
        )

    def test_download_videos_applies_unique_visual_target_to_multi_paths(self):
        provider = SimpleNamespace(name="pexels")

        for match_script_order, helper_name in (
            (False, "_download_multi_source"),
            (True, "_download_multi_ordered"),
        ):
            with self.subTest(match_script_order=match_script_order):
                with (
                    patch.dict(
                        config.app,
                        {"enabled_video_sources": ["pexels"]},
                        clear=False,
                    ),
                    patch(
                        "app.services.providers.get_active_providers",
                        return_value=[provider],
                    ),
                    patch.object(
                        material,
                        "_download_multi_source",
                        return_value=["/tmp/one.mp4", "/tmp/two.mp4", "/tmp/three.mp4"],
                    ) as download_multi_source,
                    patch.object(
                        material,
                        "_download_multi_ordered",
                        return_value=["/tmp/one.mp4", "/tmp/two.mp4", "/tmp/three.mp4"],
                    ) as download_multi_ordered,
                    patch.object(
                        material,
                        "_append_photo_fallback",
                        side_effect=lambda paths, **_kwargs: paths,
                    ),
                ):
                    result = material.download_videos(
                        task_id="crossfade-multi-materials",
                        search_terms=["city street"],
                        source="multi",
                        audio_duration=9,
                        max_clip_duration=5,
                        match_script_order=match_script_order,
                        minimum_unique_visual_count=3,
                    )

                    selected_helper = {
                        "_download_multi_source": download_multi_source,
                        "_download_multi_ordered": download_multi_ordered,
                    }[helper_name]
                    self.assertEqual(
                        result,
                        ["/tmp/one.mp4", "/tmp/two.mp4", "/tmp/three.mp4"],
                    )
                    self.assertAlmostEqual(
                        selected_helper.call_args.kwargs["audio_duration"],
                        14.999,
                        places=3,
                    )

    def test_download_videos_uses_provider_pipeline_for_single_free_source(self):
        cases = (
            ("dvids", False, "_download_multi_source"),
            ("nasa", False, "_download_multi_source"),
            ("noaa_ocean", False, "_download_multi_source"),
            ("loc", False, "_download_multi_source"),
            ("wikimedia", True, "_download_multi_ordered"),
            ("archive_org", False, "_download_multi_source"),
        )

        for source, match_script_order, helper_name in cases:
            with self.subTest(source=source, match_script_order=match_script_order):
                provider = SimpleNamespace(name=source)
                with (
                    patch.dict(
                        config.app,
                        {"material_directory": "", "photo_fallback_enabled": False},
                        clear=False,
                    ),
                    patch(
                        "app.services.providers.get_active_providers",
                        return_value=[provider],
                    ) as get_active_providers,
                    patch.object(
                        material,
                        "_download_multi_source",
                        return_value=[f"/tmp/{source}.mp4"],
                    ) as download_multi_source,
                    patch.object(
                        material,
                        "_download_multi_ordered",
                        return_value=[f"/tmp/{source}.mp4"],
                    ) as download_multi_ordered,
                    patch.object(
                        material,
                        "search_videos_pexels",
                        return_value=[],
                    ) as legacy_search,
                ):
                    result = material.download_videos(
                        task_id=f"single-{source}",
                        search_terms=["city street"],
                        source=source,
                        audio_duration=1,
                        max_clip_duration=5,
                        match_script_order=match_script_order,
                    )

                selected_helper = {
                    "_download_multi_source": download_multi_source,
                    "_download_multi_ordered": download_multi_ordered,
                }[helper_name]
                self.assertEqual(result, [f"/tmp/{source}.mp4"])
                get_active_providers.assert_called_once_with([source])
                self.assertEqual(selected_helper.call_args.kwargs["providers"], [provider])
                legacy_search.assert_not_called()

    def test_download_videos_skips_url_variants_in_script_order(self):
        search_results = {
            "opening city": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/shared.mp4?download=1",
                    duration=3,
                ),
            ],
            "middle office": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/shared.mp4?download=2",
                    duration=3,
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/office-alt.mp4",
                    duration=3,
                ),
            ],
        }
        downloaded_urls = []

        def fake_search(search_term, minimum_duration, video_aspect):
            return search_results[search_term]

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            downloaded_urls.append(video_url)
            return f"/tmp/{urlparse(video_url).path.rsplit('/', 1)[-1]}"

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(material, "save_video", side_effect=fake_save_video),
        ):
            result = material.download_videos(
                task_id="ordered-material-url-variants",
                search_terms=["opening city", "middle office"],
                source="pexels",
                audio_duration=5,
                max_clip_duration=3,
                match_script_order=True,
            )

        self.assertEqual(
            downloaded_urls,
            [
                "https://v.example/shared.mp4?download=1",
                "https://v.example/office-alt.mp4",
            ],
        )
        self.assertEqual(result, ["/tmp/shared.mp4", "/tmp/office-alt.mp4"])

    def test_download_videos_script_order_keeps_best_url_variant_per_scene(self):
        search_results = {
            "opening city": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/shared.mp4?quality=low",
                    duration=3,
                    width=640,
                    height=360,
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/shared.mp4?quality=high",
                    duration=3,
                    width=1080,
                    height=1920,
                ),
            ],
        }
        downloaded_urls = []

        def fake_search(search_term, minimum_duration, video_aspect):
            return search_results[search_term]

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            downloaded_urls.append(video_url)
            return f"/tmp/{urlparse(video_url).path.rsplit('/', 1)[-1]}"

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(material, "save_video", side_effect=fake_save_video),
        ):
            result = material.download_videos(
                task_id="ordered-material-best-variant",
                search_terms=["opening city"],
                source="pexels",
                audio_duration=1,
                max_clip_duration=3,
                match_script_order=True,
            )

        self.assertEqual(
            downloaded_urls,
            ["https://v.example/shared.mp4?quality=high"],
        )
        self.assertEqual(result, ["/tmp/shared.mp4"])

    def test_download_videos_tries_scene_query_fallback_without_flattening_order(self):
        search_results = {
            "opening city skyline cinematic": [],
            "opening city skyline": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/opening.mp4",
                    duration=5,
                )
            ],
            "middle office": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/middle.mp4",
                    duration=5,
                )
            ],
        }
        requested_terms = []
        downloaded_urls = []

        def fake_search(search_term, minimum_duration, video_aspect):
            requested_terms.append(search_term)
            return search_results[search_term]

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            downloaded_urls.append(video_url)
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(material, "save_video", side_effect=fake_save_video),
            patch.object(material.logger, "info") as log_info,
        ):
            result = material.download_videos(
                task_id="ordered-materials-fallback",
                search_terms=["opening city skyline cinematic", "middle office"],
                source="pexels",
                audio_duration=8,
                max_clip_duration=5,
                match_script_order=True,
            )

        self.assertEqual(
            requested_terms,
            [
                "opening city skyline cinematic",
                "opening city skyline",
                "middle office",
            ],
        )
        self.assertEqual(
            downloaded_urls,
            ["https://v.example/opening.mp4", "https://v.example/middle.mp4"],
        )
        self.assertEqual(result, ["/tmp/opening.mp4", "/tmp/middle.mp4"])
        log_info.assert_any_call(
            "ordered material search: mode=single, scenes=2, "
            "fallback_used=1, unresolved=0"
        )
        info_messages = "\n".join(str(call.args[0]) for call in log_info.call_args_list)
        self.assertNotIn("opening city skyline cinematic", info_messages)
        self.assertNotIn("opening city skyline", info_messages)

    def test_script_order_retries_when_initial_scene_candidates_are_unrelated(self):
        search_results = {
            "mortgage interest rate chart": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/forest.mp4",
                    duration=5,
                    title="Forest waterfall",
                )
            ],
            "mortgage interest rate": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/mortgage.mp4",
                    duration=5,
                    title="Mortgage interest rate chart",
                )
            ],
        }
        requested_terms = []
        downloaded_urls = []

        def fake_search(search_term, minimum_duration, video_aspect):
            requested_terms.append(search_term)
            return search_results[search_term]

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            downloaded_urls.append(video_url)
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(material, "save_video", side_effect=fake_save_video),
            patch.object(material.logger, "info") as log_info,
        ):
            result = material.download_videos(
                task_id="ordered-materials-relevance-retry",
                search_terms=["mortgage interest rate chart"],
                source="pexels",
                audio_duration=1,
                max_clip_duration=5,
                match_script_order=True,
            )

        self.assertEqual(
            requested_terms,
            ["mortgage interest rate chart", "mortgage interest rate"],
        )
        self.assertEqual(downloaded_urls, ["https://v.example/mortgage.mp4"])
        self.assertEqual(result, ["/tmp/mortgage.mp4"])
        log_info.assert_any_call(
            "ordered material search: mode=single, scenes=1, "
            "fallback_used=1, unresolved=0"
        )

    def test_ordered_material_search_warns_without_logging_unresolved_query(self):
        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", return_value=[]),
            patch.object(material.logger, "info") as log_info,
            patch.object(material.logger, "warning") as log_warning,
        ):
            result = material.download_videos(
                task_id="ordered-materials-unresolved",
                search_terms=["private unrecoverable scene query cinematic"],
                source="pexels",
                audio_duration=5,
                max_clip_duration=5,
                match_script_order=True,
            )

        self.assertEqual(result, [])
        log_warning.assert_any_call(
            "ordered material search: mode=single, scenes=1, "
            "fallback_used=0, unresolved=1"
        )
        messages = "\n".join(
            str(call.args[0])
            for call in log_info.call_args_list + log_warning.call_args_list
        )
        self.assertNotIn("private unrecoverable scene query cinematic", messages)

    def test_multi_ordered_tries_scene_query_fallback_without_flattening_order(self):
        search_results = {
            "opening city skyline cinematic": [],
            "opening city skyline": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/opening.mp4",
                    duration=5,
                )
            ],
            "middle office": [
                material.MaterialInfo(
                    provider="pixabay",
                    url="https://v.example/middle.mp4",
                    duration=5,
                )
            ],
        }
        requested_terms = []
        downloaded_urls = []

        def fake_search_all(search_term, providers, minimum_duration, video_aspect):
            requested_terms.append(search_term)
            return search_results[search_term]

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            downloaded_urls.append(video_url)
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.object(material, "_search_all_providers", side_effect=fake_search_all),
            patch.object(material, "save_video", side_effect=fake_save_video),
            patch.object(material.logger, "info") as log_info,
        ):
            result = material._download_multi_ordered(
                task_id="multi-ordered-materials-fallback",
                search_terms=["opening city skyline cinematic", "middle office"],
                providers=[SimpleNamespace(name="pexels")],
                video_aspect=material.VideoAspect.portrait,
                audio_duration=8,
                max_clip_duration=5,
                material_directory="/tmp",
            )

        self.assertEqual(
            requested_terms,
            [
                "opening city skyline cinematic",
                "opening city skyline",
                "middle office",
            ],
        )
        self.assertEqual(
            downloaded_urls,
            ["https://v.example/opening.mp4", "https://v.example/middle.mp4"],
        )
        self.assertEqual(result, ["/tmp/opening.mp4", "/tmp/middle.mp4"])
        log_info.assert_any_call(
            "ordered material search: mode=multi, scenes=2, "
            "fallback_used=1, unresolved=0"
        )
        info_messages = "\n".join(str(call.args[0]) for call in log_info.call_args_list)
        self.assertNotIn("opening city skyline cinematic", info_messages)
        self.assertNotIn("opening city skyline", info_messages)

    def test_multi_ordered_skips_url_variants_across_scene_groups(self):
        search_results = {
            "opening city": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/shared.mp4?download=1",
                    duration=3,
                ),
            ],
            "middle office": [
                material.MaterialInfo(
                    provider="pixabay",
                    url="https://v.example/shared.mp4?download=2",
                    duration=3,
                ),
                material.MaterialInfo(
                    provider="pixabay",
                    url="https://v.example/office-alt.mp4",
                    duration=3,
                ),
            ],
        }
        downloaded_urls = []

        def fake_search_all(search_term, providers, minimum_duration, video_aspect):
            return search_results[search_term]

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            downloaded_urls.append(video_url)
            return f"/tmp/{urlparse(video_url).path.rsplit('/', 1)[-1]}"

        with (
            patch.object(material, "_search_all_providers", side_effect=fake_search_all),
            patch.object(material, "save_video", side_effect=fake_save_video),
        ):
            result = material._download_multi_ordered(
                task_id="multi-ordered-url-variants",
                search_terms=["opening city", "middle office"],
                providers=[SimpleNamespace(name="pexels"), SimpleNamespace(name="pixabay")],
                video_aspect=material.VideoAspect.portrait,
                audio_duration=5,
                max_clip_duration=3,
                material_directory="/tmp",
            )

        self.assertEqual(
            downloaded_urls,
            [
                "https://v.example/shared.mp4?download=1",
                "https://v.example/office-alt.mp4",
            ],
        )
        self.assertEqual(result, ["/tmp/shared.mp4", "/tmp/office-alt.mp4"])

    def test_multi_source_keeps_best_exact_duplicate_across_terms(self):
        search_results = {
            "opening city": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/shared.mp4",
                    duration=3,
                    width=640,
                    height=360,
                ),
            ],
            "middle office": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/shared.mp4",
                    duration=3,
                    width=1080,
                    height=1920,
                ),
            ],
        }

        def fake_search_all(search_term, providers, minimum_duration, video_aspect):
            return search_results[search_term]

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.object(material, "_search_all_providers", side_effect=fake_search_all),
            patch.object(material, "save_video", side_effect=fake_save_video),
            patch.object(material, "_mark_video_cooldown_used") as mark_used,
        ):
            result = material._download_multi_source(
                task_id="multi-source-best-duplicate",
                search_terms=["opening city", "middle office"],
                providers=[SimpleNamespace(name="pexels")],
                video_aspect=material.VideoAspect.portrait,
                video_concat_mode=material.VideoConcatMode.sequential,
                audio_duration=1,
                max_clip_duration=3,
                material_directory="/tmp",
            )

        self.assertEqual(result, ["/tmp/shared.mp4"])
        self.assertEqual(mark_used.call_args.args[0].width, 1080)

    def test_multi_source_keeps_a_second_provider_after_duration_is_met(self):
        search_results = {
            "solar panels": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/pexels-solar.mp4",
                    duration=5,
                    width=1080,
                    height=1920,
                ),
                material.MaterialInfo(
                    provider="pixabay",
                    url="https://v.example/pixabay-solar.mp4",
                    duration=5,
                    width=1080,
                    height=1920,
                ),
            ]
        }
        downloaded_urls = []

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            downloaded_urls.append(video_url)
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.object(
                material,
                "_search_all_providers",
                side_effect=lambda *args: search_results[args[0]],
            ),
            patch.object(material, "save_video", side_effect=fake_save_video),
        ):
            result = material._download_multi_source(
                task_id="multi-source-diversity",
                search_terms=["solar panels"],
                providers=[SimpleNamespace(name="pexels"), SimpleNamespace(name="pixabay")],
                video_aspect=material.VideoAspect.portrait,
                video_concat_mode=material.VideoConcatMode.sequential,
                audio_duration=4,
                max_clip_duration=5,
                material_directory="/tmp",
            )

        self.assertEqual(
            downloaded_urls,
            [
                "https://v.example/pexels-solar.mp4",
                "https://v.example/pixabay-solar.mp4",
            ],
        )
        self.assertEqual(len(result), 2)

    def test_download_videos_prefers_best_scored_single_source_candidate(self):
        search_results = [
            material.MaterialInfo(
                provider="pexels",
                url="https://v.example/too-long.mp4",
                duration=120,
            ),
            material.MaterialInfo(
                provider="pexels",
                url="https://v.example/best-fit.mp4",
                duration=5,
            ),
            material.MaterialInfo(
                provider="pexels",
                url="https://v.example/okay-fit.mp4",
                duration=8,
            ),
        ]
        downloaded_urls = []

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            downloaded_urls.append(video_url)
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", return_value=search_results),
            patch.object(material, "save_video", side_effect=fake_save_video),
        ):
            result = material.download_videos(
                task_id="rank-single-source",
                search_terms=["city"],
                source="pexels",
                video_concat_mode="sequential",
                audio_duration=1,
                max_clip_duration=5,
            )

        self.assertEqual(downloaded_urls, ["https://v.example/best-fit.mp4"])
        self.assertEqual(result, ["/tmp/best-fit.mp4"])

    def test_download_videos_keeps_best_exact_duplicate_candidate(self):
        search_results = [
            material.MaterialInfo(
                provider="pexels",
                url="https://v.example/shared.mp4",
                duration=5,
                width=640,
                height=360,
            ),
            material.MaterialInfo(
                provider="pexels",
                url="https://v.example/shared.mp4",
                duration=5,
                width=1080,
                height=1920,
            ),
        ]
        downloaded_urls = []

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            downloaded_urls.append(video_url)
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", return_value=search_results),
            patch.object(material, "save_video", side_effect=fake_save_video),
            patch.object(material, "_mark_video_cooldown_used") as mark_used,
        ):
            result = material.download_videos(
                task_id="best-exact-duplicate",
                search_terms=["city"],
                source="pexels",
                video_concat_mode="sequential",
                audio_duration=1,
                max_clip_duration=5,
            )

        self.assertEqual(downloaded_urls, ["https://v.example/shared.mp4"])
        self.assertEqual(result, ["/tmp/shared.mp4"])
        self.assertEqual(mark_used.call_args.args[0].width, 1080)

    def test_download_videos_filters_low_quality_single_source_candidates(self):
        search_results = [
            material.MaterialInfo(provider="pexels", url="", duration=8),
            material.MaterialInfo(provider="pexels", url="https://v.example/short.mp4", duration=2),
            material.MaterialInfo(provider="pexels", url="https://v.example/long.mp4", duration=240),
            material.MaterialInfo(provider="pexels", url="https://v.example/good.mp4", duration=6),
            material.MaterialInfo(provider="pexels", url="https://v.example/good.mp4", duration=7),
        ]
        downloaded_urls = []

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            downloaded_urls.append(video_url)
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(
                config.app,
                {"material_directory": "", "max_material_duration": 180},
            ),
            patch.object(material, "search_videos_pexels", return_value=search_results),
            patch.object(material, "save_video", side_effect=fake_save_video),
        ):
            result = material.download_videos(
                task_id="filter-single-source",
                search_terms=["city"],
                source="pexels",
                video_concat_mode="sequential",
                audio_duration=1,
                max_clip_duration=5,
            )

        self.assertEqual(downloaded_urls, ["https://v.example/good.mp4"])
        self.assertEqual(result, ["/tmp/good.mp4"])

    def test_download_videos_skips_url_variants_in_ranked_pool(self):
        search_results = [
            material.MaterialInfo(
                provider="pexels",
                url="https://v.example/shared.mp4?download=1",
                duration=5,
            ),
            material.MaterialInfo(
                provider="pexels",
                url="https://v.example/shared.mp4?download=2",
                duration=5,
            ),
            material.MaterialInfo(
                provider="pexels",
                url="https://v.example/next.mp4",
                duration=5,
            ),
        ]
        downloaded_urls = []

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            downloaded_urls.append(video_url)
            return f"/tmp/{urlparse(video_url).path.rsplit('/', 1)[-1]}"

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", return_value=search_results),
            patch.object(material, "save_video", side_effect=fake_save_video),
        ):
            result = material.download_videos(
                task_id="ranked-url-variants",
                search_terms=["city"],
                source="pexels",
                video_concat_mode="sequential",
                audio_duration=8,
                max_clip_duration=5,
            )

        self.assertEqual(
            downloaded_urls,
            [
                "https://v.example/shared.mp4?download=1",
                "https://v.example/next.mp4",
            ],
        )
        self.assertEqual(result, ["/tmp/shared.mp4", "/tmp/next.mp4"])

    def test_search_video_candidates_returns_ranked_items_without_downloading(self):
        search_results = [
            material.MaterialInfo(
                provider="pexels",
                url="https://v.example/too-long.mp4",
                duration=240,
            ),
            material.MaterialInfo(
                provider="pexels",
                url="https://v.example/best.mp4",
                duration=5,
            ),
            material.MaterialInfo(
                provider="pexels",
                url="https://v.example/okay.mp4",
                duration=9,
            ),
        ]

        with (
            patch.dict(config.app, {"max_material_duration": 180}),
            patch.object(material, "search_videos_pexels", return_value=search_results),
            patch.object(material, "save_video") as save_video,
        ):
            result = material.search_video_candidates(
                search_terms=["city"],
                source="pexels",
                max_clip_duration=5,
                limit=6,
            )

        self.assertEqual(
            [item.url for item in result],
            ["https://v.example/best.mp4", "https://v.example/okay.mp4"],
        )
        save_video.assert_not_called()

    def test_search_video_candidates_keeps_best_exact_duplicate_candidate(self):
        search_results = [
            material.MaterialInfo(
                provider="pexels",
                url="https://v.example/shared.mp4",
                duration=5,
                width=640,
                height=360,
            ),
            material.MaterialInfo(
                provider="pexels",
                url="https://v.example/shared.mp4",
                duration=5,
                width=1080,
                height=1920,
            ),
        ]

        with patch.object(material, "search_videos_pexels", return_value=search_results):
            result = material.search_video_candidates(
                search_terms=["city"],
                source="pexels",
                max_clip_duration=5,
                limit=6,
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].width, 1080)

    def test_search_all_providers_skips_url_variants(self):
        provider = SimpleNamespace(
            name="pexels",
            search=lambda search_term, minimum_duration, video_aspect: [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/shared.mp4?token=one",
                    duration=5,
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/shared.mp4?token=two#preview",
                    duration=5,
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/next.mp4",
                    duration=5,
                ),
            ],
        )

        result = material._search_all_providers(
            search_term="city",
            providers=[provider],
            minimum_duration=5,
            video_aspect=material.VideoAspect.portrait,
        )

        self.assertEqual(
            [item.url for item in result],
            [
                "https://v.example/shared.mp4?token=one",
                "https://v.example/next.mp4",
            ],
        )

    def test_search_all_providers_keeps_best_url_variant(self):
        provider = SimpleNamespace(
            name="pexels",
            search=lambda search_term, minimum_duration, video_aspect: [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/shared.mp4?quality=low",
                    duration=5,
                    width=640,
                    height=360,
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/shared.mp4?quality=high",
                    duration=5,
                    width=1080,
                    height=1920,
                ),
            ],
        )

        result = material._search_all_providers(
            search_term="city",
            providers=[provider],
            minimum_duration=5,
            video_aspect=material.VideoAspect.portrait,
        )

        self.assertEqual(
            [item.url for item in result],
            ["https://v.example/shared.mp4?quality=high"],
        )

    def test_rank_materials_prefers_high_resolution_when_known(self):
        low_resolution = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/low.mp4",
            duration=5,
            width=640,
            height=360,
        )
        high_resolution = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/high.mp4",
            duration=5,
            width=1080,
            height=1920,
        )

        ranked = material._rank_materials(
            [low_resolution, high_resolution],
            max_clip_duration=5,
        )

        self.assertEqual(ranked[0].url, "https://v.example/high.mp4")

    def test_rank_materials_excludes_known_low_resolution_when_equally_relevant_alternative_exists(self):
        low_resolution = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/low-solar.mp4",
            duration=5,
            width=640,
            height=360,
            search_query="solar panels",
            title="Solar panels on a rooftop",
        )
        high_resolution = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/high-solar.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="solar panels",
            title="Solar panels on a rooftop",
        )

        ranked = material._rank_materials(
            [low_resolution, high_resolution],
            max_clip_duration=5,
        )

        self.assertEqual([item.url for item in ranked], [high_resolution.url])

    def test_rank_materials_keeps_low_resolution_when_it_is_more_relevant(self):
        low_resolution_match = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/low-solar-match.mp4",
            duration=5,
            width=640,
            height=360,
            search_query="solar panels",
            title="Solar panels on a rooftop",
        )
        high_resolution_unrelated = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/high-unrelated.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="solar panels",
            title="Downtown traffic at night",
        )

        ranked = material._rank_materials(
            [low_resolution_match, high_resolution_unrelated],
            max_clip_duration=5,
        )

        self.assertEqual(
            [item.url for item in ranked],
            [low_resolution_match.url, high_resolution_unrelated.url],
        )

    def test_rank_materials_prefers_target_portrait_orientation(self):
        landscape = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/landscape.mp4",
            duration=5,
            width=1920,
            height=1080,
        )
        portrait = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/portrait.mp4",
            duration=5,
            width=1080,
            height=1920,
        )

        ranked = material._rank_materials(
            [landscape, portrait],
            max_clip_duration=5,
            video_aspect=material.VideoAspect.portrait,
        )

        self.assertEqual(ranked[0].url, portrait.url)

    def test_rank_materials_prefers_portrait_over_square_for_portrait_target(self):
        square = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/square.mp4",
            duration=5,
            width=1080,
            height=1080,
        )
        portrait = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/portrait.mp4",
            duration=5,
            width=1080,
            height=1920,
        )

        ranked = material._rank_materials(
            [square, portrait],
            max_clip_duration=5,
            video_aspect=material.VideoAspect.portrait,
        )

        self.assertEqual(ranked[0].url, portrait.url)

    def test_rank_materials_prefers_native_portrait_resolution_over_upscale(self):
        upscaled_portrait = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/720-portrait.mp4",
            duration=5,
            width=720,
            height=1280,
        )
        native_portrait = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/1080-portrait.mp4",
            duration=5,
            width=1080,
            height=1920,
        )

        ranked = material._rank_materials(
            [upscaled_portrait, native_portrait],
            max_clip_duration=5,
            video_aspect=material.VideoAspect.portrait,
        )

        self.assertEqual(ranked[0].url, native_portrait.url)

    def test_rank_materials_prefers_target_landscape_orientation(self):
        portrait = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/portrait.mp4",
            duration=5,
            width=1080,
            height=1920,
        )
        landscape = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/landscape.mp4",
            duration=5,
            width=1920,
            height=1080,
        )

        ranked = material._rank_materials(
            [portrait, landscape],
            max_clip_duration=5,
            video_aspect=material.VideoAspect.landscape,
        )

        self.assertEqual(ranked[0].url, landscape.url)

    def test_rank_materials_keeps_best_url_variant(self):
        low_resolution = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/shared.mp4?download=low",
            duration=5,
            width=640,
            height=360,
        )
        high_resolution = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/shared.mp4?download=high",
            duration=5,
            width=1080,
            height=1920,
        )

        ranked = material._rank_materials(
            [low_resolution, high_resolution],
            max_clip_duration=5,
        )

        self.assertEqual([item.url for item in ranked], [high_resolution.url])

    def test_rank_materials_keeps_candidates_with_unknown_resolution(self):
        unknown_resolution = material.MaterialInfo(
            provider="coverr",
            url="https://v.example/unknown.mp4",
            duration=5,
        )
        low_resolution = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/low.mp4",
            duration=5,
            width=640,
            height=360,
        )

        ranked = material._rank_materials(
            [low_resolution, unknown_resolution],
            max_clip_duration=5,
        )

        self.assertEqual(
            {item.url for item in ranked},
            {"https://v.example/unknown.mp4", "https://v.example/low.mp4"},
        )

    def test_rank_materials_uses_content_metadata_to_break_equal_score(self):
        unrelated = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/ocean.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="solar panels",
            title="Waves on a quiet beach",
            tags=["ocean", "coast"],
        )
        matching = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/solar.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="solar panels",
            title="Solar panels on a rooftop",
            tags=["renewable energy"],
        )

        ranked = material._rank_materials(
            [unrelated, matching],
            max_clip_duration=5,
        )

        self.assertEqual(ranked[0].url, "https://v.example/solar.mp4")

    def test_rank_materials_prefers_stronger_content_match_over_provider_bias(self):
        pexels_unrelated = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/pexels-unrelated.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="solar panels",
            title="Downtown traffic at night",
        )
        pixabay_matching = material.MaterialInfo(
            provider="pixabay",
            url="https://v.example/pixabay-matching.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="solar panels",
            title="Solar panels on a rooftop",
        )

        ranked = material._rank_materials(
            [pexels_unrelated, pixabay_matching],
            max_clip_duration=5,
        )

        self.assertEqual(ranked[0].url, pixabay_matching.url)

    def test_rank_materials_normalizes_diacritics_for_content_match(self):
        unrelated = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/unrelated-city.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="\u0130stanbul \u015fehir",
            title="Mountain village",
        )
        matching = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/istanbul.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="\u0130stanbul \u015fehir",
            title="Istanbul sehir skyline",
        )

        ranked = material._rank_materials(
            [unrelated, matching],
            max_clip_duration=5,
        )

        self.assertEqual(ranked[0].url, matching.url)

    def test_light_stem_normalizes_common_inflections_without_shortening_roots(
        self,
    ):
        self.assertEqual(material._light_stem("running"), "run")
        self.assertEqual(material._light_stem("runs"), "run")
        self.assertEqual(material._light_stem("kitaplar"), "kitap")
        self.assertEqual(material._light_stem("kitapta"), "kitap")
        self.assertEqual(material._light_stem("kosuyor"), "kos")
        self.assertEqual(material._light_stem("kitap"), "kitap")
        self.assertEqual(material._light_stem("kit"), "kit")

    def test_score_content_match_keeps_exact_matches_at_full_score(self):
        item = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/solar.mp4",
            duration=5,
            search_query="solar panels",
            title="Solar panels on a rooftop",
        )

        self.assertEqual(material._score_content_match(item), 1.0)

    def test_score_content_match_rewards_inflectional_matches(self):
        item = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/running.mp4",
            duration=5,
            search_query="running",
            title="Athlete runs on a track",
        )

        self.assertAlmostEqual(material._score_content_match(item), 0.85)

    def test_score_content_match_gives_partial_credit_for_close_spelling(self):
        item = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/economy.mp4",
            duration=5,
            search_query="economy",
            title="Econmy outlook",
        )

        self.assertAlmostEqual(material._score_content_match(item), 0.6)

    def test_score_content_match_rejects_unrelated_words(self):
        item = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/cat.mp4",
            duration=5,
            search_query="finans",
            title="Kedi oynuyor",
        )

        self.assertEqual(material._score_content_match(item), 0.0)

    def test_score_content_match_rewards_bilingual_visual_concepts(self):
        item = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/prices.mp4",
            duration=5,
            search_query="enflasyon",
            title="Grocery prices in a market",
        )

        self.assertGreater(material._score_content_match(item), 0.0)

    def test_rank_materials_prefers_visual_concept_over_unrelated_metadata(self):
        unrelated = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/unrelated.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="inflation",
            title="Downtown skyline at sunset",
        )
        matching = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/prices.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="inflation",
            title="Rising grocery prices",
        )

        ranked = material._rank_materials([unrelated, matching], max_clip_duration=5)

        self.assertEqual(ranked[0].url, matching.url)

    def test_rank_materials_preserves_order_when_metadata_is_missing(self):
        first = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/first.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="city street",
        )
        second = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/second.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="city street",
        )

        ranked = material._rank_materials(
            [first, second],
            max_clip_duration=5,
        )

        self.assertEqual([item.url for item in ranked], [first.url, second.url])

    def test_rank_materials_does_not_reward_unrelated_metadata(self):
        no_metadata = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/no-metadata.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="city street",
        )
        unrelated_metadata = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/unrelated-metadata.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="city street",
            title="Forest waterfall",
            tags=["nature"],
        )

        ranked = material._rank_materials(
            [no_metadata, unrelated_metadata],
            max_clip_duration=5,
        )

        self.assertEqual(
            [item.url for item in ranked],
            [no_metadata.url, unrelated_metadata.url],
        )

    def test_rank_materials_does_not_treat_search_query_as_content(self):
        first = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/no-query.mp4",
            duration=5,
            width=1080,
            height=1920,
        )
        query_only = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/query-only.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="forest waterfall",
        )

        ranked = material._rank_materials(
            [first, query_only],
            max_clip_duration=5,
        )

        self.assertEqual([item.url for item in ranked], [first.url, query_only.url])

    def test_rank_materials_prefers_substantive_content_match_over_tiny_primary_score_lead(self):
        higher_primary_score = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/higher-primary-score.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="forest waterfall",
            title="Downtown traffic at night",
        )
        lower_primary_score_match = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/lower-primary-score-match.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="forest waterfall",
            title="Forest waterfall in daylight",
        )

        primary_scores = {
            higher_primary_score.url: 0.8000000001,
            lower_primary_score_match.url: 0.8,
        }
        with patch.object(
            material,
            "_score_material",
            side_effect=lambda item, _duration, *_args: primary_scores[item.url],
        ):
            ranked = material._rank_materials(
                [lower_primary_score_match, higher_primary_score],
                max_clip_duration=5,
            )

        self.assertEqual(ranked[0].url, lower_primary_score_match.url)

    def test_rank_materials_rejects_single_generic_token_match_for_visual_scene(self):
        generic_flag = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/turkey-flag.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="turkey inflation groceries",
            title="Turkish national flag over mountains",
        )
        substantive_match = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/grocery-prices.mp4",
            duration=5,
            width=1080,
            height=1920,
            search_query="turkey inflation groceries",
            title="Grocery prices in a market",
        )
        primary_scores = {
            generic_flag.url: 0.9,
            substantive_match.url: 0.8,
        }

        with patch.object(
            material,
            "_score_material",
            side_effect=lambda item, _duration, *_args: primary_scores[item.url],
        ):
            ranked = material._rank_materials(
                [generic_flag, substantive_match],
                max_clip_duration=5,
            )

        self.assertEqual(ranked[0].url, substantive_match.url)

    def test_download_selected_videos_uses_user_selected_order(self):
        selected_items = [
            material.MaterialInfo(
                provider="pexels",
                url="https://v.example/second-choice.mp4",
                duration=5,
            ),
            material.MaterialInfo(
                provider="pixabay",
                url="https://v.example/first-choice.mp4",
                duration=5,
            ),
        ]
        downloaded_urls = []

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            downloaded_urls.append(video_url)
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "save_video", side_effect=fake_save_video),
        ):
            result = material.download_selected_videos(
                task_id="selected-materials",
                selected_items=selected_items,
                audio_duration=8,
                max_clip_duration=5,
            )

        self.assertEqual(
            downloaded_urls,
            [
                "https://v.example/second-choice.mp4",
                "https://v.example/first-choice.mp4",
            ],
        )
        self.assertEqual(
            result,
            ["/tmp/second-choice.mp4", "/tmp/first-choice.mp4"],
        )

    def test_download_selected_videos_skips_url_variants(self):
        selected_items = [
            material.MaterialInfo(
                provider="pexels",
                url="https://v.example/shared.mp4?token=one",
                duration=5,
            ),
            material.MaterialInfo(
                provider="pixabay",
                url="https://v.example/shared.mp4?token=two#preview",
                duration=5,
            ),
            material.MaterialInfo(
                provider="coverr",
                url="https://v.example/next.mp4",
                duration=5,
            ),
        ]
        downloaded_urls = []

        def fake_save_video(video_url, save_dir="", minimum_duration=0.0):
            downloaded_urls.append(video_url)
            return f"/tmp/{urlparse(video_url).path.rsplit('/', 1)[-1]}"

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "save_video", side_effect=fake_save_video),
        ):
            result = material.download_selected_videos(
                task_id="selected-material-url-variants",
                selected_items=selected_items,
                audio_duration=8,
                max_clip_duration=5,
            )

        self.assertEqual(
            downloaded_urls,
            [
                "https://v.example/shared.mp4?token=one",
                "https://v.example/next.mp4",
            ],
        )
        self.assertEqual(result, ["/tmp/shared.mp4", "/tmp/next.mp4"])

    def test_download_logs_omit_signed_url_without_changing_request(self):
        signed_url = (
            "https://storage.coverr.co/videos/private/download.mp4?token=secret-token"
        )
        item = material.MaterialInfo(
            provider="coverr",
            url=signed_url,
            duration=5,
        )

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_coverr", return_value=[item]),
            patch.object(
                material, "save_video", return_value="/tmp/coverr-saved.mp4"
            ) as save_video,
            patch.object(material.logger, "info") as log_info,
        ):
            result = material.download_videos(
                task_id="signed-download-log",
                search_terms=["nature"],
                source="coverr",
                audio_duration=1,
                max_clip_duration=5,
            )

        self.assertEqual(result, ["/tmp/coverr-saved.mp4"])
        save_video.assert_called_once_with(
            video_url=signed_url,
            save_dir="",
            minimum_duration=0.0,
        )
        log_info.assert_any_call("downloading video [coverr]")
        messages = "\n".join(str(call.args[0]) for call in log_info.call_args_list)
        self.assertNotIn(signed_url, messages)
        self.assertNotIn("secret-token", messages)

    def test_download_error_log_omits_signed_url_from_item_and_exception(self):
        signed_url = (
            "https://storage.coverr.co/videos/private/download.mp4?token=secret-token"
        )
        item = material.MaterialInfo(
            provider="coverr",
            url=signed_url,
            duration=5,
        )

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(
                material,
                "save_video",
                side_effect=RuntimeError(f"download failed for {signed_url}"),
            ) as save_video,
            patch.object(material.logger, "info") as log_info,
            patch.object(material.logger, "error") as log_error,
        ):
            result = material.download_selected_videos(
                task_id="signed-download-error-log",
                selected_items=[item],
                audio_duration=1,
                max_clip_duration=5,
            )

        self.assertEqual(result, [])
        save_video.assert_called_once_with(
            video_url=signed_url,
            save_dir="",
            minimum_duration=0.0,
        )
        log_info.assert_any_call("downloading selected video [coverr]")
        log_error.assert_called_once_with(
            "failed to download selected video [coverr]: RuntimeError"
        )
        messages = "\n".join(
            str(call.args[0])
            for call in log_info.call_args_list + log_error.call_args_list
        )
        self.assertNotIn(signed_url, messages)
        self.assertNotIn("secret-token", messages)

    def test_download_error_log_keeps_safe_http_status(self):
        signed_url = (
            "https://storage.coverr.co/videos/private/download.mp4?token=secret-token"
        )
        item = material.MaterialInfo(
            provider="coverr",
            url=signed_url,
            duration=5,
        )
        error = requests.HTTPError(f"403 Client Error for url: {signed_url}")
        error.response = SimpleNamespace(status_code=403)

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "save_video", side_effect=error),
            patch.object(material.logger, "error") as log_error,
        ):
            result = material.download_selected_videos(
                task_id="safe-http-status-log",
                selected_items=[item],
                audio_duration=1,
                max_clip_duration=5,
            )

        self.assertEqual(result, [])
        log_error.assert_called_once_with(
            "failed to download selected video [coverr]: HTTPError status=403"
        )
        self.assertNotIn(signed_url, str(log_error.call_args))
        self.assertNotIn("secret-token", str(log_error.call_args))

    def test_safe_provider_search_logs_omit_query_and_exception_details(self):
        private_query = "private customer acquisition strategy"
        item = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/result.mp4",
            duration=5,
        )
        received_queries = []

        def successful_search(search_term, minimum_duration, video_aspect):
            received_queries.append(search_term)
            return [item]

        provider = SimpleNamespace(name="pexels", search=successful_search)
        with patch.object(material.logger, "info") as log_info:
            result = material._safe_provider_search(
                provider,
                private_query,
                minimum_duration=5,
                video_aspect=material.VideoAspect.portrait,
            )

        self.assertEqual(result, [item])
        self.assertEqual(received_queries, [private_query])
        log_info.assert_called_once_with("[pexels] search returned 1 results")
        self.assertNotIn(private_query, str(log_info.call_args))

        def failing_search(search_term, minimum_duration, video_aspect):
            raise RuntimeError(f"provider rejected {search_term}")

        provider.search = failing_search
        with patch.object(material.logger, "warning") as log_warning:
            result = material._safe_provider_search(
                provider,
                private_query,
                minimum_duration=5,
                video_aspect=material.VideoAspect.portrait,
            )

        self.assertEqual(result, [])
        log_warning.assert_called_once_with(
            "[pexels] search failed: RuntimeError"
        )
        self.assertNotIn(private_query, str(log_warning.call_args))

    def test_single_source_result_log_omits_query_text(self):
        private_query = "private executive transition plan"

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", return_value=[]),
            patch.object(material.logger, "info") as log_info,
        ):
            result = material.download_videos(
                task_id="private-query-log",
                search_terms=[private_query],
                source="pexels",
                audio_duration=1,
                max_clip_duration=5,
            )

        self.assertEqual(result, [])
        log_info.assert_any_call("found 0 videos")
        messages = "\n".join(str(call.args[0]) for call in log_info.call_args_list)
        self.assertNotIn(private_query, messages)


class TestMaterialAttributionFormatting(unittest.TestCase):
    def test_provider_record_is_kept_without_adding_a_credit_line(self):
        records = []
        material.append_material_attribution_record(
            records,
            MaterialInfo(
                provider="pexels",
                url="https://videos.example/clip.mp4",
            ),
            "C:/materials/clip.mp4",
        )

        self.assertEqual(
            records,
            [
                {
                    "video_path": "C:/materials/clip.mp4",
                    "provider": "pexels",
                    "title": "",
                    "license": "",
                    "license_url": "",
                    "attribution": "",
                    "source_url": "https://videos.example/clip.mp4",
                }
            ],
        )
        self.assertEqual(material.format_material_attributions(records), "")

    def test_append_material_attributions_adds_deduplicated_credits(self):
        text = material.append_material_attributions(
            "Original caption",
            [
                {
                    "provider": "wikimedia",
                    "title": "City clip",
                    "license": "CC BY-SA 4.0",
                    "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
                    "attribution": "City clip - Jane Doe - CC BY-SA 4.0",
                    "source_url": "https://commons.wikimedia.org/wiki/File:City.webm",
                },
                {
                    "provider": "wikimedia",
                    "title": "City clip",
                    "license": "CC BY-SA 4.0",
                    "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
                    "attribution": "City clip - Jane Doe - CC BY-SA 4.0",
                    "source_url": "https://commons.wikimedia.org/wiki/File:City.webm",
                },
            ],
        )

        self.assertIn("Original caption", text)
        self.assertIn("Credits:", text)
        self.assertIn("City clip - Jane Doe - CC BY-SA 4.0", text)
        self.assertEqual(text.count("City clip - Jane Doe"), 1)

    def test_append_material_attributions_keeps_caption_when_empty(self):
        self.assertEqual(
            material.append_material_attributions("Original caption", []),
            "Original caption",
        )


class TestMaterialSearchRandomization(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)
        config.app["video_cooldown_enabled"] = False
        config.app["video_cooldown_enabled"] = False

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    def _query_args(self, call):
        return parse_qs(urlparse(call.args[0]).query)

    def test_pexels_provider_sets_material_dimensions(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "duration": 8,
                        "image": "https://example.com/provider-preview.jpg",
                        "video_files": [
                            {
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/portrait.mp4",
                            }
                        ],
                    }
                ]
            }
        )

        with patch(
            "app.services.providers.pexels.requests.get",
            return_value=fake_response,
        ):
            results = PexelsProvider().search("cat", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/portrait.mp4")
        self.assertEqual(
            results[0].preview_url,
            "https://example.com/provider-preview.jpg",
        )
        self.assertEqual(results[0].width, 1080)
        self.assertEqual(results[0].height, 1920)

    def test_pexels_provider_accepts_larger_matching_aspect_file(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "duration": 8,
                        "video_files": [
                            {
                                "width": 2160,
                                "height": 3840,
                                "link": "https://example.com/portrait-4k.mp4",
                            }
                        ],
                    }
                ]
            }
        )

        with patch(
            "app.services.providers.pexels.requests.get",
            return_value=fake_response,
        ):
            results = PexelsProvider().search("cat", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/portrait-4k.mp4")
        self.assertEqual(results[0].width, 2160)
        self.assertEqual(results[0].height, 3840)

    def test_pexels_provider_skips_malformed_item_and_keeps_valid_results(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {},
                    {
                        "duration": 8,
                        "video_files": [
                            {
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/valid.mp4",
                            }
                        ],
                    },
                ]
            }
        )

        with patch(
            "app.services.providers.pexels.requests.get",
            return_value=fake_response,
        ):
            results = PexelsProvider().search("cat", minimum_duration=1)

        self.assertEqual(
            [item.url for item in results],
            ["https://example.com/valid.mp4"],
        )

    def test_pexels_provider_prefers_native_aspect_before_resolution(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "duration": 8,
                        "video_files": [
                            {
                                "width": 3840,
                                "height": 2160,
                                "link": "https://example.com/landscape-4k.mp4",
                            },
                            {
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/portrait-hd.mp4",
                            },
                        ],
                    }
                ]
            }
        )

        with patch(
            "app.services.providers.pexels.requests.get",
            return_value=fake_response,
        ):
            results = PexelsProvider().search("city", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/portrait-hd.mp4")
        self.assertEqual((results[0].width, results[0].height), (1080, 1920))

    def test_search_pexels_accepts_croppable_portrait_for_four_by_five(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "duration": 8,
                        "video_files": [
                            {
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/portrait.mp4",
                            }
                        ],
                    }
                ]
            }
        )

        with patch(
            "app.services.material.requests.get",
            return_value=fake_response,
        ) as get:
            results = material.search_videos_pexels(
                "cat",
                minimum_duration=1,
                video_aspect=VideoAspect.portrait_4_5,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/portrait.mp4")
        query = parse_qs(urlparse(get.call_args.args[0]).query)
        self.assertEqual(query["orientation"], ["portrait"])

    def test_pexels_provider_uses_portrait_search_for_4_5_aspect(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(json=lambda: {"videos": []})

        with patch(
            "app.services.providers.pexels.requests.get",
            return_value=fake_response,
        ) as get:
            PexelsProvider().search(
                "cat",
                minimum_duration=1,
                video_aspect=VideoAspect.portrait_4_5,
            )

        query = self._query_args(get.call_args)
        self.assertEqual(query["orientation"], ["portrait"])

    def test_pexels_provider_accepts_croppable_portrait_for_four_by_five(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "duration": 8,
                        "video_files": [
                            {
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/portrait.mp4",
                            }
                        ],
                    }
                ]
            }
        )

        with patch(
            "app.services.providers.pexels.requests.get",
            return_value=fake_response,
        ):
            results = PexelsProvider().search(
                "cat",
                minimum_duration=1,
                video_aspect=VideoAspect.portrait_4_5,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/portrait.mp4")

    def test_pixabay_provider_rejects_items_below_target_height(self):
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1920,
                                "height": 1080,
                                "url": "https://example.com/landscape.mp4",
                            }
                        },
                    },
                    {
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1080,
                                "height": 1920,
                                "url": "https://example.com/portrait.mp4",
                            }
                        },
                    },
                ]
            }
        )

        with patch(
            "app.services.providers.pixabay.requests.get",
            return_value=fake_response,
        ):
            results = PixabayProvider().search("cat", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/portrait.mp4")
        self.assertEqual(results[0].width, 1080)
        self.assertEqual(results[0].height, 1920)

    def test_pixabay_provider_skips_malformed_item_and_keeps_valid_results(self):
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {},
                    {
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1080,
                                "height": 1920,
                                "url": "https://example.com/valid.mp4",
                            }
                        },
                    },
                ]
            }
        )

        with patch(
            "app.services.providers.pixabay.requests.get",
            return_value=fake_response,
        ):
            results = PixabayProvider().search("cat", minimum_duration=1)

        self.assertEqual(
            [item.url for item in results],
            ["https://example.com/valid.mp4"],
        )

    def test_pixabay_provider_uses_best_matching_video_variant(self):
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1080,
                                "height": 1920,
                                "url": "https://example.com/portrait-hd.mp4",
                                "thumbnail": "https://example.com/portrait-hd.jpg",
                            },
                            "fullHD": {
                                "width": 2160,
                                "height": 3840,
                                "url": "https://example.com/portrait-4k.mp4",
                                "thumbnail": "https://example.com/portrait-4k.jpg",
                            },
                        },
                    }
                ]
            }
        )

        with patch(
            "app.services.providers.pixabay.requests.get",
            return_value=fake_response,
        ):
            results = PixabayProvider().search("cat", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/portrait-4k.mp4")
        self.assertEqual(results[0].preview_url, "https://example.com/portrait-4k.jpg")
        self.assertEqual(results[0].width, 2160)
        self.assertEqual(results[0].height, 3840)

    def test_pixabay_provider_prefers_native_aspect_before_resolution(self):
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 3840,
                                "height": 2160,
                                "url": "https://example.com/landscape-4k.mp4",
                            },
                            "medium": {
                                "width": 1080,
                                "height": 1920,
                                "url": "https://example.com/portrait-hd.mp4",
                            },
                        },
                    }
                ]
            }
        )

        with patch(
            "app.services.providers.pixabay.requests.get",
            return_value=fake_response,
        ):
            results = PixabayProvider().search("city", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/portrait-hd.mp4")
        self.assertEqual((results[0].width, results[0].height), (1080, 1920))

    def test_pixabay_provider_sets_search_query_and_tags(self):
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "duration": 8,
                        "tags": " city, skyline, sunrise ",
                        "videos": {
                            "large": {
                                "width": 1080,
                                "height": 1920,
                                "url": "https://example.com/portrait.mp4",
                            }
                        },
                    }
                ]
            }
        )

        with patch(
            "app.services.providers.pixabay.requests.get",
            return_value=fake_response,
        ):
            results = PixabayProvider().search(
                "modern city skyline",
                minimum_duration=1,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].search_query, "modern city skyline")
        self.assertEqual(results[0].tags, ["city", "skyline", "sunrise"])
        self.assertEqual(results[0].title, "")
        self.assertEqual(results[0].description, "")

    def test_pexels_provider_sets_search_query(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "duration": 8,
                        "video_files": [
                            {
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/portrait.mp4",
                            }
                        ],
                    }
                ]
            }
        )

        with patch(
            "app.services.providers.pexels.requests.get",
            return_value=fake_response,
        ):
            results = PexelsProvider().search(
                "focused office teamwork",
                minimum_duration=1,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].search_query, "focused office teamwork")

    def test_coverr_provider_sets_search_and_content_metadata(self):
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "duration": 8,
                        "title": "Focused office teamwork",
                        "description": "A small team collaborates around a desk.",
                        "tags": ["office", " teamwork ", "", 42],
                        "thumbnail": "https://example.com/office.jpg",
                        "urls": {
                            "mp4_download": "https://example.com/office.mp4",
                        },
                    }
                ]
            }
        )

        with patch(
            "app.services.providers.coverr.requests.get",
            return_value=fake_response,
        ):
            results = CoverrProvider().search(
                "focused office teamwork",
                minimum_duration=1,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].search_query, "focused office teamwork")
        self.assertEqual(results[0].title, "Focused office teamwork")
        self.assertEqual(
            results[0].description,
            "A small team collaborates around a desk.",
        )
        self.assertEqual(results[0].tags, ["office", "teamwork"])
        self.assertEqual(results[0].preview_url, "https://example.com/office.jpg")

    def test_nasa_provider_sets_search_and_content_metadata(self):
        config.proxy.clear()
        search_response = SimpleNamespace(
            json=lambda: {
                "collection": {
                    "items": [
                        {
                            "href": "https://example.com/assets.json",
                            "data": [
                                {
                                    "title": "Moon landing preparation",
                                    "description": "Astronauts prepare for a lunar mission.",
                                    "keywords": [" moon ", "astronauts", "", 42],
                                }
                            ],
                        }
                    ]
                }
            }
        )
        assets_response = SimpleNamespace(
            json=lambda: ["https://example.com/lunar-mission~orig.mp4"]
        )

        with patch(
            "app.services.providers.nasa.requests.get",
            side_effect=[search_response, assets_response],
        ):
            results = NASAProvider().search(
                "astronaut lunar mission",
                minimum_duration=1,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].search_query, "astronaut lunar mission")
        self.assertEqual(results[0].title, "Moon landing preparation")
        self.assertEqual(
            results[0].description,
            "Astronauts prepare for a lunar mission.",
        )
        self.assertEqual(results[0].tags, ["moon", "astronauts"])

    def test_wikimedia_provider_sets_search_query_and_title(self):
        config.proxy.clear()
        search_response = SimpleNamespace(
            json=lambda: {
                "query": {
                    "search": [{"title": "File:City skyline.webm"}],
                }
            }
        )
        info_response = SimpleNamespace(
            json=lambda: {
                "query": {
                    "pages": {
                        "1": {
                            "title": "File:City skyline.webm",
                            "videoinfo": [
                                {
                                    "mediatype": "VIDEO",
                                    "duration": 8,
                                    "width": 1920,
                                    "height": 1080,
                                    "mime": "video/webm",
                                    "url": "https://example.com/city.webm",
                                    "extmetadata": {
                                        "Artist": {"value": "City Camera Crew"},
                                        "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                        "LicenseUrl": {
                                            "value": "https://creativecommons.org/licenses/by-sa/4.0/"
                                        },
                                    },
                                }
                            ],
                        }
                    }
                }
            }
        )

        with patch(
            "app.services.providers.wikimedia.requests.get",
            side_effect=[search_response, info_response],
        ):
            results = WikimediaProvider().search(
                "modern city skyline",
                minimum_duration=1,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].search_query, "modern city skyline")
        self.assertEqual(results[0].title, "File:City skyline.webm")
        self.assertEqual(results[0].width, 1920)
        self.assertEqual(results[0].height, 1080)
        self.assertEqual(results[0].license, "CC BY-SA 4.0")
        self.assertEqual(
            results[0].license_url,
            "https://creativecommons.org/licenses/by-sa/4.0/",
        )
        self.assertEqual(
            results[0].attribution,
            "File:City skyline.webm - City Camera Crew - CC BY-SA 4.0",
        )

    def test_wikimedia_provider_skips_search_results_without_titles(self):
        config.proxy.clear()
        search_response = SimpleNamespace(
            json=lambda: {
                "query": {
                    "search": [{}, {"title": "File:City.webm"}],
                }
            }
        )
        info_response = SimpleNamespace(
            json=lambda: {
                "query": {
                    "pages": {
                        "1": {
                            "title": "File:City.webm",
                            "videoinfo": [
                                {
                                    "mediatype": "VIDEO",
                                    "duration": 8,
                                    "mime": "video/webm",
                                    "url": "https://example.com/city.webm",
                                }
                            ],
                        }
                    }
                }
            }
        )

        with patch(
            "app.services.providers.wikimedia.requests.get",
            side_effect=[search_response, info_response],
        ):
            results = WikimediaProvider().search("city", minimum_duration=1)

        self.assertEqual(
            [item.url for item in results],
            ["https://example.com/city.webm"],
        )

    def test_wikimedia_provider_skips_malformed_videoinfo_pages(self):
        config.proxy.clear()
        search_response = SimpleNamespace(
            json=lambda: {
                "query": {"search": [{"title": "File:City.webm"}]}
            }
        )
        info_response = SimpleNamespace(
            json=lambda: {
                "query": {
                    "pages": {
                        "1": "bad-page",
                        "2": {"videoinfo": ["bad-video-info"]},
                        "3": {
                            "title": "File:City.webm",
                            "videoinfo": [
                                {
                                    "mediatype": "VIDEO",
                                    "duration": 8,
                                    "mime": "video/webm",
                                    "url": "https://example.com/city.webm",
                                }
                            ],
                        },
                    }
                }
            }
        )

        with patch(
            "app.services.providers.wikimedia.requests.get",
            side_effect=[search_response, info_response],
        ):
            results = WikimediaProvider().search("city", minimum_duration=1)

        self.assertEqual(
            [item.url for item in results],
            ["https://example.com/city.webm"],
        )

    def test_wikimedia_provider_prefers_selected_derivative_dimensions(self):
        config.proxy.clear()
        search_response = SimpleNamespace(
            json=lambda: {"query": {"search": [{"title": "File:City.mp4"}]}}
        )
        info_response = SimpleNamespace(
            json=lambda: {
                "query": {
                    "pages": {
                        "1": {
                            "title": "File:City.mp4",
                            "videoinfo": [
                                {
                                    "mediatype": "VIDEO",
                                    "duration": 8,
                                    "width": 3840,
                                    "height": 2160,
                                    "derivatives": [
                                        {
                                            "type": "video/mp4",
                                            "src": "https://example.com/city-portrait.mp4",
                                            "width": 1080,
                                            "height": 1920,
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                }
            }
        )

        with patch(
            "app.services.providers.wikimedia.requests.get",
            side_effect=[search_response, info_response],
        ):
            results = WikimediaProvider().search("city", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/city-portrait.mp4")
        self.assertEqual(results[0].width, 1080)
        self.assertEqual(results[0].height, 1920)

    def test_wikimedia_provider_prefers_best_aspect_matching_mp4_derivative(self):
        config.proxy.clear()
        search_response = SimpleNamespace(
            json=lambda: {"query": {"search": [{"title": "File:City.mp4"}]}}
        )
        info_response = SimpleNamespace(
            json=lambda: {
                "query": {
                    "pages": {
                        "1": {
                            "title": "File:City.mp4",
                            "videoinfo": [
                                {
                                    "mediatype": "VIDEO",
                                    "duration": 8,
                                    "derivatives": [
                                        {
                                            "type": "video/mp4",
                                            "src": "https://example.com/city-landscape-4k.mp4",
                                            "width": 3840,
                                            "height": 2160,
                                        },
                                        {
                                            "type": "video/mp4",
                                            "src": "https://example.com/city-portrait-hd.mp4",
                                            "width": 1080,
                                            "height": 1920,
                                        },
                                        {
                                            "type": "video/mp4",
                                            "src": "https://example.com/city-portrait-sd.mp4",
                                            "width": 720,
                                            "height": 1280,
                                        },
                                    ],
                                }
                            ],
                        }
                    }
                }
            }
        )

        with patch(
            "app.services.providers.wikimedia.requests.get",
            side_effect=[search_response, info_response],
        ):
            results = WikimediaProvider().search(
                "city",
                minimum_duration=1,
                video_aspect=VideoAspect.portrait,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/city-portrait-hd.mp4")
        self.assertEqual((results[0].width, results[0].height), (1080, 1920))

    def test_pexels_provider_uses_detail_page_slug_as_content_title(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "duration": 8,
                        "url": (
                            "https://www.pexels.com/video/"
                            "a-person-holding-a-turkish-lira-banknote-1234567/"
                        ),
                        "video_files": [
                            {
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/portrait.mp4",
                            }
                        ],
                    }
                ]
            }
        )

        with patch(
            "app.services.providers.pexels.requests.get",
            return_value=fake_response,
        ):
            results = PexelsProvider().search("turkish lira", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "a person holding a turkish lira banknote")

    def test_pexels_page_title_rejects_urls_without_descriptive_slug(self):
        self.assertEqual(
            _pexels_page_title("https://www.pexels.com/video/1234567/"),
            "",
        )
        self.assertEqual(
            _pexels_page_title("https://www.pexels.com/photo/city-street-1234567/"),
            "",
        )

    def test_rank_materials_prefers_pexels_detail_page_title_matching_query(self):
        matching_item = MaterialInfo(
            provider="pexels",
            url="https://example.com/matching.mp4",
            duration=8,
            width=1080,
            height=1920,
            search_query="turkish lira",
            title="a person holding a turkish lira banknote",
        )
        unrelated_item = MaterialInfo(
            provider="pexels",
            url="https://example.com/unrelated.mp4",
            duration=8,
            width=1080,
            height=1920,
            search_query="turkish lira",
            title="a surfer riding a wave at sunset",
        )

        with patch.dict(
            config.app,
            {
                "twelvelabs_material_rerank_enabled": False,
                "twelvelabs_visual_rerank_enabled": False,
                "twelvelabs_clip_qa_enabled": False,
            },
            clear=False,
        ):
            ranked_items = material._rank_materials(
                [unrelated_item, matching_item],
                max_clip_duration=5,
                video_aspect=VideoAspect.portrait,
            )

        self.assertEqual(ranked_items[0].url, matching_item.url)

    def test_wikimedia_provider_uses_source_dimensions_for_incomplete_derivative_metadata(self):
        config.proxy.clear()
        search_response = SimpleNamespace(
            json=lambda: {"query": {"search": [{"title": "File:City.mp4"}]}}
        )
        info_response = SimpleNamespace(
            json=lambda: {
                "query": {
                    "pages": {
                        "1": {
                            "title": "File:City.mp4",
                            "videoinfo": [
                                {
                                    "mediatype": "VIDEO",
                                    "duration": 8,
                                    "width": 3840,
                                    "height": 2160,
                                    "derivatives": [
                                        {
                                            "type": "video/mp4",
                                            "src": "https://example.com/city.mp4",
                                            "width": 1080,
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                }
            }
        )

        with patch(
            "app.services.providers.wikimedia.requests.get",
            side_effect=[search_response, info_response],
        ):
            results = WikimediaProvider().search("city", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].width, 3840)
        self.assertEqual(results[0].height, 2160)

    def test_archive_provider_sets_search_query_and_title(self):
        config.proxy.clear()
        search_response = SimpleNamespace(
            json=lambda: {
                "response": {
                    "docs": [
                        {
                            "identifier": "city-film",
                            "title": "Historic city film",
                        }
                    ]
                }
            }
        )
        metadata_response = SimpleNamespace(
            json=lambda: {
                "metadata": {
                    "title": "Restored historic city film",
                    "description": "Archival footage of a busy city street.",
                    "subject": ["city", " archive ", "", 42],
                    "licenseurl": (
                        "https://creativecommons.org/publicdomain/zero/1.0/"
                    ),
                },
                "files": [
                    {
                        "format": "MPEG4",
                        "name": "city.mp4",
                        "size": "1000",
                        "length": "8",
                        "width": "1920",
                        "height": "1080",
                        "source": "original",
                    }
                ]
            }
        )

        with patch(
            "app.services.providers.archive_org.requests.get",
            side_effect=[search_response, metadata_response],
        ):
            results = ArchiveOrgProvider().search(
                "historic city street",
                minimum_duration=1,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].search_query, "historic city street")
        self.assertEqual(results[0].title, "Restored historic city film")
        self.assertEqual(
            results[0].description,
            "Archival footage of a busy city street.",
        )
        self.assertEqual(results[0].tags, ["city", "archive"])
        self.assertEqual(results[0].width, 1920)
        self.assertEqual(results[0].height, 1080)

    def test_archive_provider_preserves_open_license_metadata(self):
        config.proxy.clear()
        search_response = SimpleNamespace(
            json=lambda: {"response": {"docs": [{"identifier": "open-film"}]}}
        )
        metadata_response = SimpleNamespace(
            json=lambda: {
                "metadata": {
                    "title": "Open city film",
                    "creator": "Archive Creator",
                    "licenseurl": "https://creativecommons.org/licenses/by/4.0/",
                    "license": "Creative Commons Attribution 4.0",
                },
                "files": [
                    {
                        "format": "MPEG4",
                        "name": "open-city.mp4",
                        "size": "1000",
                        "length": "8",
                    }
                ],
            }
        )

        with patch(
            "app.services.providers.archive_org.requests.get",
            side_effect=[search_response, metadata_response],
        ):
            results = ArchiveOrgProvider().search("city", minimum_duration=1)

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.license, "Creative Commons Attribution 4.0")
        self.assertEqual(
            item.license_url,
            "https://creativecommons.org/licenses/by/4.0/",
        )
        self.assertIn("Archive Creator", item.attribution)
        self.assertIn("archive.org/details/open-film", item.attribution)

    def test_archive_provider_skips_restricted_license_metadata(self):
        config.proxy.clear()
        search_response = SimpleNamespace(
            json=lambda: {"response": {"docs": [{"identifier": "restricted-film"}]}}
        )
        metadata_response = SimpleNamespace(
            json=lambda: {
                "metadata": {
                    "title": "Restricted city film",
                    "rights": "All rights reserved",
                },
                "files": [
                    {
                        "format": "MPEG4",
                        "name": "restricted-city.mp4",
                        "size": "1000",
                        "length": "8",
                    }
                ],
            }
        )

        with patch(
            "app.services.providers.archive_org.requests.get",
            side_effect=[search_response, metadata_response],
        ):
            results = ArchiveOrgProvider().search("city", minimum_duration=1)

        self.assertEqual(results, [])

    def test_archive_provider_prefers_native_aspect_before_file_size(self):
        config.proxy.clear()
        search_response = SimpleNamespace(
            json=lambda: {"response": {"docs": [{"identifier": "open-film"}]}}
        )
        metadata_response = SimpleNamespace(
            json=lambda: {
                "metadata": {
                    "title": "Open city film",
                    "licenseurl": "https://creativecommons.org/publicdomain/zero/1.0/",
                },
                "files": [
                    {
                        "format": "MPEG4",
                        "name": "landscape-4k.mp4",
                        "size": "9000000",
                        "length": "8",
                        "width": "3840",
                        "height": "2160",
                        "source": "original",
                    },
                    {
                        "format": "MPEG4",
                        "name": "portrait-hd.mp4",
                        "size": "4000000",
                        "length": "8",
                        "width": "1080",
                        "height": "1920",
                        "source": "original",
                    },
                ],
            }
        )

        with patch(
            "app.services.providers.archive_org.requests.get",
            side_effect=[search_response, metadata_response],
        ):
            results = ArchiveOrgProvider().search(
                "city",
                minimum_duration=1,
                video_aspect=VideoAspect.portrait,
            )

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].url.endswith("/portrait-hd.mp4"))
        self.assertEqual(results[0].width, 1080)
        self.assertEqual(results[0].height, 1920)

    def test_dvids_provider_selects_native_aspect_and_preserves_attribution(self):
        config.app["dvids_api_keys"] = ["free-dvids-key"]
        config.proxy.clear()
        search_response = SimpleNamespace(
            json=lambda: {
                "results": [
                    {
                        "id": "video:123",
                        "type": "video",
                        "duration": 12,
                        "title": "City logistics training",
                    }
                ]
            }
        )
        asset_response = SimpleNamespace(
            json=lambda: {
                "results": {
                    "id": "video:123",
                    "type": "video",
                    "title": "City logistics training",
                    "description": "A logistics team works in a city environment.",
                    "keywords": "logistics, city, teamwork",
                    "duration": 12,
                    "url": "https://www.dvidshub.net/video/123/city-logistics",
                    "image": "https://example.com/city-frame.jpg",
                    "credit": [{"name": "Jordan Example"}],
                    "files": [
                        {
                            "type": "video/mp4",
                            "src": "https://example.com/city-landscape.mp4",
                            "width": 1920,
                            "height": 1080,
                        },
                        {
                            "type": "video/mp4",
                            "src": "https://example.com/city-portrait.mp4",
                            "width": 1080,
                            "height": 1920,
                        },
                    ],
                }
            }
        )

        with patch(
            "app.services.providers.dvids.requests.get",
            side_effect=[search_response, asset_response],
        ) as get:
            results = DVIDSProvider().search(
                "city logistics",
                minimum_duration=5,
                video_aspect=VideoAspect.portrait,
            )

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.provider, "dvids")
        self.assertEqual(item.url, "https://example.com/city-portrait.mp4")
        self.assertEqual((item.width, item.height), (1080, 1920))
        self.assertEqual(item.search_query, "city logistics")
        self.assertEqual(item.description, "A logistics team works in a city environment.")
        self.assertEqual(item.tags, ["logistics", "city", "teamwork"])
        self.assertEqual(item.preview_url, "https://example.com/city-frame.jpg")
        self.assertIn("Jordan Example", item.attribution)
        self.assertIn("public domain", item.license.lower())
        self.assertEqual(get.call_args_list[0].kwargs["params"]["type"], "video")
        self.assertEqual(get.call_args_list[0].kwargs["params"]["hd"], 1)

    def test_search_page_defaults_to_first_page_without_random(self):
        config.app.pop("material_search_max_page", None)
        config.app.pop("pexels_search_max_page", None)

        with patch("app.services.providers.utils.random.randint") as randint:
            page = provider_utils.get_search_page("pexels")

        self.assertEqual(page, 1)
        randint.assert_not_called()

    def test_search_page_clamps_max_page(self):
        config.app["material_search_max_page"] = 999

        with patch("app.services.providers.utils.random.randint", return_value=50) as randint:
            page = provider_utils.get_search_page("pexels")

        self.assertEqual(page, 50)
        randint.assert_called_once_with(1, provider_utils.MAX_RANDOM_SEARCH_PAGE)

    def test_search_page_uses_source_specific_override(self):
        config.app["material_search_max_page"] = 9
        config.app["pexels_search_max_page"] = 2

        with patch("app.services.providers.utils.random.randint", return_value=2) as randint:
            page = provider_utils.get_search_page("pexels")

        self.assertEqual(page, 2)
        randint.assert_called_once_with(1, 2)

    def test_search_page_invalid_value_falls_back_to_first_page(self):
        config.app["pexels_search_max_page"] = "not-a-number"

        with patch("app.services.providers.utils.random.randint") as randint:
            page = provider_utils.get_search_page("pexels")

        self.assertEqual(page, 1)
        randint.assert_not_called()

    def test_select_best_video_variant_prefers_native_aspect_before_resolution(self):
        variants = [
            {
                "link": "https://example.com/landscape-4k.mp4",
                "width": 3840,
                "height": 2160,
            },
            {
                "link": "https://example.com/portrait-hd.mp4",
                "width": 1080,
                "height": 1920,
            },
        ]

        selected = provider_utils.select_best_video_variant(
            variants,
            video_aspect=VideoAspect.portrait,
            url_key="link",
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected[0]["link"], "https://example.com/portrait-hd.mp4")
        self.assertEqual(selected[1:], (1080, 1920))

    def test_provider_searches_send_configured_random_page(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app["material_search_max_page"] = 5
        config.proxy.clear()

        fake_response = SimpleNamespace(json=lambda: {"videos": [], "hits": []})

        with (
            patch("app.services.providers.utils.random.randint", return_value=3),
            patch(
                "app.services.providers.pexels.requests.get",
                return_value=fake_response,
            ) as get,
        ):
            PexelsProvider().search("cat", minimum_duration=1)
            PixabayProvider().search("cat", minimum_duration=1)
            CoverrProvider().search("cat", minimum_duration=1)

        pexels_query = self._query_args(get.call_args_list[0])
        pixabay_query = self._query_args(get.call_args_list[1])
        coverr_query = self._query_args(get.call_args_list[2])

        self.assertEqual(pexels_query["page"], ["3"])
        self.assertEqual(pexels_query["per_page"], ["20"])
        self.assertEqual(pixabay_query["page"], ["3"])
        self.assertEqual(pixabay_query["per_page"], ["50"])
        self.assertEqual(coverr_query["page"], ["3"])
        self.assertEqual(coverr_query["page_size"], ["20"])
        self.assertEqual(coverr_query["sort"], ["popular"])

    def test_pixabay_provider_search_log_omits_query_and_api_key(self):
        config.app["pixabay_api_keys"] = ["private-pixabay-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(json=lambda: {"hits": []})

        with (
            patch(
                "app.services.providers.pixabay.requests.get",
                return_value=fake_response,
            ) as get,
            patch("app.services.providers.pixabay.logger.info") as log_info,
        ):
            PixabayProvider().search("private scene query", minimum_duration=1)

        request_query = self._query_args(get.call_args)
        self.assertEqual(request_query["q"], ["private scene query"])
        self.assertEqual(request_query["key"], ["private-pixabay-key"])
        log_info.assert_called_once_with("[pixabay] searching videos")

    def test_provider_search_logs_omit_query_text(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(json=lambda: {"videos": [], "hits": []})
        cases = (
            (
                PexelsProvider(),
                "pexels",
                "app.services.providers.pexels.requests.get",
                "app.services.providers.pexels.logger.info",
            ),
            (
                CoverrProvider(),
                "coverr",
                "app.services.providers.coverr.requests.get",
                "app.services.providers.coverr.logger.info",
            ),
        )

        for provider, provider_name, request_path, log_path in cases:
            with self.subTest(provider=provider_name):
                with (
                    patch(request_path, return_value=fake_response),
                    patch(log_path) as log_info,
                ):
                    provider.search("private scene query", minimum_duration=1)

                log_info.assert_called_once_with(
                    f"[{provider_name}] searching videos"
                )

    def test_legacy_searches_send_configured_random_page(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app["material_search_max_page"] = 5
        config.proxy.clear()

        fake_response = SimpleNamespace(json=lambda: {"videos": [], "hits": []})

        with (
            patch("app.services.providers.utils.random.randint", return_value=4),
            patch(
                "app.services.material.requests.get",
                return_value=fake_response,
            ) as get,
        ):
            material.search_videos_pexels("cat", minimum_duration=1)
            material.search_videos_pixabay("cat", minimum_duration=1)
            material.search_videos_coverr("cat", minimum_duration=1)

        pexels_query = self._query_args(get.call_args_list[0])
        pixabay_query = self._query_args(get.call_args_list[1])
        coverr_query = self._query_args(get.call_args_list[2])

        self.assertEqual(pexels_query["page"], ["4"])
        self.assertEqual(pexels_query["per_page"], ["20"])
        self.assertEqual(pixabay_query["page"], ["4"])
        self.assertEqual(pixabay_query["per_page"], ["50"])
        self.assertEqual(coverr_query["page"], ["4"])
        self.assertEqual(coverr_query["page_size"], ["20"])
        self.assertEqual(coverr_query["sort"], ["popular"])

    def test_legacy_pixabay_search_log_omits_query_and_api_key(self):
        config.app["pixabay_api_keys"] = ["private-pixabay-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(json=lambda: {"hits": []})

        with (
            patch(
                "app.services.material.requests.get",
                return_value=fake_response,
            ) as get,
            patch.object(material.logger, "info") as log_info,
        ):
            material.search_videos_pixabay(
                "private scene query",
                minimum_duration=1,
            )

        request_query = self._query_args(get.call_args)
        self.assertEqual(request_query["q"], ["private scene query"])
        self.assertEqual(request_query["key"], ["private-pixabay-key"])
        log_info.assert_called_once_with("[pixabay] searching videos")

    def test_legacy_search_logs_omit_query_text(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(json=lambda: {"videos": [], "hits": []})
        cases = (
            (material.search_videos_pexels, "pexels"),
            (material.search_videos_coverr, "coverr"),
        )

        for search_videos, provider_name in cases:
            with self.subTest(provider=provider_name):
                with (
                    patch(
                        "app.services.material.requests.get",
                        return_value=fake_response,
                    ),
                    patch.object(material.logger, "info") as log_info,
                ):
                    search_videos("private scene query", minimum_duration=1)

                log_info.assert_called_once_with(
                    f"[{provider_name}] searching videos"
                )

    def test_mainstream_search_error_logs_redact_exception_urls(self):
        config.app["pexels_api_keys"] = ["private-pexels-key"]
        config.app["pixabay_api_keys"] = ["private-pixabay-key"]
        config.app["coverr_api_keys"] = ["private-coverr-key"]
        config.proxy.clear()
        private_query = "private acquisition strategy"
        provider_cases = (
            (
                PexelsProvider(),
                "pexels",
                "app.services.providers.pexels.requests.get",
                "app.services.providers.pexels.logger.error",
            ),
            (
                PixabayProvider(),
                "pixabay",
                "app.services.providers.pixabay.requests.get",
                "app.services.providers.pixabay.logger.error",
            ),
            (
                CoverrProvider(),
                "coverr",
                "app.services.providers.coverr.requests.get",
                "app.services.providers.coverr.logger.error",
            ),
        )

        for provider, provider_name, request_path, log_path in provider_cases:
            with self.subTest(layer="provider", provider=provider_name):
                error = requests.HTTPError(
                    "429 Client Error for url: "
                    f"https://example.test/search?q={private_query}"
                    f"&key=private-{provider_name}-key"
                )
                error.response = SimpleNamespace(status_code=429)
                with (
                    patch(request_path, side_effect=error),
                    patch(log_path) as log_error,
                ):
                    result = provider.search(private_query, minimum_duration=1)

                self.assertEqual(result, [])
                log_error.assert_called_once_with(
                    f"[{provider_name}] search failed: HTTPError status=429"
                )
                self.assertNotIn(private_query, str(log_error.call_args))
                self.assertNotIn("private-", str(log_error.call_args))

        legacy_cases = (
            (material.search_videos_pexels, "pexels"),
            (material.search_videos_pixabay, "pixabay"),
            (material.search_videos_coverr, "coverr"),
        )
        for search_videos, provider_name in legacy_cases:
            with self.subTest(layer="legacy", provider=provider_name):
                error = requests.HTTPError(
                    "429 Client Error for url: "
                    f"https://example.test/search?q={private_query}"
                    f"&key=private-{provider_name}-key"
                )
                error.response = SimpleNamespace(status_code=429)
                with (
                    patch("app.services.material.requests.get", side_effect=error),
                    patch.object(material.logger, "error") as log_error,
                ):
                    result = search_videos(private_query, minimum_duration=1)

                self.assertEqual(result, [])
                log_error.assert_called_once_with(
                    f"[{provider_name}] search failed: HTTPError status=429"
                )
                self.assertNotIn(private_query, str(log_error.call_args))
                self.assertNotIn("private-", str(log_error.call_args))

    def test_mainstream_search_logs_omit_unexpected_response_bodies(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.proxy.clear()
        sensitive_response = {
            "error": "private upstream detail",
            "request_url": "https://example.test/search?key=secret-response-key",
        }
        fake_response = SimpleNamespace(json=lambda: sensitive_response)
        provider_cases = (
            (
                PexelsProvider(),
                "pexels",
                "app.services.providers.pexels.requests.get",
                "app.services.providers.pexels.logger.error",
            ),
            (
                PixabayProvider(),
                "pixabay",
                "app.services.providers.pixabay.requests.get",
                "app.services.providers.pixabay.logger.error",
            ),
            (
                CoverrProvider(),
                "coverr",
                "app.services.providers.coverr.requests.get",
                "app.services.providers.coverr.logger.error",
            ),
        )

        for provider, provider_name, request_path, log_path in provider_cases:
            with self.subTest(layer="provider", provider=provider_name):
                with (
                    patch(request_path, return_value=fake_response),
                    patch(log_path) as log_error,
                ):
                    result = provider.search("private query", minimum_duration=1)

                self.assertEqual(result, [])
                log_error.assert_called_once_with(
                    f"[{provider_name}] search returned unexpected response"
                )
                self.assertNotIn("secret-response-key", str(log_error.call_args))

    def test_legacy_search_logs_omit_unexpected_response_bodies(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.proxy.clear()
        sensitive_response = {
            "error": "private upstream detail",
            "request_url": "https://example.test/search?key=secret-response-key",
        }
        fake_response = SimpleNamespace(json=lambda: sensitive_response)
        legacy_cases = (
            (material.search_videos_pexels, "pexels"),
            (material.search_videos_pixabay, "pixabay"),
            (material.search_videos_coverr, "coverr"),
        )

        for search_videos, provider_name in legacy_cases:
            with self.subTest(provider=provider_name):
                with (
                    patch(
                        "app.services.material.requests.get",
                        return_value=fake_response,
                    ),
                    patch.object(material.logger, "error") as log_error,
                ):
                    result = search_videos("private query", minimum_duration=1)

                self.assertEqual(result, [])
                log_error.assert_called_once_with(
                    f"[{provider_name}] search returned unexpected response"
                )
                self.assertNotIn("secret-response-key", str(log_error.call_args))

    def test_secondary_provider_search_logs_omit_query_text(self):
        config.proxy.clear()
        private_query = "private executive transition plan"
        cases = (
            (
                NASAProvider(),
                "nasa",
                "app.services.providers.nasa.requests.get",
                "app.services.providers.nasa.logger.info",
                {"collection": {"items": []}},
                "q",
            ),
            (
                WikimediaProvider(),
                "wikimedia",
                "app.services.providers.wikimedia.requests.get",
                "app.services.providers.wikimedia.logger.info",
                {"query": {"search": []}},
                "srsearch",
            ),
            (
                ArchiveOrgProvider(),
                "archive_org",
                "app.services.providers.archive_org.requests.get",
                "app.services.providers.archive_org.logger.info",
                {"response": {"docs": []}},
                "q",
            ),
        )

        for provider, name, request_path, log_path, payload, query_key in cases:
            with self.subTest(provider=name):
                fake_response = SimpleNamespace(json=lambda: payload)
                with (
                    patch(request_path, return_value=fake_response) as get,
                    patch(log_path) as log_info,
                ):
                    result = provider.search(private_query, minimum_duration=1)

                self.assertEqual(result, [])
                request_value = get.call_args.kwargs["params"][query_key]
                self.assertIn(private_query, request_value)
                log_info.assert_any_call(f"[{name}] searching videos")
                log_info.assert_any_call(f"[{name}] search returned 0 videos")
                self.assertNotIn(private_query, str(log_info.call_args_list))

    def test_secondary_provider_search_errors_redact_exception_urls(self):
        config.proxy.clear()
        private_query = "private restructuring plan"
        cases = (
            (
                NASAProvider(),
                "nasa",
                "app.services.providers.nasa.requests.get",
                "app.services.providers.nasa.logger.error",
            ),
            (
                WikimediaProvider(),
                "wikimedia",
                "app.services.providers.wikimedia.requests.get",
                "app.services.providers.wikimedia.logger.error",
            ),
            (
                ArchiveOrgProvider(),
                "archive_org",
                "app.services.providers.archive_org.requests.get",
                "app.services.providers.archive_org.logger.error",
            ),
        )

        for provider, name, request_path, log_path in cases:
            with self.subTest(provider=name):
                error = requests.HTTPError(
                    "503 Server Error for url: "
                    f"https://example.test/search?q={private_query}&token=secret"
                )
                error.response = SimpleNamespace(status_code=503)
                with (
                    patch(request_path, side_effect=error),
                    patch(log_path) as log_error,
                ):
                    result = provider.search(private_query, minimum_duration=1)

                self.assertEqual(result, [])
                log_error.assert_called_once_with(
                    f"[{name}] search failed: HTTPError status=503"
                )
                self.assertNotIn(private_query, str(log_error.call_args))
                self.assertNotIn("token=secret", str(log_error.call_args))

    def test_provider_http_status_is_checked_before_json_parsing(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.proxy.clear()

        def fail_json():
            raise AssertionError("JSON body must not be parsed after HTTP 401")

        unauthorized = SimpleNamespace(
            status_code=401,
            url="https://example.test/search?token=secret",
            json=fail_json,
        )
        provider_cases = (
            (
                PexelsProvider(),
                "pexels",
                "app.services.providers.pexels.requests.get",
                "app.services.providers.pexels.logger.error",
            ),
            (
                PixabayProvider(),
                "pixabay",
                "app.services.providers.pixabay.requests.get",
                "app.services.providers.pixabay.logger.error",
            ),
            (
                CoverrProvider(),
                "coverr",
                "app.services.providers.coverr.requests.get",
                "app.services.providers.coverr.logger.error",
            ),
            (
                NASAProvider(),
                "nasa",
                "app.services.providers.nasa.requests.get",
                "app.services.providers.nasa.logger.error",
            ),
            (
                WikimediaProvider(),
                "wikimedia",
                "app.services.providers.wikimedia.requests.get",
                "app.services.providers.wikimedia.logger.error",
            ),
            (
                ArchiveOrgProvider(),
                "archive_org",
                "app.services.providers.archive_org.requests.get",
                "app.services.providers.archive_org.logger.error",
            ),
        )

        for provider, name, request_path, log_path in provider_cases:
            with self.subTest(layer="provider", provider=name):
                with (
                    patch(request_path, return_value=unauthorized),
                    patch(log_path) as log_error,
                ):
                    result = provider.search("private query", minimum_duration=1)

                self.assertEqual(result, [])
                log_error.assert_called_once_with(
                    f"[{name}] search failed: HTTPError status=401"
                )
                self.assertNotIn("token=secret", str(log_error.call_args))

        legacy_cases = (
            (material.search_videos_pexels, "pexels"),
            (material.search_videos_pixabay, "pixabay"),
            (material.search_videos_coverr, "coverr"),
        )
        for search_videos, name in legacy_cases:
            with self.subTest(layer="legacy", provider=name):
                with (
                    patch(
                        "app.services.material.requests.get",
                        return_value=unauthorized,
                    ),
                    patch.object(material.logger, "error") as log_error,
                ):
                    result = search_videos("private query", minimum_duration=1)

                self.assertEqual(result, [])
                log_error.assert_called_once_with(
                    f"[{name}] search failed: HTTPError status=401"
                )
                self.assertNotIn("token=secret", str(log_error.call_args))

    def test_provider_initialization_error_log_omits_exception_details(self):
        class FailingProvider:
            def __init__(self):
                raise RuntimeError(
                    "failed for https://example.test/init?token=secret-init-token"
                )

        with (
            patch.dict(
                provider_registry.PROVIDER_REGISTRY,
                {"failing_provider": FailingProvider},
            ),
            patch("loguru.logger.warning") as log_warning,
        ):
            result = provider_registry.get_active_providers(["failing_provider"])

        self.assertEqual(result, [])
        log_warning.assert_called_once_with(
            "[providers] 'failing_provider' initialization failed: RuntimeError"
        )
        self.assertNotIn("secret-init-token", str(log_warning.call_args))

    def test_legacy_missing_api_key_error_omits_other_config_values(self):
        with patch.dict(
            config.app,
            {
                "pexels_api_keys": [],
                "pixabay_api_keys": ["private-other-provider-key"],
                "proxy_password": "private-proxy-password",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError) as raised:
                material.get_api_key("pexels_api_keys")

        message = str(raised.exception)
        self.assertEqual(message, "pexels_api_keys is not set in config.toml")
        self.assertNotIn("private-other-provider-key", message)
        self.assertNotIn("private-proxy-password", message)

    def test_secondary_provider_nested_fetch_errors_omit_remote_identifiers(self):
        config.proxy.clear()
        private_url = "https://example.test/private-item?token=secret"

        def fail_json():
            raise AssertionError("JSON body must not be parsed after HTTP 403")

        forbidden_response = SimpleNamespace(
            status_code=403,
            url=private_url,
            json=fail_json,
        )

        nasa_search_response = SimpleNamespace(
            json=lambda: {"collection": {"items": [{"href": private_url}]}}
        )
        with (
            patch(
                "app.services.providers.nasa.requests.get",
                side_effect=[nasa_search_response, forbidden_response],
            ),
            patch("app.services.providers.nasa.logger.warning") as log_warning,
        ):
            result = NASAProvider().search("space", minimum_duration=1)

        self.assertEqual(result, [])
        log_warning.assert_called_once_with(
            "[nasa] collection fetch failed: HTTPError status=403"
        )
        self.assertNotIn(private_url, str(log_warning.call_args))

        wikimedia_search_response = SimpleNamespace(
            json=lambda: {"query": {"search": [{"title": "Private video"}]}}
        )
        with (
            patch(
                "app.services.providers.wikimedia.requests.get",
                side_effect=[wikimedia_search_response, forbidden_response],
            ),
            patch("app.services.providers.wikimedia.logger.error") as log_error,
        ):
            result = WikimediaProvider().search("space", minimum_duration=1)

        self.assertEqual(result, [])
        log_error.assert_called_once_with(
            "[wikimedia] videoinfo fetch failed: HTTPError status=403"
        )
        self.assertNotIn(private_url, str(log_error.call_args))

        with (
            patch(
                "app.services.providers.archive_org.requests.get",
                return_value=forbidden_response,
            ),
            patch("app.services.providers.archive_org.logger.warning") as log_warning,
        ):
            result = ArchiveOrgProvider()._fetch_best_mp4(
                "private-item-token-secret",
                minimum_duration=1,
            )

        self.assertIsNone(result)
        log_warning.assert_called_once_with(
            "[archive_org] metadata fetch failed: HTTPError status=403"
        )
        self.assertNotIn("private-item-token-secret", str(log_warning.call_args))

class TestCoverrProvider(unittest.TestCase):
    """
    Coverr 视频素材源(spec: 2026-06-09-coverr-video-provider-design.md)。
    全部用 unittest.mock 替换 requests，确保 CI 不依赖真实网络和真实 API key。
    """

    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    # ---------------- Tests for search_videos_coverr ----------------

    def test_search_coverr_uses_mp4_download_url(self):
        """
        search_videos_coverr 应把每个 hit 转成 MaterialInfo，并把 urls.mp4_download
        直接作为 MaterialInfo.url。
        按 Coverr 官方文档 (api.coverr.co/docs/videos/#download-a-video),
        GET mp4_download 本身就被 Coverr 计入下载统计,无需额外 PATCH ping。
        同时验证 Authorization header 使用 Bearer scheme。
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "page": 0,
                "pages": 50,
                "page_size": 20,
                "total": 1,
                "hits": [
                    {
                        "id": "S1YbPl1NfI",
                        "duration": 11.625,
                        "aspect_ratio": "16:9",
                        "title": "Misty forest trail",
                        "description": "A quiet trail winds through the forest.",
                        "tags": ["forest", " trail ", "", 42],
                        "poster": "https://example.com/forest-poster.jpg",
                        "urls": {
                            "mp4": "https://storage.coverr.co/videos/abc?token=xyz",
                            "mp4_preview": "https://storage.coverr.co/videos/abc/preview?token=xyz",
                            "mp4_download": "https://storage.coverr.co/videos/abc/download?token=xyz",
                        },
                    }
                ],
            }
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ) as get:
            results = material.search_videos_coverr("nature", minimum_duration=5)

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.provider, "coverr")
        self.assertEqual(item.duration, 11)
        self.assertEqual(item.search_query, "nature")
        self.assertEqual(item.title, "Misty forest trail")
        self.assertEqual(
            item.description,
            "A quiet trail winds through the forest.",
        )
        self.assertEqual(item.tags, ["forest", "trail"])
        self.assertEqual(item.preview_url, "https://example.com/forest-poster.jpg")
        # url 字段就是 mp4_download URL,不再做 coverr://id|url 编码
        self.assertEqual(
            item.url, "https://storage.coverr.co/videos/abc/download?token=xyz"
        )
        # Bearer auth + TLS verify on by default
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"], "Bearer coverr-key"
        )
        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_coverr_uses_tls_verification_by_default(self):
        """与 pexels/pixabay 一致:未显式配置时 TLS 校验默认开启。"""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(json=lambda: {"hits": []})

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ) as get:
            material.search_videos_coverr("nature", minimum_duration=1)

        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_coverr_allows_explicit_tls_disable_for_proxy(self):
        """企业自签证书代理场景必须能显式关闭 TLS 校验。"""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app["tls_verify"] = False
        config.proxy.clear()

        fake_response = SimpleNamespace(json=lambda: {"hits": []})

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ) as get:
            material.search_videos_coverr("nature", minimum_duration=1)

        self.assertFalse(get.call_args.kwargs["verify"])

    def test_search_coverr_filters_by_min_duration_and_accepts_string(self):
        """
        Coverr duration 字段在不同响应里可能是 number 或 string,
        两种格式都要接受;低于 minimum_duration 的应被过滤。
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "id": "shortvid",
                        "duration": 3,  # below minimum
                        "urls": {"mp4_download": "https://example.com/a.mp4"},
                    },
                    {
                        "id": "stringdur",
                        "duration": "10.500000",  # string accepted
                        "urls": {"mp4_download": "https://example.com/b.mp4"},
                    },
                ]
            }
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ):
            results = material.search_videos_coverr("x", minimum_duration=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].duration, 10)
        self.assertEqual(results[0].url, "https://example.com/b.mp4")

    def test_search_coverr_skips_invalid_items(self):
        """缺 id 或缺 urls.mp4_download 的条目应被跳过,不应抛异常。"""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {  # missing urls.mp4_download
                        "id": "no-download",
                        "duration": 10,
                        "urls": {"mp4_preview": "https://example.com/preview.mp4"},
                    },
                    {  # missing id
                        "duration": 10,
                        "urls": {"mp4_download": "https://example.com/x.mp4"},
                    },
                    {  # valid baseline
                        "id": "good",
                        "duration": 10,
                        "urls": {"mp4_download": "https://example.com/good.mp4"},
                    },
                ]
            }
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ):
            results = material.search_videos_coverr("x", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/good.mp4")

    def test_search_coverr_returns_empty_on_failure(self):
        """
        响应结构异常 / 网络异常时,函数必须返回 [] 而不是抛异常,
        与 pexels/pixabay 行为保持一致。
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        # Subtest A: malformed response (no "hits" key)
        with self.subTest("malformed response"):
            fake_response = SimpleNamespace(
                json=lambda: {"error": "rate limited"}
            )
            with patch(
                "app.services.material.requests.get", return_value=fake_response
            ):
                results = material.search_videos_coverr("x", minimum_duration=1)
            self.assertEqual(results, [])

        # Subtest B: network exception bubbles up from requests.get
        with self.subTest("network exception"):
            with patch(
                "app.services.material.requests.get",
                side_effect=requests.ConnectionError("boom"),
            ):
                results = material.search_videos_coverr("x", minimum_duration=1)
            self.assertEqual(results, [])

    # ---------------- Tests for download_videos coverr branch ----------------

    def test_download_videos_passes_mp4_download_url_to_save_video(self):
        """
        在 source="coverr" 时:
          1. dispatch 到 search_videos_coverr
          2. coverr item 走通用下载路径:save_video 收到的就是 mp4_download URL
             (不再有 coverr://id|url 编码,也不再调用 PATCH ping)
          3. 返回保存路径
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.app.pop("material_directory", None)
        config.proxy.clear()

        fake_item = material.MaterialInfo()
        fake_item.provider = "coverr"
        fake_item.url = "https://storage.coverr.co/videos/abc/download?token=xyz"
        fake_item.duration = 10

        with patch(
            "app.services.material.search_videos_coverr",
            return_value=[fake_item],
        ) as search, patch(
            "app.services.material.save_video",
            return_value="/tmp/coverr-saved.mp4",
        ) as save:
            result = material.download_videos(
                task_id="t-coverr",
                search_terms=["nature"],
                source="coverr",
                audio_duration=5,
                max_clip_duration=5,
            )

        # 1. dispatch
        self.assertEqual(search.call_count, 1)

        # 2. save_video 收到的就是 mp4_download URL,原样传入
        save_url = save.call_args.kwargs.get("video_url") or save.call_args.args[0]
        self.assertEqual(
            save_url, "https://storage.coverr.co/videos/abc/download?token=xyz"
        )

        # 3. 返回值正确
        self.assertEqual(result, ["/tmp/coverr-saved.mp4"])


class TestVideoCooldown(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)
        config.app["video_cooldown_enabled"] = False

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    def _item(self, url, duration=6, provider="pexels"):
        return material.MaterialInfo(
            provider=provider,
            url=url,
            duration=duration,
        )

    def test_rank_materials_prioritizes_fresh_url_when_enabled(self):
        old_item = self._item("https://v.example/old.mp4")
        fresh_item = self._item("https://v.example/fresh.mp4")

        with (
            patch.dict(
                config.app,
                {
                    "video_cooldown_enabled": True,
                    "video_cooldown_days": 7,
                    "max_material_duration": 180,
                },
            ),
            patch.object(
                material.video_cooldown,
                "recent_urls",
                return_value={"https://v.example/old.mp4"},
            ) as recent_urls,
        ):
            cooldown_stats = {"moved_recent_count": 0}
            ranked = material._rank_materials(
                [old_item, fresh_item],
                max_clip_duration=5,
                cooldown_stats=cooldown_stats,
            )

        self.assertEqual(
            [item.url for item in ranked],
            [fresh_item.url, old_item.url],
        )
        self.assertEqual(cooldown_stats["moved_recent_count"], 1)
        self.assertEqual(cooldown_stats["days"], 7)
        recent_urls.assert_called_once_with(7)

    def test_rank_materials_uses_configured_cooldown_days(self):
        item = self._item("https://v.example/fresh.mp4")

        with (
            patch.dict(
                config.app,
                {
                    "video_cooldown_enabled": True,
                    "video_cooldown_days": 14,
                    "max_material_duration": 180,
                },
            ),
            patch.object(
                material.video_cooldown,
                "recent_urls",
                return_value=set(),
            ) as recent_urls,
        ):
            ranked = material._rank_materials([item], max_clip_duration=5)

        self.assertEqual([ranked_item.url for ranked_item in ranked], [item.url])
        recent_urls.assert_called_once_with(14)

    def test_rank_materials_does_not_read_recent_urls_when_disabled(self):
        item = self._item("https://v.example/fresh.mp4")

        with (
            patch.dict(
                config.app,
                {
                    "video_cooldown_enabled": False,
                    "video_cooldown_days": 14,
                    "max_material_duration": 180,
                },
            ),
            patch.object(material.video_cooldown, "recent_urls") as recent_urls,
        ):
            cooldown_stats = {"moved_recent_count": 0}
            ranked = material._rank_materials(
                [item],
                max_clip_duration=5,
                cooldown_stats=cooldown_stats,
            )

        self.assertEqual([ranked_item.url for ranked_item in ranked], [item.url])
        self.assertEqual(cooldown_stats["moved_recent_count"], 0)
        recent_urls.assert_not_called()

    def test_rank_materials_falls_back_when_all_candidates_are_recent(self):
        first_item = self._item("https://v.example/first.mp4", duration=6)
        second_item = self._item("https://v.example/second.mp4", duration=7)

        with (
            patch.dict(
                config.app,
                {
                    "video_cooldown_enabled": True,
                    "video_cooldown_days": 7,
                    "max_material_duration": 180,
                },
            ),
            patch.object(
                material.video_cooldown,
                "recent_urls",
                return_value={
                    "https://v.example/first.mp4",
                    "https://v.example/second.mp4",
                },
            ),
        ):
            cooldown_stats = {"moved_recent_count": 0}
            ranked = material._rank_materials(
                [first_item, second_item],
                max_clip_duration=5,
                cooldown_stats=cooldown_stats,
            )

        self.assertEqual(
            {item.url for item in ranked},
            {first_item.url, second_item.url},
        )
        self.assertEqual(cooldown_stats["moved_recent_count"], 0)

    def test_ordered_candidate_groups_defer_recent_materials_until_fresh_groups_finish(
        self,
    ):
        first_fresh = self._item("https://v.example/first-fresh.mp4")
        first_recent = self._item("https://v.example/first-recent.mp4")
        second_fresh = self._item("https://v.example/second-fresh.mp4")
        second_fresh_extra = self._item("https://v.example/second-fresh-extra.mp4")
        candidate_groups = [
            ("first scene", [first_fresh, first_recent]),
            ("second scene", [second_fresh, second_fresh_extra]),
        ]

        with (
            patch.dict(config.app, {"video_cooldown_enabled": True}),
            patch.object(
                material.video_cooldown,
                "recent_urls",
                return_value={"https://v.example/first-recent.mp4"},
            ) as recent_urls,
        ):
            fresh_groups, deferred_groups = (
                material._split_ordered_candidate_groups_by_cooldown(
                    candidate_groups
                )
            )

        self.assertEqual(
            fresh_groups,
            [
                ("first scene", [first_fresh]),
                ("second scene", [second_fresh, second_fresh_extra]),
            ],
        )
        self.assertEqual(deferred_groups, [("first scene", [first_recent])])
        recent_urls.assert_called_once_with(7)

    def test_ordered_candidate_groups_preserve_order_when_cooldown_is_disabled(
        self,
    ):
        first_item = self._item("https://v.example/first.mp4")
        second_item = self._item("https://v.example/second.mp4")
        candidate_groups = [("scene", [first_item, second_item])]

        with (
            patch.dict(config.app, {"video_cooldown_enabled": False}),
            patch.object(material.video_cooldown, "recent_urls") as recent_urls,
        ):
            fresh_groups, deferred_groups = (
                material._split_ordered_candidate_groups_by_cooldown(
                    candidate_groups
                )
            )

        self.assertEqual(fresh_groups, candidate_groups)
        self.assertEqual(deferred_groups, [])
        recent_urls.assert_not_called()

    def test_script_order_download_prefers_global_fresh_candidates_before_cooldown(
        self,
    ):
        first_fresh = self._item("https://v.example/first-fresh.mp4", duration=2)
        first_recent = self._item("https://v.example/first-recent.mp4", duration=2)
        second_fresh = self._item("https://v.example/second-fresh.mp4", duration=2)
        second_fresh_extra = self._item(
            "https://v.example/second-fresh-extra.mp4", duration=2
        )
        candidates_by_term = {
            "first scene": [first_fresh, first_recent],
            "second scene": [second_fresh, second_fresh_extra],
        }
        saved_urls = []

        def search_candidates(search_term, _search_videos):
            return candidates_by_term[search_term], False

        def save_candidate(item, _material_directory):
            saved_urls.append(item.url)
            return f"/tmp/{len(saved_urls)}.mp4"

        with (
            patch.dict(
                config.app,
                {
                    "video_cooldown_enabled": True,
                    "video_cooldown_days": 7,
                },
            ),
            patch.object(
                material,
                "_search_script_order_candidates",
                side_effect=search_candidates,
            ),
            patch.object(
                material,
                "_rank_materials",
                side_effect=lambda items, *_args, **_kwargs: list(items),
            ),
            patch.object(material, "_save_ranked_material", side_effect=save_candidate),
            patch.object(material, "_mark_video_cooldown_used"),
            patch.object(
                material.video_cooldown,
                "recent_urls",
                return_value={first_recent.url},
            ),
        ):
            result = material._download_videos_by_script_order(
                task_id="fresh-first-order",
                search_terms=list(candidates_by_term),
                search_videos=lambda **_kwargs: [],
                video_aspect=material.VideoAspect.portrait,
                audio_duration=4,
                max_clip_duration=2,
                material_directory="/tmp",
            )

        self.assertEqual(result, ["/tmp/1.mp4", "/tmp/2.mp4", "/tmp/3.mp4"])
        self.assertEqual(
            saved_urls,
            [first_fresh.url, second_fresh.url, second_fresh_extra.url],
        )

    def test_multi_ordered_download_prefers_global_fresh_candidates_before_cooldown(
        self,
    ):
        first_fresh = self._item("https://v.example/first-fresh.mp4", duration=2)
        first_recent = self._item("https://v.example/first-recent.mp4", duration=2)
        second_fresh = self._item("https://v.example/second-fresh.mp4", duration=2)
        second_fresh_extra = self._item(
            "https://v.example/second-fresh-extra.mp4", duration=2
        )
        candidates_by_term = {
            "first scene": [first_fresh, first_recent],
            "second scene": [second_fresh, second_fresh_extra],
        }
        saved_urls = []

        def search_candidates(search_term, _search_videos):
            return candidates_by_term[search_term], False

        def save_candidate(item, _material_directory):
            saved_urls.append(item.url)
            return f"/tmp/{len(saved_urls)}.mp4"

        with (
            patch.dict(
                config.app,
                {
                    "video_cooldown_enabled": True,
                    "video_cooldown_days": 7,
                },
            ),
            patch.object(
                material,
                "_search_script_order_candidates",
                side_effect=search_candidates,
            ),
            patch.object(
                material,
                "_rank_materials",
                side_effect=lambda items, *_args, **_kwargs: list(items),
            ),
            patch.object(material, "_save_ranked_material", side_effect=save_candidate),
            patch.object(material, "_mark_video_cooldown_used"),
            patch.object(
                material.video_cooldown,
                "recent_urls",
                return_value={first_recent.url},
            ),
        ):
            result = material._download_multi_ordered(
                task_id="fresh-first-multi",
                search_terms=list(candidates_by_term),
                providers=[],
                video_aspect=material.VideoAspect.portrait,
                audio_duration=5,
                max_clip_duration=2,
                material_directory="/tmp",
            )

        self.assertEqual(result, ["/tmp/1.mp4", "/tmp/2.mp4", "/tmp/3.mp4"])
        self.assertEqual(
            saved_urls,
            [first_fresh.url, second_fresh.url, second_fresh_extra.url],
        )

    def test_successful_selected_download_marks_video_used_when_enabled(self):
        selected_items = [
            self._item("https://v.example/selected.mp4", provider="pixabay"),
        ]

        with (
            patch.dict(
                config.app,
                {
                    "material_directory": "",
                    "video_cooldown_enabled": True,
                    "video_cooldown_days": 7,
                },
            ),
            patch.object(material, "save_video", return_value="/tmp/selected.mp4"),
            patch.object(material.video_cooldown, "mark_used") as mark_used,
        ):
            result = material.download_selected_videos(
                task_id="selected-cooldown",
                selected_items=selected_items,
                audio_duration=1,
                max_clip_duration=5,
            )

        self.assertEqual(result, ["/tmp/selected.mp4"])
        mark_used.assert_called_once_with(
            "https://v.example/selected.mp4",
            provider="pixabay",
        )

    def test_failed_selected_download_does_not_mark_video_used(self):
        selected_items = [
            self._item("https://v.example/fail.mp4", provider="pexels"),
        ]

        with (
            patch.dict(
                config.app,
                {
                    "material_directory": "",
                    "video_cooldown_enabled": True,
                    "video_cooldown_days": 7,
                },
            ),
            patch.object(material, "save_video", return_value=""),
            patch.object(material.video_cooldown, "mark_used") as mark_used,
        ):
            result = material.download_selected_videos(
                task_id="selected-cooldown-failed",
                selected_items=selected_items,
                audio_duration=1,
                max_clip_duration=5,
            )

        self.assertEqual(result, [])
        mark_used.assert_not_called()


class TestPhotoFallback(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        config.app.pop("photo_fallback_enabled", None)
        config.app["material_directory"] = ""
        config.app["smithsonian_api_keys"] = []
        config.app["openverse_photo_fallback_enabled"] = False
        config.app["europeana_api_keys"] = []
        config.app["europeana_photo_fallback_enabled"] = False
        config.app["museum_photo_fallback_enabled"] = False

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)

    def test_search_photos_pexels_uses_photo_search_endpoint(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        response = SimpleNamespace(
            json=lambda: {
                "photos": [
                    {
                        "width": 1080,
                        "height": 1920,
                        "url": "https://www.pexels.com/photo/example/",
                        "photographer": "Example Creator",
                        "alt": "City skyline",
                        "src": {"original": "https://images.example/photo.jpg"},
                    }
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=response) as get:
            results = material.search_photos_pexels(
                "city skyline",
                video_aspect=VideoAspect.portrait,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://images.example/photo.jpg")
        self.assertEqual(results[0].search_query, "city skyline")
        self.assertEqual(results[0].title, "City skyline")
        self.assertIn("Example Creator", results[0].attribution)
        self.assertIn("/v1/search", get.call_args.args[0])

    def test_search_photos_pixabay_uses_image_endpoint(self):
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "imageWidth": 1080,
                        "imageHeight": 1920,
                        "largeImageURL": "https://images.example/photo.jpg",
                        "pageURL": "https://pixabay.com/photos/example/",
                        "user": "Example Creator",
                        "tags": "city, skyline",
                    }
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=response) as get:
            results = material.search_photos_pixabay(
                "city skyline",
                video_aspect=VideoAspect.portrait,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://images.example/photo.jpg")
        self.assertEqual(results[0].tags, ["city", "skyline"])
        self.assertIn("pixabay.com/api/?", get.call_args.args[0])

    def test_search_photos_smithsonian_keeps_only_cc0_high_resolution_images(self):
        config.app["smithsonian_api_keys"] = ["smithsonian-key"]
        response = SimpleNamespace(
            json=lambda: {
                "response": {
                    "rows": [
                        {
                            "title": "CC0 bird photograph",
                            "content": {
                                "descriptiveNonRepeating": {
                                    "metadata_usage": {"access": "CC0"},
                                    "online_media": {
                                        "media": [
                                            {
                                                "type": "Images",
                                                "usage": {"access": "CC0"},
                                                "thumbnail": (
                                                    "https://ids.si.edu/ids/deliveryService/"
                                                    "id/ark:/65665/example/90"
                                                ),
                                                "resources": [
                                                    {
                                                        "label": "High-resolution JPEG",
                                                        "url": (
                                                            "https://ids.si.edu/ids/download?"
                                                            "id=ark:/65665/example"
                                                        ),
                                                        "width": 1080,
                                                        "height": 1920,
                                                    }
                                                ],
                                            }
                                        ]
                                    },
                                }
                            },
                        },
                        {
                            "title": "Restricted image",
                            "content": {
                                "descriptiveNonRepeating": {
                                    "online_media": {
                                        "media": [
                                            {
                                                "type": "Images",
                                                "usage": {"access": "Usage restrictions"},
                                                "resources": [
                                                    {
                                                        "label": "High-resolution JPEG",
                                                        "url": (
                                                            "https://ids.si.edu/ids/download?"
                                                            "id=ark:/65665/restricted"
                                                        ),
                                                        "width": 1080,
                                                        "height": 1920,
                                                    }
                                                ],
                                            }
                                        ]
                                    }
                                }
                            },
                        },
                        {
                            "title": "Untrusted image host",
                            "content": {
                                "descriptiveNonRepeating": {
                                    "online_media": {
                                        "media": [
                                            {
                                                "type": "Images",
                                                "usage": {"access": "CC0"},
                                                "resources": [
                                                    {
                                                        "label": "High-resolution JPEG",
                                                        "url": "https://images.example/photo.jpg",
                                                        "width": 1080,
                                                        "height": 1920,
                                                    }
                                                ],
                                            }
                                        ]
                                    }
                                }
                            },
                        },
                        {
                            "title": "Too small image",
                            "content": {
                                "descriptiveNonRepeating": {
                                    "online_media": {
                                        "media": [
                                            {
                                                "type": "Images",
                                                "usage": {"access": "CC0"},
                                                "resources": [
                                                    {
                                                        "label": "High-resolution JPEG",
                                                        "url": (
                                                            "https://ids.si.edu/ids/download?"
                                                            "id=ark:/65665/small"
                                                        ),
                                                        "width": 400,
                                                        "height": 400,
                                                    }
                                                ],
                                            }
                                        ]
                                    }
                                }
                            },
                        },
                    ]
                }
            }
        )

        with patch("app.services.material.requests.get", return_value=response) as get:
            results = material.search_photos_smithsonian(
                "city bird",
                video_aspect=VideoAspect.portrait,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].provider, "smithsonian")
        self.assertEqual(results[0].url, "https://ids.si.edu/ids/download?id=ark:/65665/example")
        self.assertEqual(results[0].width, 1080)
        self.assertEqual(results[0].height, 1920)
        self.assertEqual(results[0].title, "CC0 bird photograph")
        self.assertEqual(results[0].license, "CC0")
        self.assertEqual(
            results[0].license_url,
            "https://creativecommons.org/publicdomain/zero/1.0/",
        )
        self.assertIn("Smithsonian", results[0].attribution)
        self.assertIn("api.si.edu/openaccess/api/v1.0/search", get.call_args.args[0])
        self.assertNotIn("smithsonian-key", get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs["params"]["api_key"], "smithsonian-key")
        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_photos_smithsonian_skips_request_without_an_api_key(self):
        config.app.pop("smithsonian_api_keys", None)

        with patch("app.services.material.requests.get") as get:
            results = material.search_photos_smithsonian("city bird")

        self.assertEqual(results, [])
        get.assert_not_called()

    def test_search_photos_openverse_keeps_only_safe_cc0_or_public_domain_images(self):
        config.app["openverse_photo_fallback_enabled"] = True
        response = SimpleNamespace(
            json=lambda: {
                "results": [
                    {
                        "url": "https://images.example/cc0-city.jpg",
                        "thumbnail": "https://images.example/cc0-city-thumb.jpg",
                        "filetype": "jpg",
                        "width": 1080,
                        "height": 1920,
                        "license": "cc0",
                        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                        "creator": "Example Creator",
                        "creator_url": "https://example.org/creator",
                        "title": "City at night",
                        "tags": [{"name": "city"}, {"name": "night"}],
                        "mature": False,
                    },
                    {
                        "url": "https://images.example/public-domain.jpg",
                        "thumbnail": "https://images.example/public-domain-thumb.jpg",
                        "width": 1920,
                        "height": 1080,
                        "license": "pdm",
                        "license_url": "https://creativecommons.org/publicdomain/mark/1.0/",
                        "creator": "Archive",
                        "title": "Public-domain city",
                        "tags": ["archive"],
                        "mature": False,
                    },
                    {
                        "url": "https://images.example/by-city.jpg",
                        "width": 1920,
                        "height": 1080,
                        "license": "by",
                        "license_url": "https://creativecommons.org/licenses/by/4.0/",
                        "mature": False,
                    },
                    {
                        "url": "http://images.example/insecure.jpg",
                        "width": 1920,
                        "height": 1080,
                        "license": "cc0",
                        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                        "mature": False,
                    },
                    {
                        "url": "https://images.example/small.jpg",
                        "width": 320,
                        "height": 240,
                        "license": "cc0",
                        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                        "mature": False,
                    },
                    {
                        "url": "https://images.example/mature.jpg",
                        "width": 1920,
                        "height": 1080,
                        "license": "cc0",
                        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                        "mature": True,
                    },
                ]
            }
        )

        with patch(
            "app.services.providers.openverse.requests.get", return_value=response
        ) as get:
            results = material.search_photos_openverse(
                "city skyline", video_aspect=VideoAspect.portrait
            )

        self.assertEqual([item.url for item in results], [
            "https://images.example/cc0-city.jpg",
            "https://images.example/public-domain.jpg",
        ])
        self.assertEqual(results[0].provider, "openverse")
        self.assertEqual(results[0].license, "CC0")
        self.assertEqual(results[1].license, "Public Domain Mark")
        self.assertEqual(results[0].tags, ["city", "night"])
        self.assertIn("Example Creator", results[0].attribution)
        self.assertEqual(
            get.call_args.kwargs["params"]["license"], "cc0,pdm"
        )
        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_photos_openverse_skips_network_when_disabled(self):
        with patch("app.services.providers.openverse.requests.get") as get:
            results = material.search_photos_openverse("city skyline")

        self.assertEqual(results, [])
        get.assert_not_called()

    def test_search_photos_europeana_accepts_only_cc0_or_public_domain_records(self):
        config.app["europeana_api_keys"] = ["europeana-key"]
        config.app["europeana_photo_fallback_enabled"] = True
        search_response = SimpleNamespace(
            json=lambda: {"items": [{"id": "/123/example"}, {"id": "/123/pdm"}]}
        )
        record_response = SimpleNamespace(
            json=lambda: {
                "object": {
                    "title": ["Public-domain city archive"],
                    "aggregations": [
                        {
                            "edmRights": (
                                "http://creativecommons.org/publicdomain/zero/1.0/"
                            ),
                            "edmIsShownBy": "https://media.example.org/city.jpg",
                            "edmDataProvider": ["Example Archive"],
                        }
                    ],
                }
            }
        )
        pdm_record_response = SimpleNamespace(
            json=lambda: {
                "object": {
                    "title": ["Public-domain marked archive"],
                    "aggregations": [
                        {
                            "edmRights": (
                                "https://creativecommons.org/publicdomain/mark/1.0/"
                            ),
                            "edmIsShownBy": "https://media.example.org/archive.jpg",
                        }
                    ],
                }
            }
        )

        with patch(
            "app.services.providers.europeana.requests.get",
            side_effect=[search_response, record_response, pdm_record_response],
        ) as get:
            results = europeana_provider.search_photos_europeana("city archive")

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].provider, "europeana")
        self.assertEqual(results[0].license, "CC0")
        self.assertEqual(results[1].license, "Public Domain Mark")
        self.assertEqual(results[0].title, "Public-domain city archive")
        self.assertIn("Example Archive", results[0].attribution)
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["X-Api-Key"], "europeana-key")
        self.assertEqual(get.call_args_list[0].kwargs["params"]["reusability"], "open")

    def test_search_photos_europeana_uses_one_api_key_lookup_per_search(self):
        config.app["europeana_api_keys"] = ["first-key", "second-key"]
        config.app["europeana_photo_fallback_enabled"] = True
        search_response = SimpleNamespace(json=lambda: {"items": []})

        with patch(
            "app.services.providers.europeana.get_api_key",
            return_value="first-key",
        ) as get_api_key, patch(
            "app.services.providers.europeana.requests.get",
            return_value=search_response,
        ):
            results = europeana_provider.search_photos_europeana("city archive")

        self.assertEqual(results, [])
        get_api_key.assert_called_once_with("europeana_api_keys")

    def test_search_photos_europeana_rejects_non_public_domain_or_unsafe_media(self):
        config.app["europeana_api_keys"] = ["europeana-key"]
        config.app["europeana_photo_fallback_enabled"] = True
        search_response = SimpleNamespace(
            json=lambda: {"items": [{"id": "/123/by"}, {"id": "/123/unsafe"}]}
        )
        by_record = SimpleNamespace(
            json=lambda: {
                "object": {
                    "aggregations": [
                        {
                            "edmRights": "https://creativecommons.org/licenses/by/4.0/",
                            "edmIsShownBy": "https://media.example.org/by.jpg",
                        }
                    ]
                }
            }
        )
        unsafe_record = SimpleNamespace(
            json=lambda: {
                "object": {
                    "aggregations": [
                        {
                            "edmRights": (
                                "http://creativecommons.org/publicdomain/mark/1.0/"
                            ),
                            "edmIsShownBy": "http://127.0.0.1/private.jpg",
                        }
                    ]
                }
            }
        )

        with patch(
            "app.services.providers.europeana.requests.get",
            side_effect=[search_response, by_record, unsafe_record],
        ):
            results = europeana_provider.search_photos_europeana("city archive")

        self.assertEqual(results, [])

    def test_search_photos_europeana_skips_network_without_a_key(self):
        config.app["europeana_photo_fallback_enabled"] = True

        with patch("app.services.providers.europeana.requests.get") as get:
            results = europeana_provider.search_photos_europeana("city archive")

        self.assertEqual(results, [])
        get.assert_not_called()

    def test_search_photos_met_keeps_only_public_domain_trusted_images(self):
        config.app["museum_photo_fallback_enabled"] = True
        search_response = SimpleNamespace(json=lambda: {"objectIDs": [100, 200]})
        public_domain_response = SimpleNamespace(
            json=lambda: {
                "objectID": 100,
                "title": "Public-domain landscape",
                "isPublicDomain": True,
                "primaryImage": "https://images.metmuseum.org/CRDImages/ep/original/example.jpg",
                "artistDisplayName": "Example Artist",
                "objectURL": "https://www.metmuseum.org/art/collection/search/100",
            }
        )
        restricted_response = SimpleNamespace(
            json=lambda: {
                "objectID": 200,
                "title": "Restricted work",
                "isPublicDomain": False,
                "primaryImage": "https://images.metmuseum.org/CRDImages/ep/original/restricted.jpg",
            }
        )

        with patch(
            "app.services.providers.museum.requests.get",
            side_effect=[search_response, public_domain_response, restricted_response],
        ) as get:
            results = museum_provider.search_photos_met("landscape")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].provider, "met")
        self.assertEqual(results[0].license, "Public Domain")
        self.assertEqual(results[0].title, "Public-domain landscape")
        self.assertIn("Example Artist", results[0].attribution)
        self.assertEqual(get.call_count, 3)
        self.assertTrue(get.call_args_list[0].kwargs["params"]["hasImages"])

    def test_search_photos_artic_keeps_only_public_domain_images(self):
        config.app["museum_photo_fallback_enabled"] = True
        response = SimpleNamespace(
            json=lambda: {
                "config": {"iiif_url": "https://www.artic.edu/iiif/2"},
                "data": [
                    {
                        "id": 100,
                        "title": "Public-domain city",
                        "is_public_domain": True,
                        "image_id": "safe-image-id",
                        "artist_display": "Example Artist",
                        "thumbnail": {"width": 1920, "height": 1080},
                    },
                    {
                        "id": 200,
                        "title": "Restricted city",
                        "is_public_domain": False,
                        "image_id": "restricted-image-id",
                        "thumbnail": {"width": 1920, "height": 1080},
                    },
                ],
            }
        )

        with patch(
            "app.services.providers.museum.requests.get", return_value=response
        ) as get:
            results = museum_provider.search_photos_artic("city")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].provider, "artic")
        self.assertEqual(results[0].license, "Public Domain")
        self.assertTrue(results[0].url.endswith("/safe-image-id/full/1686,/0/default.jpg"))
        self.assertIn("Example Artist", results[0].attribution)
        self.assertEqual(
            get.call_args.kwargs["params"]["query[term][is_public_domain]"],
            "true",
        )

    def test_museum_photo_fallback_runs_only_after_primary_candidates_fail(self):
        config.app["museum_photo_fallback_enabled"] = True
        primary_photo = MaterialInfo(
            provider="pexels",
            url="https://images.example/primary.jpg",
            width=1080,
            height=1920,
        )
        museum_photo = MaterialInfo(
            provider="met",
            url="https://images.metmuseum.org/CRDImages/ep/original/example.jpg",
            width=1080,
            height=1920,
        )
        processed_photo = MaterialInfo(
            provider="local",
            url="C:/storage/local_videos/museum.jpg.mp4",
        )

        with (
            patch.object(
                material,
                "_photo_searchers_for_source",
                return_value=[Mock(return_value=[primary_photo])],
            ),
            patch.object(material, "search_photos_met", return_value=[museum_photo]) as search_met,
            patch.object(material, "search_photos_artic", return_value=[]) as search_artic,
            patch.object(
                material,
                "save_image",
                side_effect=["", "C:/storage/local_videos/museum.jpg"],
            ) as save_image,
            patch(
                "app.services.video.preprocess_video",
                return_value=[processed_photo],
            ),
        ):
            result = material._append_photo_fallback(
                [],
                search_terms=["city"],
                source="pexels",
                video_aspect=VideoAspect.portrait,
                audio_duration=5,
                max_clip_duration=5,
            )

        self.assertEqual(result, ["C:/storage/local_videos/museum.jpg.mp4"])
        search_met.assert_called_once_with("city", VideoAspect.portrait)
        search_artic.assert_called_once_with("city", VideoAspect.portrait)
        self.assertIs(
            save_image.call_args_list[1].kwargs["redirect_url_validator"],
            museum_provider.is_safe_museum_image_url,
        )

    def test_save_image_rejects_openverse_redirect_to_private_host(self):
        redirect = SimpleNamespace(
            status_code=302,
            headers={"Location": "https://127.0.0.1/private-image.jpg"},
            content=b"",
        )

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.services.material.requests.get", return_value=redirect
        ) as get:
            image_path = material.save_image(
                "https://images.example/cc0-city.jpg",
                save_dir=temp_dir,
                redirect_url_validator=openverse_provider.is_safe_openverse_image_url,
            )

        self.assertEqual(image_path, "")
        self.assertEqual(get.call_count, 1)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_europeana_photo_fallback_uses_safe_redirect_validation(self):
        europeana_photo = MaterialInfo(
            provider="europeana",
            url="https://media.example.org/public-domain-city.jpg",
        )
        processed_photo = MaterialInfo(
            provider="local",
            url="C:/storage/local_videos/europeana.jpg.mp4",
        )

        with (
            patch.object(
                material,
                "save_image",
                return_value="C:/storage/local_videos/europeana.jpg",
            ) as save_image,
            patch(
                "app.services.video.preprocess_video",
                return_value=[processed_photo],
            ),
        ):
            result = material._append_photo_fallback_candidates(
                [],
                [europeana_photo],
                required_count=1,
                max_clip_duration=5,
            )

        self.assertEqual(result, ["C:/storage/local_videos/europeana.jpg.mp4"])
        self.assertIs(
            save_image.call_args.kwargs["redirect_url_validator"],
            europeana_provider.is_safe_europeana_image_url,
        )

    def test_photo_fallback_adds_openverse_after_configured_sources(self):
        config.app["openverse_photo_fallback_enabled"] = True
        config.app["enabled_video_sources"] = []
        config.app["pexels_api_keys"] = []
        config.app["pixabay_api_keys"] = []

        searchers = material._photo_searchers_for_source("multi")

        self.assertEqual(searchers, [material.search_photos_openverse])

    def test_photo_fallback_adds_europeana_after_openverse_when_configured(self):
        config.app["openverse_photo_fallback_enabled"] = True
        config.app["europeana_photo_fallback_enabled"] = True
        config.app["europeana_api_keys"] = ["europeana-key"]
        config.app["enabled_video_sources"] = []
        config.app["pexels_api_keys"] = []
        config.app["pixabay_api_keys"] = []

        searchers = material._photo_searchers_for_source("multi")

        self.assertEqual(
            searchers,
            [material.search_photos_openverse, material.search_photos_europeana],
        )

    def test_photo_fallback_adds_smithsonian_when_a_key_is_configured(self):
        config.app["enabled_video_sources"] = []
        config.app["pexels_api_keys"] = []
        config.app["pixabay_api_keys"] = []
        config.app["smithsonian_api_keys"] = ["smithsonian-key"]

        searchers = material._photo_searchers_for_source("multi")

        self.assertEqual(searchers, [material.search_photos_smithsonian])

    def test_photo_fallback_adds_smithsonian_after_the_selected_source(self):
        config.app["smithsonian_api_keys"] = ["smithsonian-key"]

        searchers = material._photo_searchers_for_source("pexels")

        self.assertEqual(
            searchers,
            [material.search_photos_pexels, material.search_photos_smithsonian],
        )

    def test_photo_fallback_skips_unconfigured_enabled_stock_sources(self):
        config.app["enabled_video_sources"] = ["pexels", "pixabay"]
        config.app["pexels_api_keys"] = []
        config.app["pixabay_api_keys"] = []
        config.app["smithsonian_api_keys"] = ["smithsonian-key"]

        searchers = material._photo_searchers_for_source("multi")

        self.assertEqual(searchers, [material.search_photos_smithsonian])

    def test_photo_fallback_continues_when_another_searcher_fails(self):
        photo = MaterialInfo(
            provider="smithsonian",
            url="https://ids.si.edu/ids/download?id=ark:/65665/example",
            width=1080,
            height=1920,
        )
        failed_searcher = Mock(side_effect=ValueError("missing key"))
        succeeding_searcher = Mock(return_value=[photo])

        with patch.object(
            material,
            "_photo_searchers_for_source",
            return_value=[failed_searcher, succeeding_searcher],
        ):
            candidates = material._search_photo_fallback_candidates(
                ["city bird"],
                "multi",
                VideoAspect.portrait,
            )

        self.assertEqual(candidates, [photo])
        succeeding_searcher.assert_called_once_with("city bird", VideoAspect.portrait)

    def test_download_videos_with_smithsonian_source_skips_stock_video_search(self):
        config.app["smithsonian_api_keys"] = ["smithsonian-key"]

        with (
            patch.object(material, "search_videos_pexels") as search_videos_pexels,
            patch.object(material, "search_photos_smithsonian", return_value=[])
            as search_photos_smithsonian,
        ):
            result = material.download_videos(
                task_id="smithsonian-photo-only",
                search_terms=["city bird"],
                source="smithsonian",
                video_aspect=VideoAspect.portrait,
                audio_duration=5,
                max_clip_duration=5,
            )

        self.assertEqual(result, [])
        search_videos_pexels.assert_not_called()
        search_photos_smithsonian.assert_called_once_with(
            "city bird", VideoAspect.portrait
        )

    def test_save_image_uses_tls_verification_by_default(self):
        response = SimpleNamespace(
            content=b"image-bytes",
            headers={"Content-Type": "image/jpeg"},
            status_code=200,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("app.services.material.utils.storage_dir", return_value=temp_dir),
                patch("app.services.material.requests.get", return_value=response) as get,
            ):
                image_path = material.save_image("https://images.example/photo.jpg")

            self.assertTrue(os.path.isfile(image_path))

        self.assertTrue(get.call_args.kwargs["verify"])

    def test_download_videos_uses_processed_photo_when_videos_are_insufficient(self):
        photo = MaterialInfo(
            provider="pexels",
            url="https://images.example/photo.jpg",
            duration=5,
            width=1080,
            height=1920,
            title="City skyline",
            license="Pexels License",
            attribution="Photo by Example Creator on Pexels",
        )
        processed_photo = MaterialInfo(
            provider="local",
            url="C:/storage/local_videos/photo.jpg.mp4",
            duration=5,
        )
        attribution_records = []

        with (
            patch.object(material, "search_videos_pexels", return_value=[]),
            patch.object(material, "search_photos_pexels", return_value=[photo]),
            patch.object(
                material,
                "save_image",
                return_value="C:/storage/local_videos/photo.jpg",
            ),
            patch(
                "app.services.video.preprocess_video",
                return_value=[processed_photo],
            ) as preprocess_video,
        ):
            result = material.download_videos(
                task_id="photo-fallback",
                search_terms=["city skyline"],
                source="pexels",
                video_aspect=VideoAspect.portrait,
                audio_duration=5,
                max_clip_duration=5,
                attribution_records=attribution_records,
            )

        self.assertEqual(result, ["C:/storage/local_videos/photo.jpg.mp4"])
        preprocess_video.assert_called_once()
        self.assertEqual(
            attribution_records[0]["video_path"],
            "C:/storage/local_videos/photo.jpg.mp4",
        )

    def test_download_videos_uses_photo_when_one_video_is_below_required_count(self):
        video_item = MaterialInfo(
            provider="pexels",
            url="https://videos.example/clip.mp4",
            duration=5,
            width=1080,
            height=1920,
        )
        photo = MaterialInfo(
            provider="pexels",
            url="https://images.example/photo.jpg",
            duration=5,
            width=1080,
            height=1920,
        )
        processed_photo = MaterialInfo(
            provider="local",
            url="C:/storage/local_videos/photo.jpg.mp4",
            duration=5,
        )

        with (
            patch.object(material, "search_videos_pexels", return_value=[video_item]),
            patch.object(material, "save_video", return_value="C:/task/clip.mp4"),
            patch.object(material, "search_photos_pexels", return_value=[photo]),
            patch.object(
                material,
                "save_image",
                return_value="C:/storage/local_videos/photo.jpg",
            ),
            patch(
                "app.services.video.preprocess_video",
                return_value=[processed_photo],
            ),
        ):
            result = material.download_videos(
                task_id="photo-fallback-short",
                search_terms=["city skyline"],
                source="pexels",
                video_aspect=VideoAspect.portrait,
                audio_duration=10,
                max_clip_duration=5,
            )

        self.assertEqual(
            result,
            ["C:/task/clip.mp4", "C:/storage/local_videos/photo.jpg.mp4"],
        )

    def test_download_videos_uses_photo_to_meet_unique_visual_target(self):
        video_items = [
            MaterialInfo(
                provider="pexels",
                url=f"https://videos.example/clip-{index}.mp4",
                duration=5,
                width=1080,
                height=1920,
            )
            for index in range(1, 3)
        ]
        photo = MaterialInfo(
            provider="pexels",
            url="https://images.example/photo.jpg",
            duration=5,
            width=1080,
            height=1920,
        )
        processed_photo = MaterialInfo(
            provider="local",
            url="C:/storage/local_videos/photo.jpg.mp4",
            duration=5,
        )

        def fake_save_video(video_url, save_dir="", **_kwargs):
            return f"C:/task/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.object(material, "search_videos_pexels", return_value=video_items),
            patch.object(material, "save_video", side_effect=fake_save_video),
            patch.object(material, "search_photos_pexels", return_value=[photo]),
            patch.object(
                material,
                "save_image",
                return_value="C:/storage/local_videos/photo.jpg",
            ),
            patch(
                "app.services.video.preprocess_video",
                return_value=[processed_photo],
            ),
        ):
            result = material.download_videos(
                task_id="photo-fallback-crossfade",
                search_terms=["city skyline"],
                source="pexels",
                video_concat_mode="sequential",
                audio_duration=9,
                max_clip_duration=5,
                minimum_unique_visual_count=3,
            )

        self.assertEqual(
            result,
            [
                "C:/task/clip-1.mp4",
                "C:/task/clip-2.mp4",
                "C:/storage/local_videos/photo.jpg.mp4",
            ],
        )

    def test_download_videos_keeps_legacy_result_when_photo_fallback_is_disabled(self):
        config.app["photo_fallback_enabled"] = False

        with (
            patch.object(material, "search_videos_pexels", return_value=[]),
            patch.object(material, "search_photos_pexels") as search_photos,
        ):
            result = material.download_videos(
                task_id="photo-fallback-disabled",
                search_terms=["city skyline"],
                source="pexels",
                audio_duration=5,
                max_clip_duration=5,
            )

        self.assertEqual(result, [])
        search_photos.assert_not_called()

    def test_download_videos_keeps_legacy_result_when_no_photos_are_found(self):
        with (
            patch.object(material, "search_videos_pexels", return_value=[]),
            patch.object(material, "search_photos_pexels", return_value=[]),
            patch.object(material, "save_image") as save_image,
        ):
            result = material.download_videos(
                task_id="photo-fallback-none",
                search_terms=["city skyline"],
                source="pexels",
                audio_duration=5,
                max_clip_duration=5,
            )

        self.assertEqual(result, [])
        save_image.assert_not_called()


class TestOpenMontageMaterialSelection(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        config.app["video_cooldown_enabled"] = False
        config.app["photo_fallback_enabled"] = False

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)

    def test_download_videos_does_not_auto_add_openmontage_output(self):
        openmontage_path = (
            "C:/A/money/OpenMontage/projects/money-printing-inflation/"
            "final_silent_9x16_1080p.mp4"
        )
        remote_item = MaterialInfo(
            provider="pexels",
            url="https://videos.example/clip.mp4",
            duration=5,
            width=1080,
            height=1920,
        )

        with (
            patch.object(
                material,
                "find_openmontage_output",
                return_value=openmontage_path,
                create=True,
            ) as find_openmontage,
            patch.object(material, "search_videos_pexels", return_value=[remote_item]),
            patch.object(material, "save_video", return_value="C:/task/remote.mp4"),
        ):
            result = material.download_videos(
                task_id="openmontage-selection",
                search_terms=["money printing inflation"],
                source="pexels",
                audio_duration=5,
                max_clip_duration=5,
            )

        self.assertEqual(result, ["C:/task/remote.mp4"])
        find_openmontage.assert_not_called()


class TestMaterialVisualQuality(unittest.TestCase):
    class _FakeClip:
        duration = 6.0

        def __init__(self, frame):
            self.frame = frame

        def get_frame(self, _timestamp):
            return self.frame

    def test_visual_quality_filter_rejects_nearly_black_flat_clip(self):
        black_frame = np.zeros((12, 12, 3), dtype=np.uint8)

        with patch.dict(
            config.app,
            {"video_visual_quality_filter_enabled": True},
            clear=False,
        ):
            acceptable = material._is_video_clip_visually_acceptable(
                self._FakeClip(black_frame)
            )

        self.assertFalse(acceptable)

    def test_visual_quality_filter_rejects_frozen_detailed_clip(self):
        checkerboard = (np.indices((12, 12)).sum(axis=0) % 2 * 255).astype(
            np.uint8
        )
        detailed_frame = np.stack([checkerboard] * 3, axis=-1)

        with patch.dict(
            config.app,
            {"video_visual_quality_filter_enabled": True},
            clear=False,
        ):
            acceptable = material._is_video_clip_visually_acceptable(
                self._FakeClip(detailed_frame)
            )

        self.assertFalse(acceptable)

    def test_visual_quality_filter_accepts_bright_moving_clip(self):
        gradient = np.tile(np.arange(12, dtype=np.uint8) * 20, (12, 1))
        detailed_frame = np.stack([gradient] * 3, axis=-1)

        class MovingClip:
            duration = 6.0

            def get_frame(self, timestamp):
                return np.roll(detailed_frame, int(round(timestamp * 3)), axis=1)

        with patch.dict(
            config.app,
            {"video_visual_quality_filter_enabled": True},
            clear=False,
        ):
            acceptable = material._is_video_clip_visually_acceptable(MovingClip())

        self.assertTrue(acceptable)

    def test_visual_quality_filter_rejects_frozen_slideshow_sequence(self):
        checkerboard = (np.indices((12, 12)).sum(axis=0) % 2 * 255).astype(
            np.uint8
        )
        first_slide = np.stack([checkerboard] * 3, axis=-1)
        second_slide = np.stack([255 - checkerboard] * 3, axis=-1)

        class SlideshowClip:
            duration = 6.0

            def get_frame(self, timestamp):
                if timestamp < 2.5:
                    return first_slide
                if timestamp < 4.5:
                    return second_slide
                return first_slide

        with patch.dict(
            config.app,
            {"video_visual_quality_filter_enabled": True},
            clear=False,
        ):
            acceptable = material._is_video_clip_visually_acceptable(SlideshowClip())

        self.assertFalse(acceptable)

    def test_visual_quality_filter_fails_open_when_frame_read_fails(self):
        class BrokenClip:
            duration = 6.0

            def get_frame(self, _timestamp):
                raise OSError("decoder unavailable")

        with patch.dict(
            config.app,
            {"video_visual_quality_filter_enabled": True},
            clear=False,
        ):
            acceptable = material._is_video_clip_visually_acceptable(BrokenClip())

        self.assertTrue(acceptable)

    def test_visual_quality_filter_accepts_mixed_samples_after_decode_failure(self):
        black_frame = np.zeros((12, 12, 3), dtype=np.uint8)
        checkerboard = (np.indices((12, 12)).sum(axis=0) % 2 * 255).astype(
            np.uint8
        )
        detailed_frame = np.stack([checkerboard] * 3, axis=-1)

        class PartlyReadableClip:
            duration = 6.0

            def get_frame(self, timestamp):
                if timestamp > 4:
                    raise OSError("late frame unavailable")
                return black_frame if timestamp < 2 else detailed_frame

        with patch.dict(
            config.app,
            {"video_visual_quality_filter_enabled": True},
            clear=False,
        ):
            acceptable = material._is_video_clip_visually_acceptable(
                PartlyReadableClip()
            )

        self.assertTrue(acceptable)

    def test_save_video_rejects_visually_weak_material(self):
        black_frame = np.zeros((12, 12, 3), dtype=np.uint8)
        fake_response = SimpleNamespace(content=b"fake-video")

        class WeakVideoFileClip(self._FakeClip):
            duration = 6.0
            fps = 24

            def __init__(self, _path):
                super().__init__(black_frame)

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.dict(
                    config.app,
                    {"video_visual_quality_filter_enabled": True},
                    clear=False,
                ),
                patch.object(material.requests, "get", return_value=fake_response),
                patch.object(material, "VideoFileClip", WeakVideoFileClip),
            ):
                video_path = material.save_video(
                    "https://example.com/weak.mp4",
                    save_dir=temp_dir,
                )

        self.assertEqual(video_path, "")


class TestMaterialPreviewQuality(unittest.TestCase):
    @staticmethod
    def _preview_response(pixels):
        image = Image.fromarray(pixels.astype(np.uint8))
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        return SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "image/png"},
            content=stream.getvalue(),
        )

    def test_preview_quality_score_rejects_dark_flat_preview(self):
        item = MaterialInfo(
            provider="pexels",
            url="https://example.com/video.mp4",
            preview_url="https://example.com/preview.png",
        )
        black_preview = np.zeros((24, 24, 3), dtype=np.uint8)

        with patch.object(
            material.requests,
            "get",
            return_value=self._preview_response(black_preview),
        ) as get:
            score = material._preview_visual_quality_score(item)

        self.assertIsNotNone(score)
        self.assertLess(score, material._MIN_PREVIEW_QUALITY_SCORE)
        self.assertTrue(get.call_args.kwargs["verify"])

    def test_preview_fields_do_not_shift_legacy_positional_values(self):
        item = MaterialInfo("pexels", "https://example.com/video.mp4", 5, 1080, 1920)

        self.assertEqual(item.duration, 5)
        self.assertEqual(item.width, 1080)
        self.assertEqual(item.height, 1920)
        self.assertEqual(item.preview_url, "")

    def test_preview_quality_score_accepts_detailed_preview(self):
        item = MaterialInfo(
            provider="pexels",
            url="https://example.com/video.mp4",
            preview_url="https://example.com/preview.png",
        )
        checkerboard = (np.indices((24, 24)).sum(axis=0) % 2 * 255).astype(
            np.uint8
        )
        detailed_preview = np.stack([checkerboard] * 3, axis=-1)

        with patch.object(
            material.requests,
            "get",
            return_value=self._preview_response(detailed_preview),
        ):
            score = material._preview_visual_quality_score(item)

        self.assertIsNotNone(score)
        self.assertGreaterEqual(score, material._MIN_PREVIEW_QUALITY_SCORE)

    def test_preview_quality_rerank_is_opt_in_and_bounded(self):
        items = [
            MaterialInfo(
                provider="pexels",
                url=f"https://example.com/video-{index}.mp4",
                preview_url=f"https://example.com/preview-{index}.jpg",
                duration=8,
                width=1080,
                height=1920,
                search_query="city market",
                title="City market",
            )
            for index in range(4)
        ]

        with (
            patch.dict(
                config.app,
                {
                    "preview_quality_rerank_enabled": True,
                    "preview_quality_rerank_max_candidates": 3,
                },
                clear=False,
            ),
            patch.object(
                material,
                "_preview_visual_quality_score",
                side_effect=[0.35, 0.95, 0.65],
            ) as preview_score,
            patch.object(material, "_score_material", return_value=0.7),
        ):
            reranked = material._rerank_materials_with_preview_quality(
                items,
                max_clip_duration=5,
                video_aspect=VideoAspect.portrait,
            )

        self.assertEqual(
            [item.url for item in reranked],
            [items[1].url, items[2].url, items[0].url, items[3].url],
        )
        self.assertEqual(preview_score.call_count, 3)

    def test_preview_quality_rerank_preserves_unscored_candidates(self):
        items = [
            MaterialInfo(
                provider="pexels",
                url=f"https://example.com/video-{index}.mp4",
                preview_url=f"https://example.com/preview-{index}.jpg",
                duration=8,
                width=1080,
                height=1920,
                search_query="city market",
                title="City market",
            )
            for index in range(3)
        ]

        with (
            patch.dict(
                config.app,
                {
                    "preview_quality_rerank_enabled": True,
                    "preview_quality_rerank_max_candidates": 3,
                },
                clear=False,
            ),
            patch.object(
                material,
                "_preview_visual_quality_score",
                side_effect=[None, 0.35, 0.95],
            ),
            patch.object(material, "_score_material", return_value=0.7),
        ):
            reranked = material._rerank_materials_with_preview_quality(
                items,
                max_clip_duration=5,
                video_aspect=VideoAspect.portrait,
            )

        self.assertEqual(
            [item.url for item in reranked],
            [items[0].url, items[2].url, items[1].url],
        )

    def test_preview_quality_rerank_does_not_fetch_when_disabled(self):
        items = [
            MaterialInfo(
                provider="pexels",
                url=f"https://example.com/video-{index}.mp4",
                preview_url=f"https://example.com/preview-{index}.jpg",
                duration=8,
                width=1080,
                height=1920,
            )
            for index in range(2)
        ]

        with (
            patch.dict(
                config.app,
                {"preview_quality_rerank_enabled": False},
                clear=False,
            ),
            patch.object(material, "_preview_visual_quality_score") as preview_score,
        ):
            reranked = material._rerank_materials_with_preview_quality(
                items,
                max_clip_duration=5,
                video_aspect=VideoAspect.portrait,
            )

        self.assertEqual(reranked, items)
        preview_score.assert_not_called()

    def test_candidate_search_does_not_trigger_preview_quality_rerank(self):
        item = MaterialInfo(
            provider="pexels",
            url="https://example.com/video.mp4",
            preview_url="https://example.com/preview.jpg",
            duration=8,
            width=1080,
            height=1920,
        )

        with (
            patch.dict(
                config.app,
                {"preview_quality_rerank_enabled": True},
                clear=False,
            ),
            patch.object(material, "search_videos_pexels", return_value=[item]),
            patch.object(
                material,
                "_rerank_materials_with_twelvelabs",
                side_effect=lambda candidates: candidates,
            ),
            patch.object(
                material,
                "_rerank_materials_with_twelvelabs_visual",
                side_effect=lambda candidates: candidates,
            ),
            patch.object(
                material,
                "_screen_twelvelabs_material_candidates",
                side_effect=lambda candidates: candidates,
            ),
            patch.object(
                material,
                "_rerank_materials_with_preview_quality",
            ) as preview_rerank,
        ):
            candidates = material.search_video_candidates(
                ["city market"],
                source="pexels",
                max_clip_duration=5,
            )

        self.assertEqual(candidates, [item])
        preview_rerank.assert_not_called()

    def test_download_path_uses_preview_quality_rerank_before_saving(self):
        item = MaterialInfo(
            provider="pexels",
            url="https://example.com/video.mp4",
            preview_url="https://example.com/preview.jpg",
            duration=8,
            width=1080,
            height=1920,
        )

        with (
            patch.dict(
                config.app,
                {
                    "photo_fallback_enabled": False,
                    "material_directory": "",
                    "preview_quality_rerank_enabled": True,
                },
                clear=False,
            ),
            patch.object(material, "search_videos_pexels", return_value=[item]),
            patch.object(
                material,
                "_screen_twelvelabs_material_candidates",
                side_effect=lambda candidates: candidates,
            ),
            patch.object(
                material,
                "_rerank_materials_with_preview_quality",
                return_value=[item],
            ) as preview_rerank,
            patch.object(
                material,
                "_save_ranked_material",
                return_value="/tmp/video.mp4",
            ),
            patch.object(material, "_mark_video_cooldown_used"),
        ):
            video_paths = material.download_videos(
                task_id="preview-rerank",
                search_terms=["city market"],
                source="pexels",
                audio_duration=1,
                max_clip_duration=5,
            )

        self.assertEqual(video_paths, ["/tmp/video.mp4"])
        preview_rerank.assert_called_once_with(
            [item],
            max_clip_duration=5,
            video_aspect=VideoAspect.portrait,
        )

    def test_weak_preview_skips_video_download(self):
        item = MaterialInfo(
            provider="pexels",
            url="https://example.com/video.mp4",
            preview_url="https://example.com/preview.png",
            preview_quality_score=0.1,
        )

        with patch.object(material, "save_video") as save_video:
            saved_path = material._save_ranked_material(item, "/tmp")

        self.assertEqual(saved_path, "")
        save_video.assert_not_called()

    def test_automatic_download_skips_weak_preview_before_video_request(self):
        item = MaterialInfo(
            provider="pexels",
            url="https://example.com/video.mp4",
            preview_url="https://example.com/preview.png",
            preview_quality_score=0.1,
            duration=5,
        )

        with (
            patch.dict(
                config.app,
                {"photo_fallback_enabled": False, "material_directory": ""},
                clear=False,
            ),
            patch.object(material, "search_videos_pexels", return_value=[item]),
            patch.object(material, "save_video") as save_video,
        ):
            video_paths = material.download_videos(
                task_id="weak-preview",
                search_terms=["city scene"],
                source="pexels",
                audio_duration=1,
                max_clip_duration=5,
            )

        self.assertEqual(video_paths, [])
        save_video.assert_not_called()

    def test_missing_preview_fails_open_before_video_download(self):
        item = MaterialInfo(
            provider="pixabay",
            url="https://example.com/video.mp4",
        )

        with patch.object(
            material,
            "save_video",
            return_value="/tmp/video.mp4",
        ) as save_video:
            saved_path = material._save_ranked_material(item, "/tmp")

        self.assertEqual(saved_path, "/tmp/video.mp4")
        save_video.assert_called_once_with(
            video_url="https://example.com/video.mp4",
            save_dir="/tmp",
            minimum_duration=0.0,
        )

    def test_unreadable_preview_fails_open_before_video_download(self):
        item = MaterialInfo(
            provider="pexels",
            url="https://example.com/video.mp4",
            preview_url="https://example.com/preview.png",
        )
        unavailable_preview = SimpleNamespace(status_code=503, headers={}, content=b"")

        with (
            patch.object(
                material.requests,
                "get",
                return_value=unavailable_preview,
            ),
            patch.object(
                material,
                "save_video",
                return_value="/tmp/video.mp4",
            ) as save_video,
        ):
            saved_path = material._save_ranked_material(item, "/tmp")

        self.assertEqual(saved_path, "/tmp/video.mp4")
        save_video.assert_called_once_with(
            video_url="https://example.com/video.mp4",
            save_dir="/tmp",
            minimum_duration=0.0,
        )


class TestMaterialReviewFeedback(unittest.TestCase):
    def test_score_material_applies_bounded_provider_feedback_penalty(self):
        item = MaterialInfo(
            provider="pexels",
            url="https://example.com/video.mp4",
            duration=8,
            width=1080,
            height=1920,
            search_query="economic market",
            title="Economic market overview",
        )

        with patch.object(
            material.review_feedback,
            "get_provider_feedback_score_adjustment",
            return_value=0.0,
        ):
            baseline = material._score_material(item, max_clip_duration=5)
        with patch.object(
            material.review_feedback,
            "get_provider_feedback_score_adjustment",
            return_value=-0.08,
        ):
            adjusted = material._score_material(item, max_clip_duration=5)

        self.assertAlmostEqual(adjusted, baseline - 0.08)


class TestMaterialResolutionRanking(unittest.TestCase):
    def test_known_native_resolution_ranks_ahead_of_unknown_resolution(self):
        unknown_size = MaterialInfo(
            provider="pexels",
            url="https://example.com/unknown-size.mp4",
            duration=8,
            search_query="economic market",
            title="Economic market overview",
        )
        native_portrait = MaterialInfo(
            provider="pexels",
            url="https://example.com/native-portrait.mp4",
            duration=8,
            width=1080,
            height=1920,
            search_query="economic market",
            title="Economic market overview",
        )

        with patch.dict(
            config.app,
            {
                "video_cooldown_enabled": False,
                "twelvelabs_material_rerank_enabled": False,
                "twelvelabs_visual_rerank_enabled": False,
                "twelvelabs_clip_qa_enabled": False,
            },
            clear=False,
        ):
            ranked = material._rank_materials(
                [unknown_size, native_portrait],
                max_clip_duration=5,
                video_aspect=VideoAspect.portrait,
            )

        self.assertEqual(
            [item.url for item in ranked],
            [native_portrait.url, unknown_size.url],
        )


class TestTwelveLabsCandidateScreening(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)

    def test_clip_qa_rejects_only_explicitly_rejected_candidates_within_budget(self):
        config.app["twelvelabs_clip_qa_enabled"] = True
        config.app["twelvelabs_clip_qa_max_candidates"] = 2
        candidates = [
            MaterialInfo(
                provider="pexels",
                url="https://example.com/reject.mp4",
                search_query="city skyline",
            ),
            MaterialInfo(
                provider="pexels",
                url="https://example.com/unknown.mp4",
                search_query="city skyline",
            ),
            MaterialInfo(
                provider="pexels",
                url="https://example.com/unreviewed.mp4",
                search_query="city skyline",
            ),
        ]

        with patch(
            "app.services.twelvelabs.clip_relevance_verdict",
            side_effect=[False, None],
        ) as verdict:
            screened = material._screen_twelvelabs_material_candidates(candidates)

        self.assertEqual(
            [candidate.url for candidate in screened],
            [
                "https://example.com/unknown.mp4",
                "https://example.com/unreviewed.mp4",
            ],
        )
        self.assertEqual(verdict.call_count, 2)

    def test_clip_qa_skips_local_urls_without_consuming_public_review_budget(self):
        config.app["twelvelabs_clip_qa_enabled"] = True
        config.app["twelvelabs_clip_qa_max_candidates"] = 1
        candidates = [
            MaterialInfo(
                provider="local",
                url="C:/local/clip.mp4",
                search_query="city skyline",
            ),
            MaterialInfo(
                provider="pexels",
                url="https://example.com/public.mp4",
                search_query="city skyline",
            ),
        ]

        with patch(
            "app.services.twelvelabs.clip_relevance_verdict",
            return_value=False,
        ) as verdict:
            screened = material._screen_twelvelabs_material_candidates(candidates)

        self.assertEqual([candidate.url for candidate in screened], ["C:/local/clip.mp4"])
        verdict.assert_called_once_with(
            "https://example.com/public.mp4",
            "city skyline",
        )


class TestTwelveLabsMaterialReranking(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        config.app["twelvelabs_api_keys"] = ["tlk_test"]
        config.app["twelvelabs_rerank_terms"] = True
        config.app["twelvelabs_material_rerank_enabled"] = True
        config.app["twelvelabs_material_rerank_max_candidates"] = 2
        config.app["twelvelabs_visual_rerank_enabled"] = False
        config.app["twelvelabs_clip_qa_enabled"] = False
        config.app["video_cooldown_enabled"] = False

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)

    @staticmethod
    def _candidates():
        return [
            MaterialInfo(
                provider="pexels",
                url="https://example.com/mountains.mp4",
                duration=5,
                width=1080,
                height=1920,
                search_query="lower household costs",
                title="Mountain valley at sunrise",
            ),
            MaterialInfo(
                provider="wikimedia",
                url="https://example.com/groceries.mp4",
                duration=5,
                width=1080,
                height=1920,
                search_query="lower household costs",
                title="Family shopping for groceries",
            ),
        ]

    def test_rank_materials_uses_twelvelabs_to_rerank_semantic_matches(self):
        candidates = self._candidates()

        with patch(
            "app.services.twelvelabs.semantic_text_similarity",
            side_effect=[0.10, 0.95],
        ) as similarity:
            ranked = material._rank_materials(candidates, max_clip_duration=5)

        self.assertEqual(ranked[0].url, "https://example.com/groceries.mp4")
        self.assertEqual(similarity.call_count, 2)


    def test_material_rerank_preserves_baseline_when_similarity_is_unavailable(self):
        candidates = self._candidates()

        with patch(
            "app.services.twelvelabs.semantic_text_similarity",
            return_value=None,
        ):
            ranked = material._rank_materials(candidates, max_clip_duration=5)

        self.assertEqual(ranked[0].url, "https://example.com/mountains.mp4")

    def test_material_rerank_respects_candidate_budget(self):
        candidates = self._candidates()
        config.app["twelvelabs_material_rerank_max_candidates"] = 1

        with patch(
            "app.services.twelvelabs.semantic_text_similarity",
            return_value=0.95,
        ) as similarity:
            ranked = material._rank_materials(candidates, max_clip_duration=5)

        self.assertEqual(ranked[0].url, "https://example.com/mountains.mp4")
        similarity.assert_called_once()

    def test_material_rerank_requires_its_explicit_opt_in(self):
        candidates = self._candidates()
        config.app.pop("twelvelabs_material_rerank_enabled", None)

        with patch(
            "app.services.twelvelabs.semantic_text_similarity",
            return_value=0.95,
        ) as similarity:
            ranked = material._rank_materials(candidates, max_clip_duration=5)

        self.assertEqual(ranked[0].url, "https://example.com/mountains.mp4")
        similarity.assert_not_called()

    def test_material_rerank_uses_twelvelabs_visual_similarity_for_video_urls(self):
        candidates = self._candidates()
        config.app["twelvelabs_material_rerank_enabled"] = False
        config.app["twelvelabs_visual_rerank_enabled"] = True
        config.app["twelvelabs_visual_rerank_max_candidates"] = 2

        with patch(
            "app.services.twelvelabs.visual_video_similarity",
            side_effect=[0.10, 0.95],
        ) as similarity:
            ranked = material._rank_materials(candidates, max_clip_duration=5)

        self.assertEqual(ranked[0].url, "https://example.com/groceries.mp4")
        self.assertEqual(similarity.call_count, 2)


class TestVecteezyProvider(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)
        config.app["video_cooldown_enabled"] = False
        config.app["photo_fallback_enabled"] = False
        config.proxy.clear()

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    def test_search_returns_commercial_video_candidates_without_storing_previews(self):
        config.app["vecteezy_api_keys"] = ["vecteezy-key"]
        config.app["vecteezy_account_id"] = "42"
        response = SimpleNamespace(
            json=lambda: {
                "resources": [
                    {
                        "id": 900,
                        "content_type": "video",
                        "title": "Portrait business meeting",
                        "file_metadata": {
                            "available_file_types": [{"extension": "mp4"}],
                        },
                    },
                    {"id": 901, "content_type": "photo"},
                ]
            }
        )

        with patch(
            "app.services.providers.vecteezy.requests.get",
            return_value=response,
        ) as get:
            results = VecteezyProvider().search(
                "business meeting",
                minimum_duration=5,
            )

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.provider, "vecteezy")
        self.assertEqual(item.url, "vecteezy-resource://900")
        self.assertEqual(item.duration, 5)
        self.assertEqual(item.title, "Portrait business meeting")
        self.assertEqual(item.license, "Vecteezy Free License")
        self.assertEqual(item.preview_url, "")
        self.assertEqual(item.search_query, "business meeting")
        self.assertEqual(
            get.call_args.args[0],
            "https://api.vecteezy.com/v2/42/resources",
        )
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"],
            "Bearer vecteezy-key",
        )
        self.assertEqual(get.call_args.kwargs["params"]["term"], "business meeting")
        self.assertEqual(get.call_args.kwargs["params"]["content_type"], "video")
        self.assertEqual(get.call_args.kwargs["params"]["license_type"], "commercial")
        self.assertTrue(get.call_args.kwargs["params"]["family_friendly"])
        self.assertEqual(get.call_args.kwargs["params"]["duration"], "5_3600")
        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_rejects_explicitly_noncommercial_resources(self):
        config.app["vecteezy_api_keys"] = ["vecteezy-key"]
        config.app["vecteezy_account_id"] = "42"
        response = SimpleNamespace(
            json=lambda: {
                "resources": [
                    {
                        "id": 900,
                        "content_type": "video",
                        "license_type": "editorial",
                        "file_metadata": {
                            "available_file_types": [{"extension": "mp4"}],
                        },
                    },
                    {
                        "id": 901,
                        "content_type": "video",
                        "license_type": "commercial",
                        "file_metadata": {
                            "available_file_types": [{"extension": "mp4"}],
                        },
                    },
                ]
            }
        )

        with patch(
            "app.services.providers.vecteezy.requests.get",
            return_value=response,
        ):
            results = VecteezyProvider().search("business meeting", minimum_duration=5)

        self.assertEqual([item.url for item in results], ["vecteezy-resource://901"])

    def test_resolve_download_url_records_required_attribution(self):
        config.app["vecteezy_api_keys"] = ["vecteezy-key"]
        config.app["vecteezy_account_id"] = "42"
        item = MaterialInfo(
            provider="vecteezy",
            url="vecteezy-resource://900",
            license="Vecteezy Free License",
        )
        response = SimpleNamespace(
            json=lambda: {
                "url": "https://downloads.example.com/900.mp4?temporary=true",
                "requires_attribution": True,
                "required_attribution_url": "https://www.vecteezy.com/attribution/900",
            }
        )

        with patch(
            "app.services.providers.vecteezy.requests.get",
            return_value=response,
        ) as get:
            download_url = resolve_vecteezy_download_url(item)

        self.assertEqual(
            download_url,
            "https://downloads.example.com/900.mp4?temporary=true",
        )
        self.assertEqual(
            item.attribution,
            "Vecteezy attribution: https://www.vecteezy.com/attribution/900",
        )
        self.assertEqual(
            get.call_args.args[0],
            "https://api.vecteezy.com/v2/42/resources/900/download",
        )
        self.assertEqual(get.call_args.kwargs["params"], {"file_type": "mp4"})
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"],
            "Bearer vecteezy-key",
        )

    def test_resolve_download_url_rejects_unrecordable_required_attribution(self):
        config.app["vecteezy_api_keys"] = ["vecteezy-key"]
        config.app["vecteezy_account_id"] = "42"
        item = MaterialInfo(provider="vecteezy", url="vecteezy-resource://900")
        response = SimpleNamespace(
            json=lambda: {
                "url": "https://downloads.example.com/900.mp4",
                "requires_attribution": True,
                "required_attribution_url": "",
            }
        )

        with patch(
            "app.services.providers.vecteezy.requests.get",
            return_value=response,
        ):
            self.assertEqual(resolve_vecteezy_download_url(item), "")

    def test_download_selected_videos_resolves_vecteezy_only_when_selected(self):
        item = MaterialInfo(
            provider="vecteezy",
            url="vecteezy-resource://900",
            duration=5,
        )

        with (
            patch(
                "app.services.providers.vecteezy.resolve_vecteezy_download_url",
                return_value="https://downloads.example.com/900.mp4",
            ) as resolve_download,
            patch.object(material, "save_video", return_value="C:/cache/900.mp4") as save,
        ):
            results = material.download_selected_videos(
                "vecteezy-selected",
                [item],
            )

        self.assertEqual(results, ["C:/cache/900.mp4"])
        resolve_download.assert_called_once_with(item)
        save.assert_called_once()
        self.assertEqual(save.call_args.kwargs["minimum_duration"], 5.0)

    def test_download_videos_routes_single_vecteezy_source_through_deferred_download(self):
        config.app["vecteezy_api_keys"] = ["vecteezy-key"]
        config.app["vecteezy_account_id"] = "42"
        search_response = SimpleNamespace(
            json=lambda: {
                "resources": [
                    {
                        "id": 900,
                        "content_type": "video",
                        "file_metadata": {
                            "available_file_types": [{"extension": "mp4"}],
                        },
                    }
                ]
            }
        )

        with (
            patch(
                "app.services.providers.vecteezy.requests.get",
                return_value=search_response,
            ),
            patch(
                "app.services.providers.vecteezy.resolve_vecteezy_download_url",
                return_value="https://downloads.example.com/900.mp4",
            ) as resolve_download,
            patch.object(material, "save_video", return_value="C:/cache/900.mp4"),
        ):
            results = material.download_videos(
                "vecteezy-single-source",
                ["business meeting"],
                source="vecteezy",
                audio_duration=1,
                max_clip_duration=5,
            )

        self.assertEqual(results, ["C:/cache/900.mp4"])
        resolve_download.assert_called_once()

    def test_save_video_does_not_log_temporary_download_url_on_request_failure(self):
        temporary_url = "https://downloads.example.com/900.mp4?signature=private"

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(
                material.requests,
                "get",
                side_effect=requests.RequestException(temporary_url),
            ),
            patch.object(material.logger, "warning") as warning,
        ):
            self.assertEqual(material.save_video(temporary_url, temp_dir), "")

        self.assertNotIn(temporary_url, str(warning.call_args))

    def test_short_download_is_rejected_before_it_can_fill_the_duration_budget(self):
        short_clip = SimpleNamespace(
            duration=4.0,
            fps=30,
            close=lambda: None,
        )

        with patch.object(material, "VideoFileClip", return_value=short_clip):
            self.assertFalse(
                material._is_saved_video_usable(
                    "C:/cache/short-vecteezy.mp4",
                    minimum_duration=5,
                )
            )

    def test_is_available_requires_both_api_key_and_account_id(self):
        config.app["vecteezy_api_keys"] = ["vecteezy-key"]
        config.app["vecteezy_account_id"] = ""
        self.assertFalse(VecteezyProvider().is_available())

        config.app["vecteezy_account_id"] = "42"
        self.assertTrue(VecteezyProvider().is_available())

        config.app["vecteezy_api_keys"] = [""]
        self.assertFalse(VecteezyProvider().is_available())


class TestNOAAOceanExplorationProvider(unittest.TestCase):
    def test_search_returns_only_public_domain_noaa_mp4_candidates(self):
        response = SimpleNamespace(
            json=lambda: {
                "results": [
                    {
                        "title": "Video segment recorded for 299 seconds",
                        "description": "An octopus observed during a deep-sea dive.",
                        "_source": {
                            "links_s": [
                                "https://untrusted.example.com/clip.mp4",
                                (
                                    "https://data.nodc.noaa.gov/oer/video/"
                                    "EX2206/Video/clip_Low.mp4"
                                ),
                            ],
                            "thumbnail_s": (
                                "https://data.nodc.noaa.gov/oer/video/"
                                "EX2206/Imagery/clip.jpg"
                            ),
                            "keywords_s": ["OCTOPUS", "CEPHALOPOD"],
                        },
                    }
                ]
            }
        )

        with patch(
            "app.services.providers.noaa_ocean.requests.get",
            return_value=response,
        ) as get:
            results = NOAOOceanExplorationProvider().search(
                "deep sea octopus",
                minimum_duration=5,
            )

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.provider, "noaa_ocean")
        self.assertEqual(
            item.url,
            "https://data.nodc.noaa.gov/oer/video/EX2206/Video/clip_Low.mp4",
        )
        self.assertEqual(item.duration, 299)
        self.assertEqual(item.attribution, "NOAA Ocean Exploration")
        self.assertEqual(item.license, "NOAA Ocean Exploration Public Domain")
        self.assertEqual(item.tags, ["OCTOPUS", "CEPHALOPOD"])
        self.assertEqual(
            get.call_args.kwargs["params"]["q"],
            '("deep*" AND "sea*" AND "octopus*") AND (NOT (STREAM) AND NOT (HIGHLIGHT))',
        )
        self.assertEqual(get.call_args.kwargs["params"]["f"], "pjson")

    def test_search_uses_requested_duration_when_noaa_duration_metadata_is_missing(self):
        response = SimpleNamespace(
            json=lambda: {
                "results": [
                    {
                        "title": "Deep-sea exploration footage",
                        "description": "Public-domain underwater footage.",
                        "_source": {
                            "links_s": [
                                "https://data.nodc.noaa.gov/oer/video/"
                                "EX2206/Video/clip_Low.mp4"
                            ]
                        },
                    }
                ]
            }
        )

        with patch(
            "app.services.providers.noaa_ocean.requests.get",
            return_value=response,
        ):
            results = NOAOOceanExplorationProvider().search(
                "underwater footage",
                minimum_duration=7,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].duration, 7)

    def test_save_provider_material_verifies_noaa_requested_duration_after_download(self):
        item = MaterialInfo(
            provider="noaa_ocean",
            url="https://data.nodc.noaa.gov/oer/video/EX2206/Video/clip_Low.mp4",
            duration=7,
        )

        with patch.object(
            material,
            "save_video",
            return_value="C:/cache/noaa.mp4",
        ) as save_video:
            saved_path = material._save_provider_material(item, "C:/cache")

        self.assertEqual(saved_path, "C:/cache/noaa.mp4")
        save_video.assert_called_once_with(
            video_url=item.url,
            save_dir="C:/cache",
            minimum_duration=7.0,
        )

    def test_search_rejects_short_copyrighted_or_untrusted_results(self):
        response = SimpleNamespace(
            json=lambda: {
                "results": [
                    {
                        "title": "Video segment recorded for 299 seconds",
                        "description": "Malformed link metadata.",
                        "_source": {"links_s": None},
                    },
                    {
                        "title": "Video segment recorded for 4 seconds",
                        "description": "Too short.",
                        "_source": {
                            "links_s": [
                                "https://www.ncei.noaa.gov/data/oceans/oer/video/short.mp4"
                            ]
                        },
                    },
                    {
                        "title": "Video segment recorded for 299 seconds",
                        "description": "Copyrighted third-party material.",
                        "_source": {
                            "links_s": [
                                "https://www.ncei.noaa.gov/data/oceans/oer/video/copyright.mp4"
                            ]
                        },
                    },
                    {
                        "title": "Video segment recorded for 299 seconds",
                        "description": "Good candidate.",
                        "_source": {
                            "links_s": ["https://cdn.example.com/clip.mp4"]
                        },
                    },
                ]
            }
        )

        with patch(
            "app.services.providers.noaa_ocean.requests.get",
            return_value=response,
        ):
            results = NOAOOceanExplorationProvider().search("octopus", minimum_duration=5)

        self.assertEqual(results, [])


class TestLibraryOfCongressProvider(unittest.TestCase):
    def test_search_accepts_explicit_public_domain_and_prefers_matching_aspect(self):
        search_response = SimpleNamespace(
            json=lambda: {
                "results": [
                    {
                        "id": "http://www.loc.gov/item/00694425/",
                        "title": "Historic economic activity",
                    }
                ]
            }
        )
        item_response = SimpleNamespace(
            json=lambda: {
                "item": {
                    "access_restricted": False,
                    "rights": [
                        "<p>This collection is in the public domain and free to use and reuse.</p>"
                    ],
                    "title": "Historic economic activity",
                    "description": ["Archival footage of a busy street market."],
                    "subject": ["economy", "history"],
                    "url": "http://www.loc.gov/item/00694425/",
                },
                "resources": [
                    {
                        "duration": 30,
                        "width": 3840,
                        "height": 2160,
                        "video": (
                            "https://tile.loc.gov/storage-services/service/mbrs/"
                            "loc/landscape.mp4"
                        ),
                        "image": (
                            "https://tile.loc.gov/storage-services/service/mbrs/"
                            "loc/landscape.jpg"
                        ),
                        "download_restricted": False,
                    },
                    {
                        "duration": 15,
                        "width": 1080,
                        "height": 1920,
                        "video": (
                            "https://tile.loc.gov/storage-services/service/mbrs/"
                            "loc/portrait.mp4"
                        ),
                        "image": (
                            "https://tile.loc.gov/storage-services/service/mbrs/"
                            "loc/portrait.jpg"
                        ),
                        "download_restricted": False,
                    },
                ],
            }
        )

        with patch(
            "app.services.providers.loc.requests.get",
            side_effect=[search_response, item_response],
        ) as get:
            results = LibraryOfCongressProvider().search(
                "Economic history!!!",
                minimum_duration=5,
                video_aspect=VideoAspect.portrait,
            )

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.provider, "loc")
        self.assertTrue(item.url.endswith("/portrait.mp4"))
        self.assertEqual((item.width, item.height), (1080, 1920))
        self.assertEqual(item.duration, 15)
        self.assertEqual(item.search_query, "Economic history!!!")
        self.assertEqual(item.description, "Archival footage of a busy street market.")
        self.assertEqual(item.tags, ["economy", "history"])
        self.assertEqual(item.license, "Library of Congress Public Domain")
        self.assertTrue(item.license_url.startswith("https://www.loc.gov/item/"))
        self.assertIn("Library of Congress", item.attribution)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["q"], "economic history")
        self.assertEqual(
            get.call_args_list[0].kwargs["headers"]["User-Agent"],
            "MoneyPrinterTurbo public-domain material search",
        )

    def test_search_rejects_unclear_restricted_or_untrusted_items(self):
        search_response = SimpleNamespace(
            json=lambda: {
                "results": [
                    {"id": "http://www.loc.gov/item/one/"},
                    {"id": "http://www.loc.gov/item/two/"},
                    {"id": "http://www.loc.gov/item/three/"},
                    {"id": "http://www.loc.gov/item/four/"},
                ]
            }
        )

        def item_response(*, rights, access_restricted=False, video_url=""):
            return SimpleNamespace(
                json=lambda: {
                    "item": {
                        "access_restricted": access_restricted,
                        "rights": rights,
                        "url": "http://www.loc.gov/item/example/",
                    },
                    "resources": [
                        {
                            "duration": 20,
                            "width": 1920,
                            "height": 1080,
                            "video": video_url,
                            "download_restricted": False,
                        }
                    ],
                }
            )

        with patch("app.services.providers.loc._MAX_ITEM_LOOKUPS", 4), patch(
            "app.services.providers.loc.requests.get",
            side_effect=[
                search_response,
                item_response(
                    rights=["Copyright restrictions may apply."],
                    video_url=(
                        "https://tile.loc.gov/storage-services/service/mbrs/"
                        "loc/unclear.mp4"
                    ),
                ),
                item_response(
                    rights=["This collection is in the public domain."],
                    access_restricted=True,
                    video_url=(
                        "https://tile.loc.gov/storage-services/service/mbrs/"
                        "loc/restricted.mp4"
                    ),
                ),
                item_response(
                    rights=["This collection is in the public domain."],
                    video_url="https://untrusted.example.com/clip.mp4",
                ),
                item_response(
                    rights=[
                        "Some material is in the public domain, but written permission "
                        "of copyright owners is required."
                    ],
                    video_url=(
                        "https://tile.loc.gov/storage-services/service/mbrs/"
                        "loc/mixed-rights.mp4"
                    ),
                ),
            ],
        ):
            results = LibraryOfCongressProvider().search(
                "history",
                minimum_duration=5,
            )

        self.assertEqual(results, [])

    def test_search_accepts_a_downloadable_nested_mp4_file(self):
        search_response = SimpleNamespace(
            json=lambda: {
                "results": [{"id": "http://www.loc.gov/item/00694425/"}]
            }
        )
        item_response = SimpleNamespace(
            json=lambda: {
                "item": {
                    "access_restricted": False,
                    "rights": ["This collection is in the public domain."],
                    "url": "http://www.loc.gov/item/00694425/",
                },
                    "resources": [
                        {
                            "duration": None,
                            "width": None,
                            "height": None,
                            "download_restricted": False,
                            "files": [
                                [
                                    {
                                        "canDownload": True,
                                        "mimetype": "video/mp4",
                                        "duration": 12,
                                        "width": 1920,
                                        "height": 1080,
                                        "download": (
                                            "https://tile.loc.gov/storage-services/"
                                            "service/mbrs/loc/file.mp4"
                                        ),
                                    },
                                    {
                                        "canDownload": True,
                                        "mimetype": "video/mp4",
                                        "duration": 20,
                                        "width": 1080,
                                        "height": 1920,
                                        "rights_restricted": True,
                                        "download": (
                                            "https://tile.loc.gov/storage-services/"
                                            "service/mbrs/loc/restricted-file.mp4"
                                        ),
                                    }
                                ]
                            ],
                    }
                ],
            }
        )

        with patch(
            "app.services.providers.loc.requests.get",
            side_effect=[search_response, item_response],
        ):
            results = LibraryOfCongressProvider().search("history", minimum_duration=5)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].url.endswith("/file.mp4"))
        self.assertEqual((results[0].duration, results[0].width, results[0].height), (12, 1920, 1080))

    def test_search_rejects_a_direct_url_when_its_matching_file_is_restricted(self):
        restricted_url = (
            "https://tile.loc.gov/storage-services/service/mbrs/loc/restricted.mp4"
        )
        search_response = SimpleNamespace(
            json=lambda: {"results": [{"id": "http://www.loc.gov/item/00694425/"}]}
        )
        item_response = SimpleNamespace(
            json=lambda: {
                "item": {
                    "access_restricted": False,
                    "rights": ["This collection is in the public domain."],
                },
                "resources": [
                    {
                        "duration": 20,
                        "width": 1920,
                        "height": 1080,
                        "video": restricted_url,
                        "download_restricted": False,
                        "files": [
                            [
                                {
                                    "canDownload": True,
                                    "mimetype": "video/mp4",
                                    "download": restricted_url,
                                    "rights_restricted": True,
                                }
                            ]
                        ],
                    }
                ],
            }
        )

        with patch(
            "app.services.providers.loc.requests.get",
            side_effect=[search_response, item_response],
        ):
            results = LibraryOfCongressProvider().search("history", minimum_duration=5)

        self.assertEqual(results, [])

    def test_search_skips_requests_when_the_loc_budget_is_exhausted(self):
        with patch(
            "app.services.providers.loc._reserve_request_slot", return_value=False
        ), patch("app.services.providers.loc.requests.get") as get:
            results = LibraryOfCongressProvider().search("history", minimum_duration=5)

        self.assertEqual(results, [])
        get.assert_not_called()

    def test_rate_limited_response_pauses_follow_up_loc_requests(self):
        with loc_provider._request_lock:
            previous_times = loc_provider._request_times.copy()
            previous_pause = loc_provider._rate_limited_until
            loc_provider._request_times.clear()
            loc_provider._rate_limited_until = 0.0
        try:
            rate_limited_response = SimpleNamespace(status_code=429, headers={})
            with patch(
                "app.services.providers.loc.requests.get",
                return_value=rate_limited_response,
            ) as get:
                self.assertIsNone(
                    loc_provider._loc_get(
                        "https://www.loc.gov/film-and-videos/", timeout=(1, 1)
                    )
                )
                get.assert_called_once()

            with patch("app.services.providers.loc.requests.get") as get:
                self.assertIsNone(
                    loc_provider._loc_get(
                        "https://www.loc.gov/film-and-videos/", timeout=(1, 1)
                    )
                )
                get.assert_not_called()
        finally:
            with loc_provider._request_lock:
                loc_provider._request_times.clear()
                loc_provider._request_times.extend(previous_times)
                loc_provider._rate_limited_until = previous_pause


if __name__ == "__main__":
    unittest.main()
