import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main


class TestServerStartup(unittest.TestCase):
    def _run_with_config(self, listen_host, api_key):
        config = SimpleNamespace(
            listen_host=listen_host,
            listen_port=8080,
            reload_debug=False,
            app={"api_key": api_key},
        )
        with (
            patch.object(main, "config", config),
            patch.object(main, "logger") as logger,
            patch.object(main.uvicorn, "run") as run_server,
        ):
            main.run()

        return logger, run_server

    def test_run_warns_for_network_bind_without_api_key(self):
        logger, run_server = self._run_with_config("0.0.0.0", "")

        logger.warning.assert_called_once()
        self.assertIn("app.api_key", logger.warning.call_args.args[0])
        run_server.assert_called_once()

    def test_run_does_not_warn_for_loopback_bind_without_api_key(self):
        logger, run_server = self._run_with_config("127.0.0.1", "")

        logger.warning.assert_not_called()
        run_server.assert_called_once()

    def test_run_does_not_warn_for_network_bind_with_api_key(self):
        logger, run_server = self._run_with_config("0.0.0.0", "configured-key")

        logger.warning.assert_not_called()
        run_server.assert_called_once()


if __name__ == "__main__":
    unittest.main()
