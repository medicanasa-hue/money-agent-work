import json
import re
from typing import List

from loguru import logger


def _strip_code_fence(text: str) -> str:
    """Strip a surrounding markdown code fence from an LLM response.

    Non-OpenAI providers (Claude, Gemini, …) frequently wrap JSON output in a
    ```json … ``` fence even when asked to return raw JSON. Removing it lets the
    first json.loads() succeed instead of falling through to the regex recovery
    path (and spuriously logging a warning). Mirrors the DOTALL handling already
    used in _parse_social_metadata().
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _parse_json_string_list(response: str) -> List[str]:
    data = None
    try:
        data = json.loads(_strip_code_fence(response))
    except Exception as exc:
        logger.warning(f"video terms response was not plain JSON array: {str(exc)}")
        decoder = json.JSONDecoder()
        saw_non_string_list = False
        for match in re.finditer(r"\[", response or ""):
            try:
                candidate, _ = decoder.raw_decode(response, match.start())
            except ValueError:
                continue
            if isinstance(candidate, list):
                if all(isinstance(item, str) for item in candidate):
                    data = candidate
                    break
                saw_non_string_list = True
        if data is None and saw_non_string_list:
            raise ValueError("response JSON array must contain strings only")

    if not isinstance(data, list):
        raise ValueError("response is not a JSON array")

    for item in data:
        if not isinstance(item, str):
            raise ValueError("response JSON array must contain strings only")
    from .terms import _normalize_video_search_terms

    return _normalize_video_search_terms(data)
