import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.models.schema import MaterialInfo
from app.services import material


def _dns_result(address: str, port: int = 443):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


class _UsableVideoFileClip:
    duration = 1
    fps = 24

    def __init__(self, path):
        self.path = path

    def close(self):
        return None


class TestVideoDownloadUrlSecurity(unittest.TestCase):
    def test_save_video_rejects_literal_private_ip_without_requesting_it(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(material.requests, "get") as get,
            patch("socket.getaddrinfo") as getaddrinfo,
        ):
            result = material.save_video(
                "http://127.0.0.1/internal.mp4", save_dir=temp_dir
            )

        self.assertEqual(result, "")
        get.assert_not_called()
        getaddrinfo.assert_not_called()

    def test_save_video_rejects_hostname_that_resolves_to_private_ip(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("10.20.30.40"),
            ) as getaddrinfo,
            patch.object(material.requests, "get") as get,
        ):
            result = material.save_video(
                "https://media.example/private.mp4", save_dir=temp_dir
            )

        self.assertEqual(result, "")
        getaddrinfo.assert_called_once()
        get.assert_not_called()

    def test_save_video_rejects_mixed_public_and_private_dns_results(self):
        answers = _dns_result("93.184.216.34") + _dns_result("192.168.1.25")
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("socket.getaddrinfo", return_value=answers),
            patch.object(material.requests, "get") as get,
        ):
            result = material.save_video(
                "https://mixed.example/video.mp4", save_dir=temp_dir
            )

        self.assertEqual(result, "")
        get.assert_not_called()

    def test_save_video_rejects_empty_dns_results(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("socket.getaddrinfo", return_value=[]),
            patch.object(material.requests, "get") as get,
        ):
            result = material.save_video(
                "https://empty-dns.example/video.mp4", save_dir=temp_dir
            )

        self.assertEqual(result, "")
        get.assert_not_called()

    def test_save_video_rejects_ipv4_mapped_private_ipv6_dns_result(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("::ffff:127.0.0.1"),
            ),
            patch.object(material.requests, "get") as get,
        ):
            result = material.save_video(
                "https://mapped.example/video.mp4", save_dir=temp_dir
            )

        self.assertEqual(result, "")
        get.assert_not_called()

    def test_save_video_rejects_userinfo_and_non_http_schemes(self):
        rejected_urls = (
            "https://user:secret@cdn.example/video.mp4",
            "file:///etc/passwd",
            "ftp://cdn.example/video.mp4",
        )

        for rejected_url in rejected_urls:
            with self.subTest(url=rejected_url):
                with (
                    tempfile.TemporaryDirectory() as temp_dir,
                    patch.object(material.requests, "get") as get,
                    patch("socket.getaddrinfo") as getaddrinfo,
                ):
                    result = material.save_video(rejected_url, save_dir=temp_dir)

                self.assertEqual(result, "")
                get.assert_not_called()
                getaddrinfo.assert_not_called()

    def test_save_video_rejects_redirect_to_private_ip(self):
        redirect_close = Mock()
        redirect = SimpleNamespace(
            status_code=302,
            headers={"Location": "http://169.254.169.254/latest/meta-data"},
            close=redirect_close,
        )

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("93.184.216.34"),
            ),
            patch.object(material.requests, "get", return_value=redirect) as get,
        ):
            result = material.save_video(
                "https://cdn.example/video.mp4", save_dir=temp_dir
            )

        self.assertEqual(result, "")
        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        redirect_close.assert_called_once_with()

    def test_save_video_follows_a_bounded_public_redirect_safely(self):
        redirect_close = Mock()
        final_close = Mock()
        redirect = SimpleNamespace(
            status_code=302,
            headers={"Location": "https://media.example/final.mp4"},
            close=redirect_close,
        )
        final = SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "video/mp4"},
            content=b"fake-video",
            close=final_close,
        )

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("93.184.216.34"),
            ) as getaddrinfo,
            patch.object(
                material.requests, "get", side_effect=(redirect, final)
            ) as get,
            patch.object(material, "VideoFileClip", _UsableVideoFileClip),
        ):
            result = material.save_video(
                "https://cdn.example/video.mp4", save_dir=temp_dir
            )

        self.assertTrue(result.endswith(".mp4"))
        self.assertEqual(get.call_count, 2)
        self.assertEqual(getaddrinfo.call_count, 2)
        self.assertTrue(all(not call.kwargs["allow_redirects"] for call in get.call_args_list))
        redirect_close.assert_called_once_with()
        final_close.assert_called_once_with()

    def test_save_video_keeps_public_streaming_download_behavior(self):
        response_close = Mock()
        response = SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "video/mp4"},
            content=b"fake-video",
            close=response_close,
        )

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("93.184.216.34"),
            ),
            patch.object(material.requests, "get", return_value=response) as get,
            patch.object(material, "VideoFileClip", _UsableVideoFileClip),
        ):
            result = material.save_video(
                "https://cdn.example/video.mp4", save_dir=temp_dir
            )

        self.assertTrue(result.endswith(".mp4"))
        self.assertTrue(get.call_args.kwargs["stream"])
        self.assertEqual(get.call_args.kwargs["timeout"], (30, 90))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        response_close.assert_called_once_with()

    def test_save_video_stops_after_three_redirect_hops(self):
        redirects = [
            SimpleNamespace(
                status_code=302,
                headers={"Location": f"https://cdn.example/hop-{index}.mp4"},
                close=Mock(),
            )
            for index in range(4)
        ]

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("93.184.216.34"),
            ),
            patch.object(material.requests, "get", side_effect=redirects) as get,
        ):
            result = material.save_video(
                "https://cdn.example/video.mp4", save_dir=temp_dir
            )

        self.assertEqual(result, "")
        self.assertEqual(get.call_count, 4)
        for redirect in redirects:
            redirect.close.assert_called_once_with()

    def test_save_video_rejects_private_connected_peer_after_public_dns(self):
        connected_socket = SimpleNamespace(
            getpeername=lambda: ("10.0.0.50", 443),
        )
        response_close = Mock()
        response = SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "video/mp4"},
            content=b"fake-video",
            close=response_close,
            raw=SimpleNamespace(
                _connection=SimpleNamespace(sock=connected_socket),
            ),
        )

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("93.184.216.34"),
            ),
            patch.object(material, "_request_uses_proxy", return_value=False),
            patch.object(material.requests, "get", return_value=response),
        ):
            result = material.save_video(
                "https://cdn.example/video.mp4", save_dir=temp_dir
            )

        self.assertEqual(result, "")
        response_close.assert_called_once_with()


class TestImageDownloadUrlSecurity(unittest.TestCase):
    def test_save_image_rejects_literal_private_ip_without_requesting_it(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(material.requests, "get") as get,
            patch("socket.getaddrinfo") as getaddrinfo,
        ):
            result = material.save_image(
                "http://127.0.0.1/internal.jpg", save_dir=temp_dir
            )

        self.assertEqual(result, "")
        get.assert_not_called()
        getaddrinfo.assert_not_called()

    def test_save_image_rejects_hostname_that_resolves_to_private_ip(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("172.16.1.5"),
            ),
            patch.object(material.requests, "get") as get,
        ):
            result = material.save_image(
                "https://images.example/private.jpg", save_dir=temp_dir
            )

        self.assertEqual(result, "")
        get.assert_not_called()

    def test_save_image_rejects_redirect_to_private_ip_for_every_provider(self):
        redirect_close = Mock()
        redirect = SimpleNamespace(
            status_code=302,
            headers={"Location": "http://192.168.1.20/internal.jpg"},
            close=redirect_close,
        )

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("93.184.216.34"),
            ),
            patch.object(material.requests, "get", return_value=redirect) as get,
        ):
            result = material.save_image(
                "https://images.example/photo.jpg", save_dir=temp_dir
            )

        self.assertEqual(result, "")
        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        redirect_close.assert_called_once_with()

    def test_save_image_keeps_provider_redirect_validator_as_an_extra_gate(self):
        redirect = SimpleNamespace(
            status_code=302,
            headers={"Location": "https://other-public.example/photo.jpg"},
            close=Mock(),
        )
        provider_validator = Mock(return_value=False)

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("93.184.216.34"),
            ),
            patch.object(material.requests, "get", return_value=redirect) as get,
        ):
            result = material.save_image(
                "https://images.example/photo.jpg",
                save_dir=temp_dir,
                redirect_url_validator=provider_validator,
            )

        self.assertEqual(result, "")
        get.assert_called_once()
        provider_validator.assert_called_once_with(
            "https://other-public.example/photo.jpg"
        )

    def test_save_image_closes_its_final_response(self):
        response_close = Mock()
        response = SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "image/jpeg"},
            content=b"image-bytes",
            close=response_close,
        )

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("93.184.216.34"),
            ),
            patch.object(material.requests, "get", return_value=response),
        ):
            result = material.save_image(
                "https://images.example/photo.jpg", save_dir=temp_dir
            )

        self.assertTrue(result.endswith(".jpg"))
        response_close.assert_called_once_with()

    def test_save_image_rejects_declared_oversized_body_before_reading_it(self):
        class Response:
            status_code = 200
            headers = {"Content-Type": "image/jpeg", "Content-Length": "6"}

            def __init__(self):
                self.close = Mock()
                self.body_was_read = False

            @property
            def content(self):
                self.body_was_read = True
                return b"abcdef"

        response = Response()
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("93.184.216.34"),
            ),
            patch.object(material, "_MAX_IMAGE_DOWNLOAD_BYTES", 5),
            patch.object(material.requests, "get", return_value=response),
        ):
            result = material.save_image(
                "https://images.example/oversized.jpg", save_dir=temp_dir
            )

        self.assertEqual(result, "")
        self.assertFalse(response.body_was_read)
        response.close.assert_called_once_with()

    def test_save_image_rejects_oversized_stream_without_content_length(self):
        iter_content = Mock(return_value=iter((b"abc", b"def")))
        response = SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "image/jpeg"},
            iter_content=iter_content,
            close=Mock(),
        )

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("93.184.216.34"),
            ),
            patch.object(material, "_MAX_IMAGE_DOWNLOAD_BYTES", 5),
            patch.object(material.requests, "get", return_value=response),
        ):
            result = material.save_image(
                "https://images.example/streamed.jpg", save_dir=temp_dir
            )

        self.assertEqual(result, "")
        iter_content.assert_called_once_with(chunk_size=1024 * 1024)
        response.close.assert_called_once_with()


class TestPreviewDownloadUrlSecurity(unittest.TestCase):
    @staticmethod
    def _item(preview_url: str) -> MaterialInfo:
        return MaterialInfo(
            provider="pexels",
            url="https://cdn.example/video.mp4",
            preview_url=preview_url,
        )

    def test_preview_score_rejects_literal_private_ip_without_requesting_it(self):
        item = self._item("http://127.0.0.1/internal.png")
        with (
            patch.object(material, "_is_preview_quality_filter_enabled", return_value=True),
            patch.object(material.requests, "get") as get,
            patch("socket.getaddrinfo") as getaddrinfo,
        ):
            result = material._preview_visual_quality_score(item)

        self.assertIsNone(result)
        get.assert_not_called()
        getaddrinfo.assert_not_called()

    def test_preview_score_rejects_hostname_that_resolves_to_private_ip(self):
        item = self._item("https://preview.example/internal.png")
        with (
            patch.object(material, "_is_preview_quality_filter_enabled", return_value=True),
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("10.0.0.25"),
            ),
            patch.object(material.requests, "get") as get,
        ):
            result = material._preview_visual_quality_score(item)

        self.assertIsNone(result)
        get.assert_not_called()

    def test_preview_score_rejects_redirect_to_private_ip(self):
        redirect_close = Mock()
        redirect = SimpleNamespace(
            status_code=302,
            headers={"Location": "http://169.254.169.254/latest/meta-data"},
            close=redirect_close,
        )
        item = self._item("https://preview.example/image.png")

        with (
            patch.object(material, "_is_preview_quality_filter_enabled", return_value=True),
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("93.184.216.34"),
            ),
            patch.object(material.requests, "get", return_value=redirect) as get,
        ):
            result = material._preview_visual_quality_score(item)

        self.assertIsNone(result)
        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        redirect_close.assert_called_once_with()

    def test_preview_score_closes_its_final_error_response(self):
        response_close = Mock()
        response = SimpleNamespace(
            status_code=500,
            headers={},
            close=response_close,
        )
        item = self._item("https://preview.example/image.png")

        with (
            patch.object(material, "_is_preview_quality_filter_enabled", return_value=True),
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("93.184.216.34"),
            ),
            patch.object(material.requests, "get", return_value=response),
        ):
            result = material._preview_visual_quality_score(item)

        self.assertIsNone(result)
        response_close.assert_called_once_with()

    def test_preview_score_rejects_declared_oversized_body_before_reading_it(self):
        class Response:
            status_code = 200
            headers = {"Content-Type": "image/png", "Content-Length": "6"}

            def __init__(self):
                self.close = Mock()
                self.body_was_read = False

            @property
            def content(self):
                self.body_was_read = True
                return b"abcdef"

        response = Response()
        item = self._item("https://preview.example/oversized.png")
        with (
            patch.object(material, "_is_preview_quality_filter_enabled", return_value=True),
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("93.184.216.34"),
            ),
            patch.object(material, "_MAX_PREVIEW_DOWNLOAD_BYTES", 5),
            patch.object(material.requests, "get", return_value=response),
        ):
            result = material._preview_visual_quality_score(item)

        self.assertIsNone(result)
        self.assertFalse(response.body_was_read)
        response.close.assert_called_once_with()

    def test_preview_score_rejects_oversized_stream_without_content_length(self):
        iter_content = Mock(return_value=iter((b"abc", b"def")))
        response = SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "image/png"},
            iter_content=iter_content,
            close=Mock(),
        )
        item = self._item("https://preview.example/streamed.png")

        with (
            patch.object(material, "_is_preview_quality_filter_enabled", return_value=True),
            patch(
                "socket.getaddrinfo",
                return_value=_dns_result("93.184.216.34"),
            ),
            patch.object(material, "_MAX_PREVIEW_DOWNLOAD_BYTES", 5),
            patch.object(material.requests, "get", return_value=response),
        ):
            result = material._preview_visual_quality_score(item)

        self.assertIsNone(result)
        iter_content.assert_called_once_with(chunk_size=1024 * 1024)
        response.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
