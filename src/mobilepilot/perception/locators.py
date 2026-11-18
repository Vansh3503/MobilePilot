"""Graded Appium locators for elements of a stored snapshot (``mobile_locate``).

Tiers follow Stuntman's crawl-native tables: **strong** an accessibility id (Android
``content-desc``, iOS ``accessibilityIdentifier``); **acceptable** an Android ``resource-id`` or an
iOS label; **fragile** visible text (breaks on every locale); **never** an index. A candidate only
counts when it matches exactly one node of the captured screen, so uniqueness is checked against
the whole raw tree, not just the elements the snapshot showed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from mobilepilot.models import RawNode, RawTree
from mobilepilot.perception.model import Element

TIERS = ("strong", "acceptable", "fragile", "never")
_FIELD_ROLES = ("field", "search", "textarea")


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


def _starts(value: str | None, prefix: str) -> bool:
    """UiAutomator's *StartsWith and *Contains ignore case (Appium, Pixel 7a 2026-11-17): count the same way."""
    return bool(value) and value.lower().startswith(prefix.lower())


def _java(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _ui(text: str) -> str:
    """A UiSelector string argument. Appium's parser strips the quotes and unescapes only \\" (appium-
    uiautomator2-server UiExpressionParser.coerceArgToType), so a newline must stay a real newline:
    json.dumps wrote \\n, which UiAutomator then looked for as a backslash and an n, and never found."""
    return '"' + text.replace('"', '\\"') + '"'


_INVISIBLE = "\u2009\u200a\u200b\u00a0\u202f\ufeff"
_DYNAMIC = re.compile(r"\b\d+\s+items?\b|[₹$€£¥]\s?\d|\d\s?%|\bTab \d+ of \d+\b|\(\d+\)", re.I)
"""Copy that changes with the content: a cart count, a price, a discount, a tab position."""


_QUERY = re.compile(r"[?&]([\w.-]+)=([^&#\s]*)")
_VISIT_TOKEN = re.compile(r"(?=[A-Z0-9]{12,}$)(?=.*\d)(?=.*[A-Z])")
"""A per-visit id in a link: Amazon's ``pf_rd_r=BX2TWQ74MADG41PYWZVX`` changed between two visits."""
_VISIT_PARAMS = {"pf_rd_r", "pd_rd_r", "pd_rd_wg", "qid", "sr", "crid", "sprefix", "sid", "session", "sessionid",
                 "token", "ts", "timestamp", "gclid", "fbclid"}


def _volatile(key: str, value: str) -> bool:
    return key.lower() in _VISIT_PARAMS or key.lower().startswith("utm_") or bool(_VISIT_TOKEN.match(value))


def _link_candidates(a11y: str, tree: RawTree, note: str) -> list[Candidate] | None:
    """A web link's content-desc is its href. When the href carries a per-visit value the exact id
    never matches again; a ``key=value`` that stays (an id, preferably all digits) still does."""
    params = _QUERY.findall(a11y)
    if not any(_volatile(k, v) for k, v in params):
        return None
    n = _count(tree, lambda node: node.a11y_id == a11y)
    found = [Candidate("accessibility id", a11y, "fragile", n,
                       "; ".join(filter(None, [note, "the link holds a value that changes every visit"])))]
    stable = sorted((f"{k}={v}" for k, v in params if v and not _volatile(k, v)),
                    key=lambda kv: not kv.split("=", 1)[1].isdigit())
    for part in stable:
        m = _count(tree, lambda node: bool(node.a11y_id) and part.lower() in node.a11y_id.lower())
        if m == 1:
            found.append(Candidate("-android uiautomator", f"new UiSelector().descriptionContains({_ui(part)})",
                                   "acceptable", m, "; ".join(filter(None, [note, "the part of the link that stays"]))))
            break
    return found


def _repeated_name(text: str) -> str | None:
    """A WebView card's text is its image alt, its title and its button word ("X X Add"): the name."""
    m = re.match(r"(.{3,}?) \1(?: |$)", text)
    return m.group(1) if m else None


def _clean(text: str) -> str:
    return text.strip(" \t\n" + _INVISIBLE)


def _stable_prefix(text: str) -> str | None:
    """The words before the first number or currency sign ("Cart 1 item" -> "Cart")."""
    m = re.match(r"([^\d₹$€£¥%(]*)", text)
    prefix = _clean(m.group(1)) if m else ""
    return prefix if len(prefix) >= 3 else None


def _node_of(el: Element, tree: RawTree) -> RawNode | None:
    if el.handle is None:
        return None
    return next((n for n in tree.root.walk() if n.handle == el.handle), None)


def _description_candidates(a11y: str, tree: RawTree, *, note: str = "") -> list[Candidate]:
    """An accessibility id, graded down when it holds changing copy, with a prefix match that survives it."""
    link = _link_candidates(a11y, tree, note)
    if link is not None:
        return link
    n = _count(tree, lambda node: node.a11y_id == a11y)
    lines = a11y.split("\n")
    if sum(1 for line in lines if _clean(line)) > 1:
        return _card_candidates(a11y, lines, n, tree, note)
    if not _DYNAMIC.search(a11y):
        return [Candidate("accessibility id", a11y, "strong", n, note)]
    found = [Candidate("accessibility id", a11y, "acceptable", n,
                       "; ".join(filter(None, [note, "holds a count, price or percentage: breaks when it changes"])))]
    prefix = _stable_prefix(a11y)
    if prefix:
        m = _count(tree, lambda node: _starts(node.a11y_id, prefix))
        found.append(Candidate("-android uiautomator", f"new UiSelector().descriptionStartsWith({_ui(prefix)})",
                               "strong", m, "; ".join(filter(None, [note, "the part that does not change"]))))
    return found


def _card_candidates(a11y: str, lines: list[str], n: int, tree: RawTree, note: str) -> list[Candidate]:
    """Compose joins a card's labels with newlines (Play Store, Pixel 7a 2026-11-17: "Candy Crush Saga\\n
    Puzzle\\n...\\nStar rating: 4.4\\n83 MB\\n"): the exact id breaks when the rating, the size or the
    install state changes. The first line is the name; the second is added when other cards share it."""
    found = [Candidate("accessibility id", a11y, "acceptable", n,
                       "; ".join(filter(None, [note, f"joins {len([x for x in lines if _clean(x)])} lines (a rating, "
                                                     "a size, an install state): breaks when any of them changes"])))]
    for k in (1, 2):
        prefix = "\n".join(lines[:k])
        if _DYNAMIC.search(prefix):
            break
        if len(_clean(prefix)) < 3:
            continue   # "YO", "Yo": too short to name anything alone; with the publisher it does
        m = _count(tree, lambda node: _starts(node.a11y_id, prefix))
        if m == 1:
            found.append(Candidate("-android uiautomator", f"new UiSelector().descriptionStartsWith({_ui(prefix)})",
                                   "strong", m, "; ".join(filter(None, [note, "its name" if k == 1 else
                                                                        "its first two lines: others share the name"]))))
            break
    return found


def _from_children(el: Element, tree: RawTree) -> list[Candidate]:
    """For a tappable container with nothing of its own (Amazon's bottom tabs, Pixel 7a
    2026-11-16): the description or label of an element inside it. Tapping that element
    taps the container around it."""
    node = _node_of(el, tree)
    if node is None:
        return []
    inner = list(node.walk())[1:]
    found: list[Candidate] = []
    desc = next((n.a11y_id for n in inner if n.a11y_id), None)
    if desc:
        found += _description_candidates(desc, tree, note="on an element inside it")
    label = next((n for n in inner if n.role == "AXStaticText" and isinstance(n.value, str) and n.value != n.a11y_id
                  and _clean(n.value) and not _DYNAMIC.search(n.value) and not _clean(n.value).isdigit()), None)
    if label is not None:
        text, raw = _clean(label.value), label.value
        exact = text == raw
        how = "text" if exact else "textContains"
        if label.identifier:
            n = _count(tree, lambda x: x.identifier == label.identifier and isinstance(x.value, str)
                       and _clean(x.value) == text)
            found.append(Candidate("-android uiautomator",
                                   f"new UiSelector().resourceId({_ui(label.identifier)}).{how}({_ui(text)})",
                                   "fragile", n, "its label, inside it"))
        else:
            n = _count(tree, lambda x: text in {_clean(t) for t in _texts(x)})
            found.append(Candidate("-android uiautomator", f"new UiSelector().{how}({_ui(text)})",
                                   "fragile", n, "its label, inside it"))
    return found


def candidates(el: Element, tree: RawTree, platform: str) -> list[Candidate]:
    """Every locator worth proposing for ``el``, best tier first."""
    found: list[Candidate] = []
    a11y = el.extra.get("a11y_id")
    ident = el.extra.get("identifier")
    text = el.extra.get("text")
    if not text and el.role == "row" and el.name:
        text = el.name.split(" — ")[0]  # a row's name is composed of its children's text: use the title
    padded = bool(text) and _clean(text) != text
    if padded:
        text = _clean(text) or None   # thin spaces around a label ("\u2009Cart\u2009"): exact text would miss
    if a11y:
        found += _description_candidates(a11y, tree) if platform == "android" else [
            Candidate("accessibility id", a11y, "strong", _count(tree, lambda node: node.a11y_id == a11y))]
    if platform == "android":
        if ident:
            n = _count(tree, lambda node: node.identifier == ident)
            found.append(Candidate("id", ident, "acceptable", n))
        if text and (name := _repeated_name(text)):
            n = _count(tree, lambda node: any(_starts(_clean(t), name) for t in _texts(node)))
            found.append(Candidate("-android uiautomator", f"new UiSelector().textStartsWith({_ui(name)})",
                                   "fragile", n, "the name only: the words after it change (Add, a quantity)"))
        if text:
            n = _count(tree, lambda node: text in {_clean(t) for t in _texts(node)})
            note = "taps the row through its title" if el.role == "row" and not el.extra.get("text") else ""
            how = "textContains" if padded else "text"
            found.append(Candidate("-android uiautomator", f"new UiSelector().{how}({_ui(text)})", "fragile", n, note))
            if ident:
                n = _count(tree, lambda node: node.identifier == ident and text in _texts(node))
                found.append(Candidate("-android uiautomator",
                                       f"new UiSelector().resourceId({_ui(ident)}).text({_ui(text)})", "fragile", n))
        field = el.role in _FIELD_ROLES
        if best(found) is None and not field:
            found += _from_children(el, tree)   # an id every sibling shares is no locator either
        cls = el.extra.get("class")
        # A field's own children are its placeholder: text() finds a label that takes no typing and is gone
        # once something is typed (Play Store's "Search Apps & Games", Pixel 7a 2026-11-17).
        if best(found) is None and cls and field:
            # a text field with no id or description (Play Store's search box): its class, when it is the
            # only one. Never its text: that is what the person typed.
            n = _count(tree, lambda node: node.native_class == cls)
            found.append(Candidate("-android uiautomator", f"new UiSelector().className({_ui(cls)})", "fragile", n,
                                   "the only text field on this screen: ask for a resource-id or content-desc"))
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
        value = c.value.replace("\n", "\\n")   # one line each; \\n here is a newline in the string
        lines.append(f"  {mark}{c.tier:<10} {c.strategy}: {value}  [{unique}]{note}")
    if not el.extra.get("a11y_id"):
        what = "content-desc" if platform == "android" else "accessibilityIdentifier"
        lines.append(f"  ! no accessibility id: ask the developers for a {what} (also a screen-reader defect)")
    return "\n".join(lines)
