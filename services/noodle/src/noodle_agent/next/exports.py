"""Read-only article assembly with immutable revision references and assets."""

import io
import json
import re
import zipfile

from .source_structure import source_structure


def article(store, identity):
    from .figure_bindings import display_projection
    note = display_projection(store, store.get_note(identity))
    if not (note["creation_key"] or "").startswith("source-map:"):
        return note.get('display_article_markdown', note.get('display_markdown', note["markdown"])), [note]
    revision = note["creation_key"].split(":")[1]
    structure = source_structure(store, revision)
    {n["node_id"]: n for n in structure["outline"]["nodes"]}
    by_parent = {}
    for n in structure["notes"]:
        by_parent.setdefault(n["primary_node_id"], []).append(n)
    body = "# " + note["title"] + "\n\n"
    if structure["generation_status"] != "SUCCEEDED":
        body += "> 当前为部分生成内容，未完成章节不代表已生成。\n\n"
    used = []
    source_sections = structure["planning_status"] == "COMPLETE_SOURCE_SECTIONS"

    def emit(node, depth):
        nonlocal body
        if source_sections and node.get("published_count", 0) == 0:
            return
        body += "\n" + ("#" * min(depth, 5)) + " " + node["title"] + "\n\n"
        for item in sorted(
            by_parent.get(node["node_id"], []),
            key=lambda n: (n["pages"][0] if n["pages"] else 0, n["creation_key"]),
        ):
            n = display_projection(store, store.get_note(item["note_id"]))
            used.append(n)
            same_title = source_sections and len(by_parent.get(node["node_id"], [])) == 1
            if not same_title:
                body += "\n" + ("#" * min(depth + 1, 5)) + " " + n["title"] + "\n\n"
            content = re.sub(r"^# [^\n]*\n\n", "", n.get('display_article_markdown', n.get('display_markdown', n["markdown"])))
            content = re.sub(
                r"^(#{2,6}) ",
                lambda m, same_title=same_title: (
                    "#" * min(len(m[1]) + depth - int(same_title), 6) + " "
                ),
                content,
                flags=re.MULTILINE,
            )
            body += content + "\n"
        for child in structure["outline"]["nodes"]:
            if child["parent_id"] == node["node_id"]:
                emit(child, depth + 1)

    for node in structure["outline"]["nodes"]:
        if node["parent_id"] is None:
            emit(node, 2)
    attached = {n["id"] for n in used}
    for item in structure["notes"]:
        if item["note_id"] not in attached:
            n = display_projection(store, store.get_note(item["note_id"]))
            used.append(n)
            body += "\n" + n.get('display_article_markdown', n.get('display_markdown', n["markdown"])) + "\n"
    return body, used


def article_zip(store, identity):
    body, used = article(store, identity)
    body = body.replace("../assets/", "assets/")
    files = {"article.md": body.encode("utf-8")}
    for ref in set(
        re.findall(r"!\[[^\]]*\]\((assets/[a-f0-9]{64}\.(?:png|jpg|jpeg|webp))\)", body)
    ):
        files[ref] = store.path(ref).read_bytes()
    files["revisions.json"] = json.dumps(
        [{"note_id": n["id"], "revision_id": n["revision"]["id"], 'display_projection': n.get('display_projection')} for n in used], ensure_ascii=False
    ).encode("utf-8")
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as z:
        for name, value in files.items():
            z.writestr(name, value)
    return result.getvalue()
