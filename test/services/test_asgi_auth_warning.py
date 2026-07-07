import unittest

from app.asgi import warn_if_api_unprotected


class TestAsgiAuthWarning(unittest.TestCase):
    def test_warns_when_api_key_empty_on_non_loopback(self):
        self.assertIsNotNone(warn_if_api_unprotected("", "0.0.0.0"))
        self.assertIsNotNone(warn_if_api_unprotected("   ", "::"))

    def test_does_not_warn_for_loopback_without_api_key(self):
        self.assertIsNone(warn_if_api_unprotected("", "127.0.0.1"))
        self.assertIsNone(warn_if_api_unprotected("", "localhost"))
        self.assertIsNone(warn_if_api_unprotected("", "::1"))

    def test_does_not_warn_when_api_key_is_set(self):
        self.assertIsNone(warn_if_api_unprotected("secret", "0.0.0.0"))


if __name__ == "__main__":
    unittest.main()
