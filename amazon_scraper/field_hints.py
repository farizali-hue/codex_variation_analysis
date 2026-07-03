from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FieldHint:
    parser: str = "text"
    label_aliases: list[str] = field(default_factory=list)
    input_aliases: list[str] = field(default_factory=list)
    display_name: str = ""
    default_output_type: str = ""


_FIELD_HINTS: dict[str, FieldHint] = {
    "packaging_type": FieldHint(
        parser="packaging_type",
        label_aliases=["Packaging", "Package Type", "Container Type", "Form Factor"],
        input_aliases=["packaging", "packaging type", "package type", "container type", "pack format"],
        display_name="Packaging Type",
        default_output_type="text",
    ),
    "ingredients": FieldHint(
        parser="text",
        label_aliases=["Ingredients", "Other Ingredients", "Ingredient List"],
        input_aliases=["ingredients", "ingredient list", "other ingredients"],
        display_name="Ingredients",
        default_output_type="short_list",
    ),
    "serving_size": FieldHint(
        parser="serving_size",
        label_aliases=["Serving Size", "Serving size", "Serving", "Servings"],
        input_aliases=["serving size", "serving-size"],
        display_name="Serving Size",
        default_output_type="number",
    ),
    "servings_per_container": FieldHint(
        parser="servings_per_container",
        label_aliases=["Servings Per Container", "Servings per Container", "Servings Per Package"],
        input_aliases=["servings per container", "servings count"],
        display_name="Servings Per Container",
        default_output_type="number",
    ),
}


def get_field_hint(field_name: str) -> FieldHint:
    key = field_name.strip().lower()
    if key in _FIELD_HINTS:
        return _FIELD_HINTS[key]

    spaced = " ".join(part for part in field_name.replace("-", "_").split("_") if part)
    aliases = [field_name, field_name.lower(), spaced, spaced.title()]
    return FieldHint(label_aliases=[alias for alias in aliases if alias])


def get_supported_field_names() -> list[str]:
    return list(_FIELD_HINTS.keys())


def get_supported_field_output_type(field_name: str) -> str | None:
    key = field_name.strip().lower()
    hint = _FIELD_HINTS.get(key)
    if hint is None or not hint.default_output_type:
        return None
    return hint.default_output_type
