"""Complete source-unit accounting and cross-window semantic merge."""

from collections import Counter

from .generation_contracts import MergePlan, Unit, WindowPlan
from .repository import InvalidKnowledge


class QualityFailure(InvalidKnowledge):
    pass


def source_units(artifact: dict, *, max_chars: int = 4000) -> list[Unit]:
    if max_chars < 100:
        raise ValueError("Unit window too small")
    result = []
    for block in artifact["blocks"]:
        value = block["text"]
        for start in range(0, len(value), max_chars):
            piece = value[start : start + max_chars]
            if not piece.strip():
                continue
            result.append(
                Unit(
                    unit_id=f"u{len(result):05d}",
                    block_id=block["id"],
                    page=block["page"],
                    start=start,
                    end=start + len(piece),
                    text=piece,
                    kind=block["kind"],
                    asset_ref=block.get("asset_ref"),
                    visual=(
                        {
                            key: block["visual"].get(key)
                            for key in ("confidence", "is_decorative", "essential", "asset_type")
                        }
                        if block.get("visual")
                        else None
                    ),
                )
            )
    if not result:
        raise QualityFailure("No readable source units")
    return result


def windows(units: list[Unit], *, budget: int = 18000, max_units: int = 32) -> list[list[Unit]]:
    if max_units < 1:
        raise ValueError("Window unit budget must be positive")
    result, current, size = [], [], 0
    for unit in units:
        cost = len(unit.model_dump_json())
        if cost > budget:
            raise QualityFailure("A source unit exceeds planning context; reduce unit size")
        if current and (size + cost > budget or len(current) >= max_units):
            result.append(current)
            current, size = [], 0
        current.append(unit)
        size += cost
    if current:
        result.append(current)
    return result


def validate_window(plan: WindowPlan, units: list[Unit]):
    expected = {u.unit_id for u in units}
    assigned = [value for topic in plan.topics for value in topic.units]
    assigned += [e.unit_id for e in plan.exclusions]
    if set(assigned) != expected or any(count != 1 for count in Counter(assigned).values()):
        counts = Counter(assigned)
        raise QualityFailure(
            "Window coverage must account for every unit exactly once: "
            f"missing={sorted(expected - set(assigned))}; unknown={sorted(set(assigned) - expected)}; "
            f"duplicates={sorted(key for key, count in counts.items() if count > 1)}"
        )
    if len({t.key for t in plan.topics}) != len(plan.topics):
        raise QualityFailure("Duplicate topic key")
    by_id = {u.unit_id: u for u in units}
    included = {value for t in plan.topics for value in t.units}
    invalid_duplicates = [
        {"unit_id": e.unit_id, "duplicate_of": e.duplicate_of}
        for e in plan.exclusions
        if e.category == "DUPLICATE"
        and (e.duplicate_of not in included or e.duplicate_of == e.unit_id)
    ]
    if invalid_duplicates:
        raise QualityFailure(
            "Duplicate exclusion requires an included counterpart: "
            f"invalid={invalid_duplicates}. For repeated page headers or navigation text, use NAVIGATION, "
            "not DUPLICATE. For duplicated teaching content, name an actually included unit in duplicate_of."
        )
    for exclusion in plan.exclusions:
        if exclusion.category != "DUPLICATE" and exclusion.duplicate_of is not None:
            raise QualityFailure(
                f"Only duplicate exclusions may name a counterpart: {exclusion.unit_id}"
            )
        if exclusion.category == "DECORATIVE":
            visual = by_id[exclusion.unit_id].visual or {}
            if not visual.get("is_decorative"):
                raise QualityFailure(
                    f"Decorative exclusion requires visual assessment: {exclusion.unit_id}"
                )


def validate_merge(plan: MergePlan, topic_keys: set[str]):
    assigned = [key for group in plan.groups for key in group.topic_keys]
    if set(assigned) != topic_keys or any(n != 1 for n in Counter(assigned).values()):
        raise QualityFailure("Global merge must preserve each local topic exactly once")
    if len({g.key for g in plan.groups}) != len(plan.groups):
        raise QualityFailure("Duplicate merged group key")
