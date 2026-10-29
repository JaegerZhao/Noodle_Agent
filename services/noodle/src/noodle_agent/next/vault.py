"""Portable Markdown projections and explicit, conflict-safe local edit import."""

import io
import json
import posixpath
import re
import zipfile
from urllib.parse import quote

from .contracts import canonical, sha
from .editor import edit_section, section_content
from .markdown_links import references, resolve_reference
from .navigation import current_notes, frozen_candidate, graph
from .repository import Conflict, InvalidKnowledge
from .vault_projection import commit_projection, recover_projection


def portable_files(store, *, public=False, obsidian=True):
    notes = current_notes(store, limit=100000)["items"]
    if public:
        with store.db.engine.connect() as c:
            from .repository import rows

            allowed = {
                r["note_id"]
                for r in rows(c, "SELECT note_id FROM publication_visibility WHERE is_public=1")
            }
        notes = [n for n in notes if n["id"] in allowed]
    by_id = {n["id"]: n for n in notes}
    projection = graph(store, limit=100000)
    edges = [e for e in projection["edges"] if e["source"] in by_id and e["target"] in by_id]
    files = {}
    manifest = []
    for identity, info in by_id.items():
        note = store.get_note(identity)
        candidate = frozen_candidate(store, note["revision"]["id"])

        def link(target, label, info=info, anchor=None):
            path = posixpath.relpath(
                by_id[target]["stable_path"], posixpath.dirname(info["stable_path"])
            )
            if obsidian:
                return (
                    "[[" + by_id[target]["stable_path"][:-3] + ('#'+anchor if anchor else '') + "|" + label.replace("|", " ") + "]]"
                )
            return (
                "["
                + label.replace("[", "\\[").replace("]", "\\]")
                + "]("
                + quote(path, safe="/.-_") + ('#'+quote(anchor) if anchor else '')
                + ")"
            )

        metadata = {
            "id": identity,
            "revision": note["revision"]["id"],
            "kind": note["kind"],
            "title": note["title"],
            "aliases": json.loads(note["aliases_json"]),
            "description": candidate.summary,
        }
        body = (
            "---\n"
            + "\n".join(k + ": " + json.dumps(v, ensure_ascii=False) for k, v in metadata.items())
            + "\n---\n\n# "
            + note["title"]
            + "\n\n"
        )
        for section in candidate.sections:
            text = section.markdown.strip()
            if not obsidian or public:
                for ref in reversed(references(text)):
                    if not ref["wiki"]:
                        continue
                    try:
                        target = resolve_reference(ref["target"], info, notes)
                        replacement = (
                            link(target["id"], ref["label"], anchor=ref['target'].partition('#')[2] or None)
                            if target["id"] in by_id
                            else ref["label"]
                        )
                    except Conflict:
                        replacement = ref["label"]
                    text = text[: ref["start"]] + replacement + text[ref["end"] :]
            body += (
                f"<!-- noodle:section {section.section_id} -->\n## {section.title}\n\n{text}\n\n"
            )
        body += "<!-- noodle:derived -->\n"
        if note["kind"] == "MOC":
            from .map_projection import ordered_entries
            body += "## 阅读路线\n\n"
            for entry, depth in ordered_entries(candidate.map_entries,
                    prune_empty=public or candidate.map_policy == "SOURCE", available=by_id):
                if entry.target_note_id and entry.target_note_id not in by_id:
                    continue
                body += "  " * depth + "- " + (link(entry.target_note_id, entry.title)
                    if entry.target_note_id else entry.title) + "\n"
        related = [e for e in edges if e["source"] == identity and not
            (note["kind"] == "MOC" and e["relation"] == "MAP_ENTRY")]
        backlinks = [e for e in edges if e["target"] == identity]
        for title, selected, key in (
            ("相关内容", related, "target"),
            ("反向链接", backlinks, "source"),
        ):
            if selected:
                body += "\n## " + title + "\n\n"
                seen_targets = set()
                for edge in selected:
                    anchor = edge.get('target_anchor') if key == 'target' else None
                    section_id = edge.get('target_section_id') if key == 'target' else edge.get('from_section_id')
                    if section_id:
                        target_candidate = frozen_candidate(store, by_id[edge[key]]['current_revision_id'])
                        anchor = next((s.title for s in target_candidate.sections if s.section_id == section_id), anchor)
                    signature = (edge[key], edge['relation'], edge.get('layer'), anchor)
                    if signature in seen_targets:
                        continue
                    seen_targets.add(signature)
                    body += (
                        "- "
                        + link(edge[key], by_id[edge[key]]["title"], anchor=anchor)
                        + "（"
                        + {"LINKS_TO": "关联知识", "RELATED_TO": "相关知识", "COMPARES_WITH": "方法比较", "DERIVED_FROM": "来源笔记", "MAP_ENTRY": "阅读目录", "PREREQUISITE_OF": "前置关系", "CONTRADICTS": "存在分歧", 'USES': '使用', 'VARIANT_OF': '方法变体'}.get(edge["relation"], edge["relation"])
                        + "）\n"
                    )
        # Assets only: originals, requests, private paths and build reports never enter a reading package.
        for ref in set(re.findall(r"!\[[^\]]*\]\(\.\./(assets/[^)]+)\)", body)):
            p = store.path(ref)
            if p.parent != store.path("assets") or p.suffix.lower() not in {
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
            }:
                raise InvalidKnowledge("Unsafe asset reference")
            data = p.read_bytes()
            if p.stem != sha(data):
                raise InvalidKnowledge("Asset hash differs")
            files[ref] = data
        files[info["stable_path"]] = body.encode()
        manifest.append(
            {
                "id": identity,
                "revision": metadata["revision"],
                "path": info["stable_path"],
                "sha256": sha(body),
            }
        )
    files["README.md"] = (
        "# Noodle 知识库\n\n从 Maps 阅读来源目录与知识地图；Notes 保存原子笔记，Concepts 保存归纳知识点。\n\n"
        + (
            "双向链接可以直接由 Obsidian 识别。"
            if obsidian
            else "使用标准相对 Markdown 链接，可移植到静态站点。"
        )
        + "\n原始 PDF 不包含在阅读包中。\n"
    ).encode()
    files[".noodle-manifest.json"] = canonical(
        {"contract": "next-vault-v1", "public": public, "notes": manifest}
    ).encode()
    # Navigation is portable data; Markdown maps are only one reading view.
    from .graph_projection import graph as project_graph
    from .knowledge_roots import roots, tree
    from .source_library import folders, list_sources
    systems = roots(store)['items'] if not public else [{'id':'all','title':'全部知识','kind':'AUTOMATIC',
        'count':sum(n['kind']=='CONCEPT' for n in notes)}]
    def complete_tree(identity):
        result = tree(store, identity, limit=3000)
        while result['next_offset'] is not None:
            page = tree(store, identity, limit=3000, offset=result['next_offset'], snapshot=result['snapshot'])
            result['entries'].extend(page['entries'])
            result['next_offset'] = page['next_offset']
        result['truncated'] = False
        for entry in result['entries']:
            entry['has_more'] = False
        return result
    workspace = {'contract': 'next-workspace-v1',
        'systems': systems,
        'trees': {r['id']: complete_tree(r['id']) for r in systems} if not public else {},
        'graph': project_graph(store, limit=100000)}
    if public:
        workspace['graph']['nodes'] = [n for n in workspace['graph']['nodes'] if n['id'] in by_id]
        workspace['graph']['edges'] = [e for e in workspace['graph']['edges'] if e['source'] in by_id and e['target'] in by_id]
        workspace['graph'] = {k: workspace['graph'][k] for k in ('nodes','edges')}
    else:
        workspace['folders'] = folders(store)['items']
        workspace['sources'] = [{k: s[k] for k in ('id','display_title','folder_id','current_revision_id','deleted_at','created_at')}
                                for s in list_sources(store)['items']]
    files['.noodle-workspace.json'] = canonical(workspace).encode()
    return files


def reading_zip(store, *, public=False, obsidian=True):
    content = io.BytesIO()
    for_files = portable_files(store, public=public, obsidian=obsidian)
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in sorted(for_files.items()):
            archive.writestr(path, data)
    return content.getvalue()


def write_vault(store):
    # Existing changed files are never overwritten by a refresh.
    root = store.path("Vault")
    root.mkdir(exist_ok=True)
    recover_projection(root)
    files = portable_files(store)
    for item in json.loads(files[".noodle-manifest.json"])["notes"]:
        store.put(f"artifacts/vault-base/{item['sha256']}.md", files[item["path"]])
    previous = {}
    manifest = root / ".noodle-manifest.json"
    if manifest.exists():
        previous = {n["path"]: n for n in json.loads(manifest.read_text(encoding="utf-8"))["notes"]}
    conflicts = []
    for path, old in previous.items():
        p = (root / path).resolve()
        if not p.is_relative_to(root):
            raise InvalidKnowledge("Unsafe manifest")
        if p.exists() and sha(p.read_bytes()) != old["sha256"]:
            conflicts.append(path)
    for path in files:
        p = (root / path).resolve()
        if not p.is_relative_to(root):
            raise InvalidKnowledge("Unsafe projection path")
        if (
            p.exists()
            and path not in previous
            and path.endswith(".md")
            and path != "README.md"
            and p.read_bytes() != files[path]
        ):
            conflicts.append(path)
    if conflicts:
        raise Conflict("Import or resolve local edits first: " + ", ".join(conflicts))
    commit_projection(root, files, [old for path, old in previous.items() if path not in files])
    from sqlalchemy import text

    from .repository import now

    exported = json.loads(files[".noodle-manifest.json"])["notes"]
    with store.repository.transaction() as c:
        c.execute(text("DELETE FROM vault_receipts"))
        for item in exported:
            c.execute(
                text(
                    "INSERT INTO vault_receipts(note_id,revision_id,file_sha256,relative_path,updated_at) VALUES(:id,:revision,:sha256,:path,:at)"
                ),
                {**item, "at": now()},
            )
    return {
        "path": str(root),
        "files": len(files),
        "notes": len(json.loads(files[".noodle-manifest.json"])["notes"]),
    }


def inspect_local(store):
    root = store.path("Vault")
    manifest = root / ".noodle-manifest.json"
    if not manifest.exists():
        return {"edits": [], "missing": [], "renamed": []}
    expected = json.loads(manifest.read_text(encoding="utf-8"))["notes"]
    by_id = {}
    by_path = {item["path"]: item for item in expected}
    duplicate = []
    for p in root.rglob("*.md"):
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        if p.is_symlink():
            raise InvalidKnowledge("Symlinks are not allowed in Vault")
        data = p.read_text(encoding="utf-8-sig")
        if not data.startswith("---\n"):
            continue
        meta = data.split("---\n", 2)[1]
        parsed = {}
        for key in ("id", "revision", "title"):
            match = re.search(r"^" + key + r": (.+)$", meta, re.MULTILINE)
            if not match:
                raise InvalidKnowledge("Missing Vault identity field")
            parsed[key] = json.loads(match[1])
        managed = by_path.get(p.relative_to(root).as_posix())
        if managed and parsed["id"] != managed["id"]:
            raise Conflict("Managed file identity was changed; keep its original ID")
        if not isinstance(parsed["id"], str) or not isinstance(parsed["revision"], str):
            raise Conflict("Invalid Vault identity")
        if parsed["id"] in by_id:
            duplicate.append(parsed["id"])
        by_id[parsed["id"]] = (p, data, parsed)
    if duplicate:
        raise Conflict("Duplicate Vault IDs: " + ", ".join(duplicate))
    edits = []
    missing = []
    renamed = []
    for old in expected:
        if old["id"] not in by_id:
            missing.append(old["id"])
            continue
        path, body, meta = by_id[old["id"]]
        if path.relative_to(root).as_posix() != old["path"]:
            renamed.append(
                {
                    "id": old["id"],
                    "path": path.relative_to(root).as_posix(),
                    "stable_path": old["path"],
                }
            )
        if sha(body) == old["sha256"]:
            continue
        if meta["revision"] != old["revision"]:
            raise Conflict("Vault base revision was modified")
        candidate = frozen_candidate(store, old["revision"])
        baseline_ref = f"artifacts/vault-base/{old['sha256']}.md"
        if not store.path(baseline_ref).exists():
            raise Conflict("Vault baseline is missing; back up local edits before a fresh export")
        baseline = store.read(baseline_ref, old["sha256"]).decode()
        if (
            body.partition("<!-- noodle:derived -->")[2]
            != baseline.partition("<!-- noodle:derived -->")[2]
        ):
            raise Conflict(
                "Generated navigation changed locally; put new Wiki links inside a note section"
            )
        expected_note = store.get_note(old["id"], revision_id=old["revision"], historical=True)
        frontmatter = body.split("---\n", 2)[1]
        for key, expected_value in (
            ("kind", expected_note["kind"]),
            ("aliases", json.loads(expected_note["aliases_json"])),
        ):
            match = re.search(r"^" + key + r": (.+)$", frontmatter, re.MULTILINE)
            if not match or json.loads(match[1]) != expected_value:
                raise Conflict("Unsupported Vault metadata edit: " + key)
        main = body.split("<!-- noodle:derived -->")[0]
        matches = list(
            re.finditer(
                r"^<!-- noodle:section ([a-zA-Z0-9_.:-]+) -->\n## ([^\n]+)\n\n", main, re.MULTILINE
            )
        )
        if len(matches) != len(candidate.sections) or [m[1] for m in matches] != [
            s.section_id for s in candidate.sections
        ]:
            raise Conflict("Section structure changed; use the structured editor")
        heading = re.search(r"^# (.+)$", main, re.MULTILINE)
        if not heading:
            raise Conflict("Missing note title")
        desired_title = heading[1]
        if meta["title"] != candidate.title:
            if desired_title not in {candidate.title, meta["title"]}:
                raise Conflict("Frontmatter title and document heading disagree")
            desired_title = meta["title"]
        for i, (match, section) in enumerate(zip(matches, candidate.sections)):
            text = main[
                match.end() : matches[i + 1].start() if i + 1 < len(matches) else len(main)
            ].strip()
            if match[2] != section.title:
                raise Conflict("Section title changed; use the structured editor")
            if text != section.markdown.strip() or desired_title != candidate.title:
                edits.append(
                    {
                        "note_id": old["id"],
                        "expected_revision": old["revision"],
                        "section_id": section.section_id,
                        "markdown": text,
                        "title": desired_title,
                    }
                )
    return {"edits": edits, "missing": missing, "renamed": renamed}


def import_vault(store, *, apply=False):
    report = inspect_local(store)
    if not apply:
        return report
    # Preflight all identities and target sections before any publish; CAS still fences concurrent writers.
    for edit in report["edits"]:
        current = store.get_note(edit["note_id"])
        base = frozen_candidate(store, edit["expected_revision"])
        active = frozen_candidate(store, current["revision"]["id"])
        old = next(s for s in base.sections if s.section_id == edit["section_id"])
        target = next((s for s in active.sections if s.section_id == edit["section_id"]), None)
        if not target or (
            section_content(target) != section_content(old)
            and target.markdown.strip() != edit["markdown"].strip()
        ):
            raise Conflict("Concurrent section conflict: " + edit["section_id"])
    results = []
    for edit in report["edits"]:
        results.append(edit_section(store, **edit))
    # Acknowledge accepted local bytes, then regenerate from current revisions. The manifest
    # is written last; a crash before refresh remains inspectable and publication is idempotent.
    root = store.path("Vault")
    manifest = root / ".noodle-manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    renames = {r["id"]: r["path"] for r in report["renamed"]}
    for item in data["notes"]:
        path = renames.get(item["id"], item["path"])
        p = (root / path).resolve()
        if not p.is_relative_to(root):
            raise InvalidKnowledge("Unsafe Vault path")
        if p.exists():
            item["path"] = path
            item["sha256"] = sha(p.read_bytes())
    manifest.write_text(canonical(data), encoding="utf-8")
    refreshed = write_vault(store)
    return {
        **report,
        "published": results,
        "projection": refreshed,
        "deletion_policy": "explicit withdrawal required",
    }
