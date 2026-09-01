import string

from app.utils import utils


def test_stable_cache_key_is_a_deterministic_128_bit_sha256_prefix():
    source = "https://example.com/video.mp4?token=merhaba"

    first_key = utils.stable_cache_key(source)
    second_key = utils.stable_cache_key(source)

    assert first_key == second_key == "318fc19ffa127db9a93def852a9587e8"
    assert len(first_key) == 32
    assert set(first_key) <= set(string.hexdigits.lower())


def test_stable_cache_key_distinguishes_similar_sources():
    first_key = utils.stable_cache_key("https://example.com/video.mp4?token=merhaba")
    second_key = utils.stable_cache_key("https://example.com/video.mp4?token=merhabb")

    assert first_key != second_key
