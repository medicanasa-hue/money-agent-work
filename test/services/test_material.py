import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.services import material
from app.services import providers as provider_registry
from app.services.providers.coverr import CoverrProvider
from app.services.providers.archive_org import ArchiveOrgProvider
from app.services.providers.nasa import NASAProvider
from app.services.providers.pexels import PexelsProvider
from app.services.providers.pixabay import PixabayProvider
from app.services.providers.wikimedia import WikimediaProvider
from app.services.providers import utils as provider_utils


class TestMaterialTlsVerification(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)
        config.app["video_cooldown_enabled"] = False

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

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
        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_pexels_accepts_larger_matching_aspect_file(self):
        config.app["pexels_api_keys"] = ["pexels-key"]

        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "duration": 8,
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
                            },
                            "fullHD": {
                                "width": 2160,
                                "height": 3840,
                                "url": "https://example.com/portrait-4k.mp4",
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

        def fake_save_video(video_url, save_dir=""):
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

        def fake_save_video(video_url, save_dir=""):
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

        def fake_save_video(video_url, save_dir=""):
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

        def fake_save_video(video_url, save_dir=""):
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

        def fake_save_video(video_url, save_dir=""):
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

        def fake_save_video(video_url, save_dir=""):
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

        def fake_save_video(video_url, save_dir=""):
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

        def fake_save_video(video_url, save_dir=""):
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

        def fake_save_video(video_url, save_dir=""):
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

        def fake_save_video(video_url, save_dir=""):
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

        def fake_save_video(video_url, save_dir=""):
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

    def test_rank_materials_keeps_tiny_primary_score_lead_ahead_of_content_match(self):
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

        self.assertEqual(ranked[0].url, higher_primary_score.url)

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

        def fake_save_video(video_url, save_dir=""):
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

        def fake_save_video(video_url, save_dir=""):
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
        save_video.assert_called_once_with(video_url=signed_url, save_dir="")
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
        save_video.assert_called_once_with(video_url=signed_url, save_dir="")
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
                            },
                            "fullHD": {
                                "width": 2160,
                                "height": 3840,
                                "url": "https://example.com/portrait-4k.mp4",
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
        self.assertEqual(results[0].width, 2160)
        self.assertEqual(results[0].height, 3840)

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
        self.assertEqual(results[0].license, "CC BY-SA 4.0")
        self.assertEqual(
            results[0].license_url,
            "https://creativecommons.org/licenses/by-sa/4.0/",
        )
        self.assertEqual(
            results[0].attribution,
            "File:City skyline.webm - City Camera Crew - CC BY-SA 4.0",
        )

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
                },
                "files": [
                    {
                        "format": "MPEG4",
                        "name": "city.mp4",
                        "size": "1000",
                        "length": "8",
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


if __name__ == "__main__":
    unittest.main()
