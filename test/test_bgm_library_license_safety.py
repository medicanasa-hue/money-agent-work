import ast
import hashlib
import os
from pathlib import Path
import re
import tempfile
import unittest


ROOT_DIR = Path(__file__).parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
SONG_DIR = ROOT_DIR / "resource" / "songs"
LICENSE_AUDIT = SONG_DIR / "LICENSE_AUDIT.md"


def _load_bgm_library_helper():
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_list_bgm_files"
    )
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"os": os}
    exec(compile(module, str(WEBUI_MAIN), "exec"), namespace)
    return namespace


class TestBgmLibraryLicenseSafety(unittest.TestCase):
    def test_library_only_lists_verified_cc0_tracks(self):
        namespace = _load_bgm_library_helper()
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "output001.mp3").touch()
            Path(temp_dir, "cc0_verified.mp3").touch()
            Path(temp_dir, "personal-upload.mp3").touch()
            namespace["song_dir"] = temp_dir

            tracks = namespace["_list_bgm_files"]()

        self.assertEqual(tracks, ["cc0_verified.mp3"])

    def test_random_bgm_library_has_five_audited_cc0_tracks(self):
        """Every random-eligible track must have a matching audited checksum."""
        tracks = sorted(SONG_DIR.glob("cc0_*.mp3"))
        self.assertGreaterEqual(len(tracks), 5)

        audit_text = LICENSE_AUDIT.read_text(encoding="utf-8")
        audited_hashes = {
            match.group("name"): match.group("checksum").upper()
            for match in re.finditer(
                r"^\|\s*(?P<name>cc0_[^|]+\.mp3)\s*\|\s*Verified CC0\s*\|.*?\|\s*`(?P<checksum>[A-Fa-f0-9]{64})`\s*\|",
                audit_text,
                re.MULTILINE,
            )
        }

        self.assertEqual({track.name for track in tracks}, set(audited_hashes))
        for track in tracks:
            checksum = hashlib.sha256(track.read_bytes()).hexdigest().upper()
            self.assertEqual(checksum, audited_hashes[track.name])


if __name__ == "__main__":
    unittest.main()
