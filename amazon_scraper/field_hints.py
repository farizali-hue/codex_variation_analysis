from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FieldHint:
    parser: str = "text"
    label_aliases: list[str] = field(default_factory=list)


_FIELD_HINTS: dict[str, FieldHint] = {
    "serving_size": FieldHint(
        parser="serving_size",
        label_aliases=["Serving Size", "Serving size", "Serving", "Servings"],
    ),
    "caffeinated": FieldHint(
        parser="yes_no_or_status",
        label_aliases=[
            "Caffeine Content Description",
            "Caffeinated",
            "Contains Caffeine",
            "Decaf",
            "Decaffeinated",
            "Low Caffeine",
        ],
    ),
    "caffeine_per_serving": FieldHint(
        parser="amount_with_unit",
        label_aliases=["Caffeine per Serving", "Caffeine", "Caffeine Content"],
    ),
    "roast_level": FieldHint(
        parser="roast_level",
        label_aliases=["Roast Level", "Coffee Roast Level", "Roast"],
    ),
}


def get_field_hint(field_name: str) -> FieldHint:
    key = field_name.strip().lower()
    if key in _FIELD_HINTS:
        return _FIELD_HINTS[key]

    spaced = " ".join(part for part in field_name.replace("-", "_").split("_") if part)
    aliases = [field_name, field_name.lower(), spaced, spaced.title()]
    return FieldHint(label_aliases=[alias for alias in aliases if alias])
