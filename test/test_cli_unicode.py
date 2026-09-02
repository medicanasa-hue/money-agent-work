import io
from unittest.mock import patch

import cli


def test_legacy_console_can_print_turkish_and_unicode_progress():
    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding="cp1252", newline="\n")
    try:
        with (
            patch.object(cli.sys, "stdout", stream),
            patch.object(cli.sys, "stderr", stream),
        ):
            cli._force_utf8_console()
            print("Türkçe: ı ş ğ ⑤\u202f!")
            stream.flush()
        assert output.getvalue().decode("utf-8") == "Türkçe: ı ş ğ ⑤\u202f!\n"
    finally:
        stream.close()


def test_missing_closed_and_wrapped_streams_are_safe():
    closed = io.TextIOWrapper(io.BytesIO())
    closed.close()
    for stream in (None, closed, io.StringIO()):
        with (
            patch.object(cli.sys, "stdout", stream),
            patch.object(cli.sys, "stderr", stream),
        ):
            cli._force_utf8_console()
