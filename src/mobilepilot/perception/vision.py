"""OCR elements are not part of MobilePilot v1 (see the README's "Later"). The copied ref and
store code asks this one question, and the answer is always no."""

from __future__ import annotations

from mobilepilot.perception.model import Element


def is_vision_element(el: Element) -> bool:  # ponytail: no OCR yet; real check when an OCR fallback lands
    return False
