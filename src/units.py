"""Unit-system labels shared across the pipeline.

The model's unit system (SI vs English / US customary) is detected from
the HEC-RAS ``.prj`` file in Tab 1 (see ``model_scanner.scan_model``) and
threaded through the whole pipeline so every output - summary, CSV,
plots, interactive maps, validation, and alert thresholds - is reported
in the model's *native* units rather than a hard-coded assumption.

HEC-RAS stores WSE / depth / velocity / elevation results in the model's
native computational units (SI → metres & m/s; English → feet & ft/s),
so the pipeline never converts those arrays - it only labels them.
"""
from __future__ import annotations


def is_si(unit_system: str | None) -> bool:
    """True for SI / metric models.

    Defaults to ``True`` when the unit system is unknown - the HEC-RAS
    ``.prj`` always carries the token, so "unknown" only arises for
    hand-written settings, and SI matches the bundled reference model.
    """
    return str(unit_system).upper() != "ENGLISH"


def unit_labels(model_is_si: bool) -> dict[str, str]:
    """Return label strings for the given unit system.

    Keys:
      length      - short length/elevation unit  (``m`` / ``ft``)
      length_long - spelled-out length unit      (``metres`` / ``feet``)
      velocity    - velocity unit                (``m/s`` / ``ft/s``)
      flow        - discharge unit               (``m³/s`` / ``cfs``)
      len_suffix  - CSV column suffix            (``m`` / ``ft``)
    """
    if model_is_si:
        return {
            "length": "m",
            "length_long": "metres",
            "velocity": "m/s",
            "flow": "m³/s",
            "len_suffix": "m",
        }
    return {
        "length": "ft",
        "length_long": "feet",
        "velocity": "ft/s",
        "flow": "cfs",
        "len_suffix": "ft",
    }
