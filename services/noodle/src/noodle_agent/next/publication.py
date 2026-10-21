"""Validate candidates and atomically accept immutable Markdown revisions."""

from __future__ import annotations

import posixpath
from urllib.parse import quote

from sqlalchemy import text

from .content import ContentRepository
from .contracts import Candidate, Review, canonical, sha
from .jobs import Lease, require_lease
from .repository import Conflict, InvalidKnowledge, Repository, identifier, insert, now, one, rows
from .sources import SourceRepository


class Publisher:
    def __init__(
        self, repository: Repository, sources: SourceRepository, content: ContentRepository
    ):
        self.repository = repository
        self.sources = sources
        self.content = content

    def validate_inputs(self, connection, note, candidate: Candidate) -> list:
        manifest = []
        atomic_sources = set()
        for section in candidate.sections:
            if note["kind"] != "MOC" and not section.evidence_ids:
                raise InvalidKnowledge("Each knowledge section requires evidence")
            for evidence_id in section.evidence_ids:
                evidence = one(
                    connection, "SELECT * FROM evidence_anchors WHERE id=:id", id=evidence_id
                )
                if not evidence or not self.sources.source_valid(
                    connection, evidence["source_revision_id"]
                ):
                    raise InvalidKnowledge("Unknown or invalid evidence")
                atomic_sources.add(evidence["source_revision_id"])
                manifest.append(
                    {
                        "section": section.section_id,
                        "evidence": evidence_id,
                        "source_revision": evidence["source_revision_id"],
                        "sha256": evidence["sha256"],
                    }
                )
            if note["kind"] == "ATOMIC" and section.inputs:
                raise InvalidKnowledge("Atomic evidence cannot depend on Concept inputs")
            if note["kind"] == "CONCEPT":
                if not section.inputs:
                    raise InvalidKnowledge("Concept sections require atomic inputs")
                supported = set()
                for contribution in section.inputs:
                    source = one(
                        connection,
                        """SELECT r.*,n.kind FROM note_revisions r JOIN notes n ON n.id=r.note_id
                        WHERE r.id=:id""",
                        id=contribution.atomic_revision_id,
                    )
                    if (
                        not source
                        or source["kind"] != "ATOMIC"
                        or not self.content.valid_revision(connection, source["id"])
                    ):
                        raise InvalidKnowledge("Concept input must be a current ATOMIC revision")
                    selected = set()
                    for section_id in contribution.section_ids:
                        present = one(
                            connection,
                            "SELECT section_id FROM sections WHERE revision_id=:r AND section_id=:s",
                            r=source["id"],
                            s=section_id,
                        )
                        if not present:
                            raise InvalidKnowledge("Unknown selected section")
                        selected.update(
                            r["evidence_id"]
                            for r in rows(
                                connection,
                                "SELECT evidence_id FROM section_evidence WHERE revision_id=:r AND section_id=:s",
                                r=source["id"],
                                s=section_id,
                            )
                        )
                    if contribution.fragments:
                        body = self.repository.read(
                            source["markdown_ref"], source["markdown_sha256"]
                        ).decode("utf-8")
                        if {f.section_id for f in contribution.fragments} != set(
                            contribution.section_ids
                        ):
                            raise InvalidKnowledge(
                                "Fragment sections differ from selected sections"
                            )
                        for fragment in contribution.fragments:
                            bounds = one(
                                connection,
                                "SELECT start_offset,end_offset FROM sections WHERE revision_id=:r AND section_id=:s",
                                r=source["id"],
                                s=fragment.section_id,
                            )
                            if (
                                not bounds
                                or not bounds["start_offset"]
                                <= fragment.start_offset
                                < fragment.end_offset
                                <= bounds["end_offset"]
                                or sha(body[fragment.start_offset : fragment.end_offset])
                                != fragment.sha256
                            ):
                                raise InvalidKnowledge(
                                    "Fragment range or hash does not match atomic body"
                                )
                    if not set(contribution.evidence_ids).issubset(selected):
                        raise InvalidKnowledge("Input evidence outside selected atomic sections")
                    supported.update(contribution.evidence_ids)
                    manifest.append(
                        {"section": section.section_id, "input": contribution.model_dump()}
                    )
                if set(section.evidence_ids) != supported:
                    raise InvalidKnowledge("Concept evidence must match atomic input support")
            elif note["kind"] == "MOC" and (section.inputs or section.evidence_ids):
                raise InvalidKnowledge("MOC is navigation, not an independent evidence page")
        if note["kind"] == "ATOMIC" and len(atomic_sources) != 1:
            raise InvalidKnowledge("Atomic note must have exactly one source revision")
        if note["kind"] != "MOC" and candidate.map_entries:
            raise InvalidKnowledge("Only MOC can contain map entries")
        entries = {e.entry_id: e for e in candidate.map_entries}
        for entry in candidate.map_entries:
            ancestors = {entry.entry_id}
            parent = entry.parent_entry_id
            while parent is not None:
                if parent not in entries or parent in ancestors:
                    raise InvalidKnowledge("Missing parent or cyclic map structure")
                ancestors.add(parent)
                parent = entries[parent].parent_entry_id
            if entry.target_revision_id and not entry.target_note_id:
                raise InvalidKnowledge("Pinned revision requires a target note")
            if entry.target_note_id:
                target = one(
                    connection, "SELECT * FROM notes WHERE id=:id", id=entry.target_note_id
                )
                if not target or not target["current_revision_id"]:
                    raise InvalidKnowledge("Unknown or unpublished map target")
                if entry.target_revision_id:
                    pinned = one(
                        connection,
                        "SELECT note_id FROM note_revisions WHERE id=:id",
                        id=entry.target_revision_id,
                    )
                    if not pinned or pinned["note_id"] != entry.target_note_id:
                        raise InvalidKnowledge("Pinned revision belongs to another note")
        for link in candidate.links:
            target = one(
                connection,
                "SELECT current_revision_id FROM notes WHERE id=:id",
                id=link.target_note_id,
            )
            if not target:
                raise InvalidKnowledge("Unknown link target")
            if link.from_section_id and link.from_section_id not in {
                s.section_id for s in candidate.sections
            }:
                raise InvalidKnowledge("Unknown outgoing section")
            if link.target_section_id and not one(
                connection,
                "SELECT section_id FROM sections WHERE revision_id=:r AND section_id=:s",
                r=target["current_revision_id"],
                s=link.target_section_id,
            ):
                raise InvalidKnowledge("Unknown target section")
        return manifest

    def publish(
        self,
        candidate: Candidate,
        *,
        expected_revision: str | None,
        review: Review,
        allow_protected_edit: bool = False,
        lease: Lease | None = None,
        guard=None,
        accepted=None,
    ) -> dict:
        candidate = Candidate.model_validate(candidate.model_dump())
        if candidate.policy_version != self.repository.policy_version:
            raise InvalidKnowledge("Candidate policy is not currently accepted")
        if (
            not review.passed
            or review.candidate_sha256 != candidate.fingerprint
            or review.policy_version != candidate.policy_version
        ):
            raise InvalidKnowledge("Missing, failed or stale semantic review")
        chunks = [f"# {candidate.title}\n\n"]
        section_rows = []
        length = len(chunks[0])
        for section in candidate.sections:
            body = section.markdown.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
            heading = f"## {section.title}\n\n"
            chunks.append(heading)
            length += len(heading)
            start = length
            chunks.append(body + "\n")
            length += len(body) + 1
            section_rows.append((section, start, start + len(body), sha(body)))
        if candidate.map_entries:
            entries = {entry.entry_id: entry for entry in candidate.map_entries}
            with self.repository.db.engine.connect() as connection:
                owner = one(
                    connection, "SELECT stable_path FROM notes WHERE id=:id", id=candidate.note_id
                )
                if not owner:
                    raise InvalidKnowledge("Unknown map")
                paths = {}
                for entry in candidate.map_entries:
                    if entry.target_note_id:
                        target = one(
                            connection,
                            "SELECT stable_path FROM notes WHERE id=:id",
                            id=entry.target_note_id,
                        )
                        if not target:
                            raise InvalidKnowledge("Unknown map target")
                        paths[entry.target_note_id] = target["stable_path"]
            chunks.append("## 阅读路线\n\n")
            children = {}
            for entry in candidate.map_entries:
                children.setdefault(entry.parent_entry_id, []).append(entry)
            stack = [(entry, 0) for entry in reversed(children.get(None, []))]
            ordered = []
            seen = set()
            while stack:
                entry, depth = stack.pop()
                if entry.entry_id in seen:
                    raise InvalidKnowledge("Missing parent or cyclic map structure")
                seen.add(entry.entry_id)
                ordered.append((entry, depth))
                stack.extend(
                    (child, depth + 1) for child in reversed(children.get(entry.entry_id, []))
                )
            if len(seen) != len(entries):
                raise InvalidKnowledge("Missing parent or cyclic map structure")
            for entry, depth in ordered:
                label = entry.title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
                if entry.target_note_id:
                    relative = posixpath.relpath(
                        paths[entry.target_note_id], posixpath.dirname(owner["stable_path"])
                    )
                    label = f"[{label}]({quote(relative, safe='/.-_')})"
                if entry.target_revision_id:
                    label += f"（固定版本：{entry.target_revision_id}）"
                if entry.description:
                    label += "：" + entry.description.replace("\n", " ")
                chunks.append("  " * depth + "- " + label + "\n")
            chunks.append("\n")
        markdown = "".join(chunks)
        revision_id = identifier()
        ref = f"revisions/{revision_id}.md"
        # File creation precedes DB publication. A failed CAS may leave an
        # unreferenced immutable file, but never a head pointing at missing data.
        self.repository.put(ref, markdown.encode("utf-8"))
        with self.repository.transaction() as connection:
            active_policy = one(
                connection, "SELECT active_policy FROM storage_contract WHERE id=1"
            )["active_policy"]
            if active_policy != candidate.policy_version:
                raise Conflict("Publication policy changed")
            if lease is not None:
                require_lease(connection, lease)
            note = one(connection, "SELECT * FROM notes WHERE id=:id", id=candidate.note_id)
            if not note or note["visibility"] != "ACTIVE":
                raise InvalidKnowledge("Unknown or inactive note")
            if note["current_revision_id"] != expected_revision:
                raise Conflict("Note head changed")
            if guard is not None:
                guard(connection)
            manifest = self.validate_inputs(connection, note, candidate)
            if expected_revision and not allow_protected_edit:
                for previous in rows(
                    connection,
                    "SELECT section_id,sha256 FROM sections WHERE revision_id=:r AND protected=1",
                    r=expected_revision,
                ):
                    current_section = next(
                        (r for r in section_rows if r[0].section_id == previous["section_id"]), None
                    )
                    actual = current_section[3] if current_section else None
                    if actual != previous["sha256"] or not current_section[0].protected:
                        raise Conflict("Protected section cannot be changed or removed")
            ordinal = connection.execute(
                text("SELECT coalesce(max(ordinal),0)+1 FROM note_revisions WHERE note_id=:id"),
                {"id": note["id"]},
            ).scalar_one()
            input_hash = sha(canonical(manifest))
            insert(
                connection,
                "note_revisions",
                id=revision_id,
                note_id=note["id"],
                parent_revision_id=expected_revision,
                ordinal=ordinal,
                title=candidate.title,
                summary=candidate.summary,
                scope_json=canonical(candidate.scope.model_dump()),
                markdown_ref=ref,
                markdown_sha256=sha(markdown),
                input_sha256=input_hash,
                policy_version=candidate.policy_version,
                author=review.reviewer,
                checks_json=canonical(review.model_dump()),
                status="PUBLISHED",
                created_at=now(),
            )
            for section, start, end, digest in section_rows:
                insert(
                    connection,
                    "sections",
                    revision_id=revision_id,
                    section_id=section.section_id,
                    position=candidate.sections.index(section),
                    title=section.title,
                    question=section.question,
                    scope_json=canonical(section.scope.model_dump()),
                    start_offset=start,
                    end_offset=end,
                    sha256=digest,
                    protected=int(section.protected),
                )
                for evidence in section.evidence_ids:
                    insert(
                        connection,
                        "section_evidence",
                        revision_id=revision_id,
                        section_id=section.section_id,
                        evidence_id=evidence,
                        role="SUPPORT",
                    )
                for contribution in section.inputs:
                    insert(
                        connection,
                        "concept_inputs",
                        id=identifier(),
                        concept_revision_id=revision_id,
                        section_id=section.section_id,
                        atomic_revision_id=contribution.atomic_revision_id,
                        selection_json=canonical(contribution.section_ids),
                        fragments_json=canonical([f.model_dump() for f in contribution.fragments]),
                        evidence_json=canonical(contribution.evidence_ids),
                        role=contribution.role,
                        action=contribution.action,
                    )
            # Parents are persisted before children, irrespective of candidate order.
            remaining = list(candidate.map_entries)
            inserted = set()
            while remaining:
                for entry in list(remaining):
                    if entry.parent_entry_id and entry.parent_entry_id not in inserted:
                        continue
                    insert(
                        connection,
                        "map_entries",
                        map_revision_id=revision_id,
                        entry_id=entry.entry_id,
                        parent_entry_id=entry.parent_entry_id,
                        position=candidate.map_entries.index(entry),
                        title=entry.title,
                        target_note_id=entry.target_note_id,
                        target_revision_id=entry.target_revision_id,
                        description=entry.description,
                    )
                    remaining.remove(entry)
                    inserted.add(entry.entry_id)
            for link in candidate.links:
                insert(
                    connection,
                    "links",
                    id=identifier(),
                    from_revision_id=revision_id,
                    from_section_id=link.from_section_id,
                    target_note_id=link.target_note_id,
                    target_section_id=link.target_section_id,
                    relation=link.relation,
                    status=link.status,
                    basis_json=canonical({"basis": link.basis}),
                )
            changed = connection.execute(
                text("""UPDATE notes SET current_revision_id=:revision,title=:title
                WHERE id=:id AND current_revision_id IS :expected"""),
                {
                    "revision": revision_id,
                    "title": candidate.title,
                    "id": note["id"],
                    "expected": expected_revision,
                },
            )
            if changed.rowcount != 1:
                raise Conflict("Note head changed")
            self.repository.advance_epoch(connection)
            self.repository.event(
                connection,
                "NOTE_PUBLISHED",
                note["id"],
                revision_id,
                {"previous_revision_id": expected_revision, "kind": note["kind"]},
                key=f"publish:{revision_id}",
            )
            # Full, immutable build contract makes projections reproducible.
            build = canonical(
                {
                    "candidate": candidate.model_dump(),
                    "review": review.model_dump(),
                    "inputs": manifest,
                    "markdown_ref": ref,
                    "markdown_sha256": sha(markdown),
                }
            )
            build_ref = f"artifacts/builds/{revision_id}.json"
            self.repository.put(build_ref, build.encode("utf-8"))
            insert(
                connection,
                "build_artifacts",
                id=identifier(),
                job_id=lease.job_id if lease is not None else None,
                kind="PUBLICATION",
                storage_ref=build_ref,
                sha256=sha(build),
                policy_version=candidate.policy_version,
                input_sha256=input_hash,
                created_at=now(),
            )
            if accepted is not None:
                accepted(connection, revision_id)
        return {
            "note_id": candidate.note_id,
            "revision_id": revision_id,
            "ordinal": ordinal,
            "markdown_sha256": sha(markdown),
            "input_sha256": input_hash,
        }
