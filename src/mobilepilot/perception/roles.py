"""Accessibility roles: short display names and the "is this interactive?" heuristic."""

from __future__ import annotations

from mobilepilot.models import RawNode

_ROLE_NAMES: dict[str, str] = {
    "AXApplication": "app",
    "AXBusyIndicator": "busy",
    "AXButton": "button",
    "AXCell": "cell",
    "AXCheckBox": "checkbox",
    "AXColorWell": "colorwell",
    "AXComboBox": "combobox",
    "AXDateField": "date",
    "AXDateTimeArea": "date",
    "AXDialog": "dialog",
    "AXDisclosureTriangle": "disclosure",
    "AXGroup": "group",
    "AXHeading": "heading",
    "AXImage": "image",
    "AXIncrementor": "stepper",
    "AXLink": "link",
    "AXList": "list",
    "AXMenu": "menu",
    "AXMenuBar": "menubar",
    "AXMenuBarItem": "menu",
    "AXMenuButton": "menubutton",
    "AXMenuItem": "menuitem",
    "AXOutline": "outline",
    "AXPopUpButton": "popup",
    "AXProgressIndicator": "progress",
    "AXRadioButton": "radio",
    "AXRow": "row",
    "AXScrollArea": "scrollarea",
    "AXSheet": "sheet",
    "AXSlider": "slider",
    "AXSplitter": "splitter",
    "AXStaticText": "text",
    "AXTabGroup": "tabs",
    "AXTable": "table",
    "AXTextArea": "textarea",
    "AXTextField": "field",
    "AXTimeField": "time",
    "AXToolbar": "toolbar",
    "AXValueIndicator": "indicator",
    "AXWebArea": "webarea",
    "AXWindow": "window",
}

_SUBROLE_NAMES: dict[tuple[str, str], str] = {
    ("AXTextField", "AXSearchField"): "search",
    ("AXTextField", "AXSecureTextField"): "secure",
    ("AXCheckBox", "AXToggle"): "toggle",
    ("AXCheckBox", "AXSwitch"): "switch",
    ("AXRadioButton", "AXTabButton"): "tab",
}

DIALOG_SUBROLES: frozenset[str] = frozenset({"AXDialog", "AXSystemDialog"})
"""Window subroles that mark a dialog (any role carrying them is shown as ``dialog``)."""

INTERACTIVE_ROLES: frozenset[str] = frozenset(
    {
        "AXButton",
        "AXCheckBox",
        "AXColorWell",
        "AXComboBox",
        "AXDateField",
        "AXDateTimeArea",
        "AXDisclosureTriangle",
        "AXIncrementor",
        "AXLink",
        "AXMenuBarItem",
        "AXMenuButton",
        "AXMenuItem",
        "AXPopUpButton",
        "AXRadioButton",
        "AXRow",
        "AXSlider",
        "AXTextArea",
        "AXTextField",
        "AXTimeField",
    }
)
"""AX roles that are always worth showing to the model as action targets."""

PASSIVE_ACTIONS: frozenset[str] = frozenset({"AXShowMenu", "AXScrollToVisible", "scroll"})
"""Actions almost every element supports; having only these does not make it interactive.
A mobile container that scrolls carries ``scroll``: that makes it scrollable, not a tap target."""

TEXT_ROLES: frozenset[str] = frozenset(
    {"AXTextField", "AXTextArea", "AXComboBox", "AXStaticText", "AXWebArea", "AXSearchField"}
)
"""Roles that hold text; when one has keyboard focus it is treated as interactive."""

TEXT_INPUT_ROLES: frozenset[str] = frozenset({"AXTextField", "AXTextArea", "AXComboBox"})
"""Roles that accept typed text."""


def short_role(ax_role: str, subrole: str | None = None) -> str:
    """Compact, model-friendly role name (``AXButton`` → ``button``).

    Some subroles refine the name (``AXTextField``/``AXSearchField`` → ``search``) and the
    dialog subroles turn any role into ``dialog``. Unknown roles lose their ``AX`` prefix
    and are lowercased (``AXSplitGroup`` → ``splitgroup``).
    """
    if subrole:
        if subrole in DIALOG_SUBROLES:
            return "dialog"
        refined = _SUBROLE_NAMES.get((ax_role, subrole))
        if refined:
            return refined
    known = _ROLE_NAMES.get(ax_role)
    if known:
        return known
    return ax_role.removeprefix("AX").lower() or "unknown"


def is_interactive(node: RawNode) -> bool:
    """True for elements the model may want to act on.

    That is: an interactive role, any action besides the near-universal ``AXShowMenu`` /
    ``AXScrollToVisible``, or a text role that currently has keyboard focus.
    """
    if node.role in INTERACTIVE_ROLES:
        return True
    if any(action not in PASSIVE_ACTIONS for action in node.actions):
        return True
    return node.focused and node.role in TEXT_ROLES
