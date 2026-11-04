"""Graded Appium locators for elements of a stored snapshot (``mobile_locate``).

Tiers follow Stuntman's crawl-native tables: **strong** an accessibility id (Android
``content-desc``, iOS ``accessibilityIdentifier``); **acceptable** an Android ``resource-id`` or an
iOS label; **fragile** visible text (breaks on every locale); **never** an index. A candidate only
counts when it matches exactly one node of the captured screen, so uniqueness is checked against
the whole raw tree, not just the elements the snapshot showed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from mobilepilot.models import RawNode, RawTree
from mobilepilot.perception.model import Element

TIERS = ("strong", "acceptable", "fragile", "never")


@dataclass(slots=True)
class Candidate:
    strategy: str
    """Appium strategy name: ``accessibility id``, ``id``, ``-android uiautomator``, ``-ios predicate string``."""
    value: str
    tier: str
    matches: int
    note: str = ""


def _count(tree: RawTree, test) -> int:
    return sum(1 for node in tree.root.walk() if node.visible and test(node))


def _texts(node: RawNode) -> set[str]:
    return {t for t in (node.title, node.label, node.value if isinstance(node.value, str) else None) if t}


def _java(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def candidates(el: Element, tree: RawTree, platform: str) -> list[Candidate]:
    """Every locator worth proposing for ``el``, best tier first."""
    found: list[Candidate] = []
    a11y = el.extra.get("a11y_id")
    ident = el.extra.get("identifier")
    text = el.extra.get("text")
    if not text and el.role == "row" and el.name:
        text = el.name.split(" — ")[0]  # a row's name is composed of its children's text: use the title
    if a11y:
        n = _count(tree, lambda node: node.a11y_id == a11y)
        found.append(Candidate("accessibility id", a11y, "strong", n))
    if platform == "android":
        if ident:
            n = _count(tree, lambda node: node.identifier == ident)
            found.append(Candidate("id", ident, "acceptable", n))
        if text:
            n = _count(tree, lambda node: text in _texts(node))
            note = "taps the row through its title" if el.role == "row" and not el.extra.get("text") else ""
            found.append(Candidate("-android uiautomator", f"new UiSelector().text({_java(text)})", "fragile", n, note))
            if ident:
                n = _count(tree, lambda node: node.identifier == ident and text in _texts(node))
                found.append(Candidate("-android uiautomator",
                                       f"new UiSelector().resourceId({_java(ident)}).text({_java(text)})", "fragile", n))
    else:
        label = el.name if el.name and el.name != a11y else None
        if label:
            n = _count(tree, lambda node: label in (node.label, node.title))
            found.append(Candidate("accessibility id", label, "acceptable", n, "the label: visible copy, locale risk"))
            cls = el.extra.get("class")
            if cls:
                value = f"type == {_java(cls)} AND label == {_java(label)}"
                n = _count(tree, lambda node: node.native_class == cls and label in (node.label, node.title))
                found.append(Candidate("-ios predicate string", value, "fragile", n))
    return found


def best(found: list[Candidate]) -> Candidate | None:
    """The best-tier candidate that names exactly one element."""
    unique = [c for c in found if c.matches == 1]
    return min(unique, key=lambda c: TIERS.index(c.tier)) if unique else None


def format_locate(el: Element, tree: RawTree, platform: str) -> str:
    """Lines for one element: the best locator, the alternatives, and a warning when the element has
    no accessibility id (a test problem and a TalkBack/VoiceOver defect at once)."""
    head = f"{el.ref} {el.role}" + (f" {_java(el.name)}" if el.name else "")
    found = candidates(el, tree, platform)
    pick = best(found)
    lines = [head]
    if pick is None:
        lines.append("  never  no unique locator (only an index or coordinates would find it)")
    for c in sorted(found, key=lambda c: (c is not pick, TIERS.index(c.tier))):
        mark = "best " if c is pick else "     "
        unique = "unique" if c.matches == 1 else f"matches {c.matches}"
        note = f"  ({c.note})" if c.note else ""
        lines.append(f"  {mark}{c.tier:<10} {c.strategy}: {c.value}  [{unique}]{note}")
    if not el.extra.get("a11y_id"):
        what = "content-desc" if platform == "android" else "accessibilityIdentifier"
        lines.append(f"  ! no accessibility id: ask the developers for a {what} (also a screen-reader defect)")
    return "\n".join(lines)
