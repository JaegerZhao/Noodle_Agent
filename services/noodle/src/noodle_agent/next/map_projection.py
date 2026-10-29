"""One deterministic tree traversal shared by source map exports."""

from .repository import InvalidKnowledge


def ordered_entries(entries, *, prune_empty=False, available=None):
    items = {e.entry_id: e for e in entries}
    children = {}
    for e in entries:
        children.setdefault(e.parent_entry_id, []).append(e)
    visible = set(items)
    if prune_empty:
        visible = set()
        for e in entries:
            if not e.target_note_id or (
                available is not None and e.target_note_id not in available
            ):
                continue
            cursor = e
            seen = set()
            while cursor:
                if cursor.entry_id in seen:
                    raise InvalidKnowledge("Cyclic map tree")
                seen.add(cursor.entry_id)
                visible.add(cursor.entry_id)
                cursor = items.get(cursor.parent_entry_id)
    result = []
    seen = set()
    stack = [(e, 0) for e in reversed(children.get(None, []))]
    while stack:
        e, depth = stack.pop()
        if e.entry_id in seen:
            raise InvalidKnowledge("Cyclic map tree")
        seen.add(e.entry_id)
        if e.entry_id not in visible:
            continue
        subs = children.get(e.entry_id, [])
        # Older source maps stored a same-title note link under its original heading.
        if (
            len(subs) == 1
            and not e.target_note_id
            and subs[0].target_note_id
            and subs[0].title == e.title
        ):
            e = e.model_copy(update={"target_note_id": subs[0].target_note_id})
            seen.add(subs[0].entry_id)
            subs = children.get(subs[0].entry_id, [])
        result.append((e, depth))
        stack.extend((child, depth + 1) for child in reversed(subs))
    if not prune_empty and len(seen) != len(items):
        raise InvalidKnowledge("Map contains missing parent")
    return result
