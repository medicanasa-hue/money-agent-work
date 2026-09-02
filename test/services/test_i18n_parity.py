import json
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).parent.parent.parent
I18N_DIR = ROOT_DIR / "webui" / "i18n"
EXPECTED_LOCALES = {"de", "en", "es", "id", "pt", "ru", "tr", "vi", "zh"}
REQUIRED_TRANSLATION_LOCALES = {"en", "tr"}


def _translation_keys(locale: str) -> set[str]:
    data = json.loads((I18N_DIR / f"{locale}.json").read_text(encoding="utf-8"))
    return set(data["Translation"])


class TestI18nParity(unittest.TestCase):
    def test_active_locales_cover_english_translation_keys(self):
        locale_names = {path.stem for path in I18N_DIR.glob("*.json")}
        self.assertEqual(locale_names, EXPECTED_LOCALES)

        english_keys = _translation_keys("en")
        missing_key_counts = {
            locale: len(english_keys - _translation_keys(locale))
            for locale in sorted(REQUIRED_TRANSLATION_LOCALES - {"en"})
        }

        self.assertEqual(missing_key_counts, {locale: 0 for locale in missing_key_counts})


if __name__ == "__main__":
    unittest.main()
