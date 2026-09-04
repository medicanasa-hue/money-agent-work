import hashlib
import unittest

from app.utils import secret_fingerprint


class TestSecretFingerprint(unittest.TestCase):
    def test_cache_fingerprint_is_stable_but_not_plain_sha256(self):
        value = "example-low-entropy-credential"

        first = secret_fingerprint.for_cache(value)
        second = secret_fingerprint.for_cache(value)

        self.assertEqual(first, second)
        self.assertNotEqual(first, hashlib.sha256(value.encode("utf-8")).hexdigest())
        self.assertNotIn(value, first)

    def test_cache_fingerprint_distinguishes_values_and_preserves_empty_marker(self):
        self.assertNotEqual(
            secret_fingerprint.for_cache("first"),
            secret_fingerprint.for_cache("second"),
        )
        self.assertEqual(secret_fingerprint.for_cache(""), "")
        self.assertEqual(secret_fingerprint.for_cache(None), "")


if __name__ == "__main__":
    unittest.main()
