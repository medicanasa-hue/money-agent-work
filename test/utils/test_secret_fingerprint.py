import unittest
from unittest.mock import patch

from app.utils import secret_fingerprint


class TestSecretFingerprint(unittest.TestCase):
    def test_cache_fingerprint_uses_pbkdf2_derivation(self):
        with patch.object(
            secret_fingerprint.hashlib,
            "pbkdf2_hmac",
            return_value=b"\xab" * 32,
        ) as derive:
            fingerprint = secret_fingerprint.for_cache("configured-credential")

        algorithm, credential, salt, iterations = derive.call_args.args
        self.assertEqual(algorithm, "sha256")
        self.assertEqual(credential, b"configured-credential")
        self.assertGreaterEqual(len(salt), 16)
        self.assertGreaterEqual(iterations, 100_000)
        self.assertEqual(fingerprint, "ab" * 32)

    def test_cache_fingerprint_is_stable_and_opaque(self):
        value = "example-low-entropy-credential"

        first = secret_fingerprint.for_cache(value)
        second = secret_fingerprint.for_cache(value)

        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
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
