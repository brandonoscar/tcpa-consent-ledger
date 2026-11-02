"""Registered message templates, rendered strictly.

A 10DLC campaign is registered with sample messages, and carriers audit what
you send against them. So every outbound text is one of your registered
templates with its blanks filled, and the renderer enforces what code can:

* only named ``{placeholders}``; a missing or an extra parameter is an error;
* no line breaks or control characters in a value, Unicode's own line and
  paragraph separators included (an extra line can forge a second message);
* the rendered text fits in ``max_rendered`` characters;
* every template carries opt-out language ("Reply STOP"), checked when the
  registry is built;
* a free-text blank (a notice, a status update) may not carry its own
  opt-out or opt-in instructions, which would contradict the footer.

What it cannot do is decide whether the prose in a free-text blank belongs to
the registered use case. That is a person's call, so keep free-text blanks
few, and put a human approval in front of them.
"""

from __future__ import annotations

import re
import string
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

_STOP_LANGUAGE = re.compile(r"\breply\s+stop\b", re.IGNORECASE)
_OPT_INSTRUCTIONS = re.compile(
    # "Reply START", 'Reply "START"', "Reply with YES", "Txt START", "Text back Y"
    r"\b(?:reply|respond|text|txt|send|msg)\b"
    r"(?:\W+(?:with|back|the\s+word))?\W+"
    r"(?:stop|start|unstop|yes|y|no|n|help|unsubscribe|join|subscribe)\b"
    r"|\bunsubscribe\b|\bopt[\s-]?(?:in|out)\b",
    re.IGNORECASE,
)
#: Control characters, and Unicode's line and paragraph separators (NEL is a
#: control character too).
_LINE_BREAKING = frozenset({"Cc", "Zl", "Zp"})


@dataclass(frozen=True)
class Template:
    text: str
    #: Placeholders that take prose rather than a name, an amount or a date.
    free_text: frozenset[str] = field(default_factory=frozenset)

    def placeholders(self) -> set[str]:
        names: set[str] = set()
        for _, name, _, _ in string.Formatter().parse(self.text):
            if name is None:
                continue
            if name == "" or name[0].isdigit() or not name.isidentifier():
                raise ValueError(f"templates use named placeholders only, got {{{name}}}")
            names.add(name)
        return names


class TemplateRegistry:
    def __init__(
        self,
        templates: Mapping[str, Template],
        *,
        max_rendered: int = 320,
        max_param: int = 1000,
        require_stop_language: bool = True,
    ) -> None:
        self._templates = dict(templates)
        self.max_rendered = max_rendered
        self.max_param = max_param
        for template_id, template in self._templates.items():
            names = template.placeholders()
            unknown_free = set(template.free_text) - names
            if unknown_free:
                raise ValueError(f"{template_id}: free_text names unknown blanks {unknown_free}")
            if require_stop_language and not _STOP_LANGUAGE.search(template.text):
                raise ValueError(f"{template_id}: every template needs 'Reply STOP' language")

    def ids(self) -> list[str]:
        return sorted(self._templates)

    def render(self, template_id: str, params: Mapping[str, str]) -> str:
        template = self._templates.get(template_id)
        if template is None:
            raise ValueError(f"unknown template {template_id!r}; registered: {self.ids()}")
        expected = template.placeholders()
        missing = expected - set(params)
        extra = set(params) - expected
        if missing:
            raise ValueError(f"{template_id}: missing params {sorted(missing)}")
        if extra:
            raise ValueError(f"{template_id}: unexpected params {sorted(extra)}")
        for name, value in sorted(params.items()):
            self._check_value(template_id, name, str(value), template.free_text)
        body = template.text.format(**{k: str(v) for k, v in params.items()})
        if len(body) > self.max_rendered:
            raise ValueError(
                f"{template_id}: renders to {len(body)} characters, over {self.max_rendered}"
            )
        return body

    def _check_value(
        self, template_id: str, name: str, value: str, free_text: Iterable[str]
    ) -> None:
        if any(unicodedata.category(c) in _LINE_BREAKING for c in value):
            raise ValueError(f"{template_id}.{name}: line breaks and control characters refused")
        if len(value) > self.max_param:
            raise ValueError(f"{template_id}.{name}: {len(value)} characters is not a text")
        if name in set(free_text) and _OPT_INSTRUCTIONS.search(value):
            raise ValueError(
                f"{template_id}.{name}: carries its own opt-out or opt-in instructions; "
                "the template's footer already has them"
            )


__all__ = ["Template", "TemplateRegistry"]
