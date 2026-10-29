"""Compile Concept revisions with a generation fence inside publication."""

import json

from sqlalchemy import bindparam, text

from ..contracts import Candidate, FragmentRef, Input, Link, Review, Scope, Section, canonical, sha
from ..jobs import require_lease
from ..planning import QualityFailure
from ..repository import Conflict, one
from .coordinator import desired, signature
from .writing import write_chapter


def chapter_declaration(entries):
    """The creating decision owns metadata, not the first supplemental input."""
    return next(
        (entry for entry in entries if entry[0]["action"] in {"CREATE_SECTION", "CREATE_CONCEPT"}),
        entries[0],
    )


def published_scope(original, sections, topics=()):
    """Grow current coverage without erasing existing explicit boundaries."""
    return original.model_copy(
        update={"includes": list(dict.fromkeys(original.includes + list(topics) + [s.question for s in sections]))}
    )


def retain_links(generated, previous, effective):
    """Author links keep their sections and ownership even at shared endpoints."""
    def key(link):
        return (link.target_note_id, link.relation, link.navigation_role,
                link.from_section_id, link.target_section_id, link.origin, link.semantic_direction)

    prior = {key(link): link for link in previous if link.origin != 'AUTHOR'}
    result = [link.model_copy(update={'position': prior[key(link)].position})
              if key(link) in prior else link for link in generated]
    present = {key(link) for link in result}
    result.extend(link for link in previous if key(link) not in present and
                  (link.origin == 'AUTHOR' or link.support_revision_ids and set(link.support_revision_ids) <= effective))
    return result


def reusable_section(store, previous, prior, entries, entity_mode):
    if previous is None:
        return False
    ids = {i["id"] for i, _, _ in entries}
    if prior == ids:
        return True
    pure = bool(prior) and prior <= ids and all(i["id"] in prior or i["action"] == "ADD_EVIDENCE" for i, _, _ in entries)
    if pure:
        with store.db.engine.connect() as c:
            if all(store.content.valid_revision(c, i.atomic_revision_id) for i in previous.inputs):
                return True
    before = {f.sha256 for value in previous.inputs for f in value.fragments}
    return entity_mode and bool(before) and before == {f["sha256"] for _, f, _ in entries}


def build(store, lease, calls):
    identity = lease.input["entity_id"]
    with store.db.engine.connect() as c:
        state = one(c, "SELECT * FROM concept_work_state WHERE concept_id=:id", id=identity)
        note = one(c, "SELECT * FROM notes WHERE id=:id", id=identity)
        items = desired(c, identity)
        claim = one(c, 'SELECT definition_json FROM concept_identity_claims WHERE concept_id=:id', id=identity)
    definition = json.loads(claim['definition_json']) if claim else {}
    if not state or state["desired_generation"] != lease.input["payload"]["generation"]:
        return {"status": "SUPERSEDED"}
    generation = state["desired_generation"]
    digest = signature(items)
    base = note["current_revision_id"]
    repair_findings = lease.input['payload'].get('repair_findings', [])
    repair_sections = {f['section_id'] for f in repair_findings}
    explicit_update = repair_findings or lease.input['payload'].get('media_refresh')
    if explicit_update and lease.input['payload'].get('expected_revision') != base:
        return {'status':'SUPERSEDED'}
    if digest != state["desired_input_hash"]:
        raise Conflict("Effective inputs changed; reconcile required")
    if note["visibility"] != "ACTIVE":
        return {"status": "MANUALLY_HIDDEN"}
    old = None
    old_report = {}
    base_published = False
    if base:
        with store.db.engine.connect() as c:
            base_published = (
                one(c, "SELECT status FROM note_revisions WHERE id=:id", id=base)["status"]
                == "PUBLISHED"
            )
            proof = one(
                c,
                "SELECT * FROM build_artifacts WHERE storage_ref=:ref",
                ref=f"artifacts/builds/{base}.json",
            )
        payload = json.loads(store.read(proof["storage_ref"], proof["sha256"]))
        old = Candidate.model_validate(payload["candidate"])
        old_report = payload["review"]["report"]
        # Metadata-only versions reuse the same checked sections; retain their proof chain.
        proof_report = old_report
        seen_proofs = set()
        while proof_report.get("metadata_only") and proof_report.get("prior_revision"):
            prior_revision = proof_report["prior_revision"]
            if prior_revision in seen_proofs:
                raise QualityFailure("Cyclic metadata review provenance")
            seen_proofs.add(prior_revision)
            with store.db.engine.connect() as c:
                prior_proof = one(c, "SELECT storage_ref,sha256 FROM build_artifacts WHERE storage_ref=:ref", ref=f"artifacts/builds/{prior_revision}.json")
            if not prior_proof:
                raise QualityFailure("Missing metadata predecessor proof")
            prior_payload = json.loads(store.read(prior_proof["storage_ref"], prior_proof["sha256"]))
            prior_candidate = Candidate.model_validate(prior_payload["candidate"])
            if prior_candidate.sections != old.sections:
                raise QualityFailure("Metadata-only revision changed checked sections")
            proof_report = prior_payload["review"]["report"]
            for field in ("checks", "section_contributions"):
                if field in proof_report:
                    old_report.setdefault(field, proof_report[field])

    from .core_state import core_summary, foundation_state
    foundation = foundation_state(items, definition, old_report)
    concise_mode = any(json.loads(i['decision_json']).get('_content_policy') == 'concise-foundation-v1' for i in items)
    rebase = concise_mode and foundation['rebase']

    def guard(c):
        require_lease(c, lease)
        latest = one(c, "SELECT * FROM concept_work_state WHERE concept_id=:id", id=identity)
        if latest["desired_generation"] != generation or signature(desired(c, identity)) != digest:
            raise Conflict("Concept generation or effective inputs changed")
        if explicit_update and one(c, 'SELECT current_revision_id FROM notes WHERE id=:id', id=identity)['current_revision_id'] != base:
            raise Conflict('Concept changed during targeted correction')

    def accepted(c, revision):
        if definition.get('_identity_boundary'):
            c.execute(text('UPDATE notes SET aliases_json=:aliases WHERE id=:id'),
                      {'aliases': canonical(definition['_identity_boundary'].get('aliases', [])), 'id': identity})
        if concise_mode and claim:
            updated = {**definition, '_source_role': 'FOUNDATION' if foundation['foundation'] else 'SUPPLEMENT',
                       '_foundation_state': foundation['state'], '_foundation_input_hash': foundation['input_hash']}
            c.execute(text('UPDATE concept_identity_claims SET definition_json=:v WHERE concept_id=:id'),
                      {'v': canonical(updated), 'id': identity})
        c.execute(
            text(
                "UPDATE concept_work_state SET completed_generation=:g,status='READY' WHERE concept_id=:id"
            ),
            {"g": generation, "id": identity},
        )
        c.execute(
            text(
                "UPDATE contribution_decisions SET applied_revision_id=:r WHERE concept_id=:id AND status='ACCEPTED' AND id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"r": revision, "id": identity, "ids": [i["id"] for i in items]},
        )
        store.repository.event(
            c,
            "MOC_ENTRY_SUGGESTED",
            identity,
            revision,
            {"title": note["title"]},
            key=f"map-suggestion:{revision}",
        )

    entity_mode = any(
        json.loads(i["decision_json"]).get("_entity_policy") == "entity-concepts-v2" for i in items
    )
    grouped = {}
    links = []  # Rebuild this derived projection from current LINK_ONLY inputs.
    for item in items:
        fragment = json.loads(item["fragment_json"])
        decision = json.loads(item["decision_json"])
        if item["action"] == "LINK_ONLY":
            with store.db.engine.connect() as c:
                target = one(
                    c,
                    "SELECT note_id FROM note_revisions WHERE id=:id",
                    id=item["atomic_revision_id"],
                )
            link = Link(
                target_note_id=target["note_id"],
                target_section_id=fragment['section_id'],
                relation="LINKS_TO",
                status="CONFIRMED",
                basis=decision["reason"],
                origin='SYSTEM',
            )
            if link not in links:
                links.append(link)
        else:
            key = (
                item["section_id"]
                or "section_" + sha(decision.get("section_question") or fragment["section_id"])[:20]
            )
            grouped.setdefault(key, []).append((item, fragment, decision))
    if entity_mode:
        from .topic_partitions import partition_topics
        grouped = partition_topics(grouped, protected={s.section_id for s in old.sections if s.protected} if old else ())
        from .entity_routing import links_for

        links.extend(links_for(store, identity, items))
        if old:
            effective = {i["atomic_revision_id"] for i in items}
            links = retain_links(links, old.links, effective)
    if old:
        for section in old.sections:
            if section.protected and section.section_id not in grouped:
                raise QualityFailure("Unsupported protected chapter needs author review")
    from ..relation_overrides import apply_overrides
    links = apply_overrides(store, identity, links)
    if not grouped:
        with store.repository.transaction() as c:
            guard(c)
            if base:
                current = one(c, "SELECT current_revision_id FROM notes WHERE id=:id", id=identity)
                if current["current_revision_id"] != base:
                    raise Conflict("Concept head changed")
                c.execute(
                    text("UPDATE note_revisions SET status='WITHDRAWN' WHERE id=:id"), {"id": base}
                )
                store.repository.advance_epoch(c)
                store.repository.event(
                    c,
                    "NOTE_INVALIDATED",
                    identity,
                    base,
                    {"reason": "No current atomic support"},
                    key=f"unsupported:{identity}:{generation}",
                )
            c.execute(
                text(
                    "UPDATE concept_work_state SET completed_generation=:g,status='NO_SUPPORTED_INPUT' WHERE concept_id=:id"
                ),
                {"g": generation, "id": identity},
            )
        return {"status": "NO_SUPPORTED_INPUT"}
    old_sections = {s.section_id: s for s in old.sections} if old else {}
    order = [s.section_id for s in old.sections if s.section_id in grouped] if old else []
    order.extend(k for k in grouped if k not in order)
    if entity_mode and old is None:
        from .entity_contracts import ROLE_TITLES

        positions = {"entity_" + role: n for n, role in enumerate(ROLE_TITLES)}
        order.sort(key=lambda k: positions.get(k, 99))
    sections = []
    used = {}
    checks = {}
    prepared = {}
    if entity_mode and getattr(calls, "policy", None) in {"single-check-v1", "structured-check-v2"}:
        specs = []
        for key in order:
            entries, previous = grouped[key], old_sections.get(key)
            prior = set(old_report.get("section_contributions", {}).get(key, []))
            if not rebase and key not in repair_sections and reusable_section(store, previous, prior, entries, entity_mode):
                continue
            if previous and previous.protected:
                raise QualityFailure("Protected chapter cannot be automatically rewritten")
            declared = chapter_declaration(entries)
            title = previous.title if previous else declared[2].get("section_title") or declared[1]["title"]
            question = previous.question if previous else declared[2].get("section_question") or title
            concise = any(d.get('_content_policy') == 'concise-foundation-v1' for _, _, d in entries)
            prior_valid = concise and not rebase and previous and key not in repair_sections and prior <= {i['id'] for i, _, _ in entries}
            previous_safe = previous and not rebase and prior <= {i['id'] for i, _, _ in entries}
            changed = [(i, f, d) for i, f, d in entries if i['id'] not in prior and i['action'] != 'ADD_EVIDENCE'] if prior_valid else entries
            from .entity_writing import retained_assets
            specs.append({"key": key, "title": title, "question": question,
                          "entity": {"title": note["title"], "domain": note["domain"]},
                          "topics": list(dict.fromkeys(d.get("topic") or f["section_title"] for _, f, d in entries)),
                          "scope": (previous.scope if previous else Scope(includes=[question])).model_dump(),
                          "fragments": [f for _, f, _ in changed],
                          "retained_images": retained_assets(store,[f for _,f,_ in entries],previous.markdown) if prior_valid else {},
                          # A core whose support was removed must be rebuilt
                          # from the surviving evidence. Giving its old prose to
                          # the writer/reviewer can silently preserve lost facts.
                          "previous": previous.markdown if previous and (not concise or previous_safe) else None,
                          'core_rebuild_reason': 'FOUNDATION_REBASE' if rebase else 'SUPPORT_OR_PROOF_CHANGED' if concise and previous and not previous_safe else None,
                          "content_policy": 'concise-foundation-v1' if concise else 'detailed-v1',
                          'foundation_state': foundation['state'], 'foundation_rebase': rebase,
                          'repair_findings':[f for f in repair_findings if f['section_id'] == key],
                          "accepted_core": {'markdown': previous.markdown, 'revision': base,
                              'provenance': 'CURRENT_VALID_CHECKED_CORE', 'support_count': len(prior)} if prior_valid else None,
                          "links": [link.model_dump() for link in links if set(link.support_revision_ids) & {i["atomic_revision_id"] for i, _, _ in entries}]})
        from .entity_patch import write_entity_sections
        prepared = write_entity_sections(store, calls, specs)
    for key in order:
        entries = grouped[key]
        previous = old_sections.get(key)
        ids = [i["id"] for i, _, _ in entries]
        used[key] = ids
        prior = set(old_report.get("section_contributions", {}).get(key, []))
        same = previous is not None and prior == set(ids)
        pure = (
            previous is not None
            and prior
            and prior <= set(ids)
            and all(i["id"] in prior or i["action"] == "ADD_EVIDENCE" for i, _, _ in entries)
        )
        if pure:
            with store.db.engine.connect() as c:
                pure = all(
                    store.content.valid_revision(c, i.atomic_revision_id) for i in previous.inputs
                )
        if entity_mode and previous:
            before = {f.sha256 for value in previous.inputs for f in value.fragments}
            after = {f["sha256"] for _, f, _ in entries}
            # Identical supported text can replace an invalidated source without a rewrite.
            same = same or (bool(before) and before == after)
        declared = chapter_declaration(entries)
        title = (
            previous.title if previous else declared[2].get("section_title") or declared[1]["title"]
        )
        question = previous.question if previous else declared[2].get("section_question") or title
        scope = previous.scope if previous else Scope(includes=[question])
        if key in repair_sections or rebase:
            same = pure = False
        if same or pure:
            markdown = previous.markdown
            checks[key] = {
                **old_report.get("checks", {}).get(key, {}),
                "writer_called": False,
                "reused_from_revision": base,
            }
            if entity_mode:
                from .entity_writing import asset_packet

                checks[key]["assets"] = asset_packet(store, [f for _, f, _ in entries])
        else:
            if previous and previous.protected:
                raise QualityFailure("Protected chapter cannot be automatically rewritten")
            if key in prepared:
                markdown, checks[key] = prepared[key]
            elif entity_mode:
                from .entity_writing import write_entity_chapter

                markdown, checks[key] = write_entity_chapter(
                    store,
                    calls,
                    key,
                    title,
                    question,
                    scope,
                    [f for _, f, _ in entries],
                    previous.markdown if previous else None,
                    [
                        link.model_dump()
                        for link in links
                        if set(link.support_revision_ids)
                        & {i["atomic_revision_id"] for i, _, _ in entries}
                    ],
                    repair_findings=[f for f in repair_findings if f['section_id'] == key],
                )
            else:
                markdown, checks[key] = write_chapter(
                    calls,
                    key,
                    title,
                    question,
                    scope,
                    [f for _, f, _ in entries],
                    previous.markdown if previous else None,
                )
        if entity_mode and concise_mode:
            from .media import candidates as media_candidates
            from .media import maintain as maintain_media
            from .media import records as media_records
            current_fragments = [f for _, f, _ in entries]
            media_requests = {d.get('media_contribution', 'AUTO') for _, _, d in entries}
            media_request = next((mode for mode in ('BETTER_FIGURE', 'KEY_FIGURE')
                                  if mode in media_requests),
                                 'NONE' if media_requests == {'NONE'} else 'AUTO')
            if same or pure:
                markdown, media_receipt = maintain_media(store, calls, note['title'], markdown,
                    current_fragments, old_report.get('checks', {}).get(key, {}).get('media'),
                    force=bool(lease.input['payload'].get('media_refresh')), requested=media_request)
                checks[key]['media'] = media_receipt
            else:
                checks[key]['media'] = {'policy': 'concept-key-figures-v1', 'called': False,
                    'selected': media_records(markdown, media_candidates(store, current_fragments))}
        contributions = []
        evidence = set()
        for item, f, d in entries:
            evidence.update(f["evidence_ids"])
            contributions.append(
                Input(
                    atomic_revision_id=item["atomic_revision_id"],
                    section_ids=[f["section_id"]],
                    fragments=[
                        FragmentRef(
                            section_id=f["section_id"],
                            start_offset=f["start_offset"],
                            end_offset=f["end_offset"],
                            sha256=f["sha256"],
                        )
                    ],
                    evidence_ids=f["evidence_ids"],
                    action=item["action"],
                    role=d["role"],
                )
            )
        sections.append(
            Section(
                section_id=key,
                title=title,
                question=question,
                scope=scope,
                markdown=markdown,
                evidence_ids=sorted(evidence),
                inputs=contributions,
                protected=previous.protected if previous else False,
            )
        )
    candidate = Candidate(
        note_id=identity,
        title=definition.get('_identity_boundary', {}).get('title') or note["title"],
        summary=core_summary(sections, note['title']) if concise_mode else old.summary
        if old
        else definition.get("description", definition.get("question", "")),
        scope=Scope.model_validate(definition.get('scope') or {'includes': [note['title']]}) if concise_mode else published_scope(
            old.scope if old else Scope.model_validate(definition["scope"]), sections,
            [d["topic"] for i in items if (d := json.loads(i["decision_json"])).get("topic")],
        ),
        sections=sections,
        links=links,
        policy_version=store.repository.policy_version,
    )
    if old and base_published and candidate.fingerprint == old.fingerprint:
        with store.repository.transaction() as c:
            guard(c)
            accepted(c, base)
        return {"status": "UNCHANGED", "revision_id": base}
    result = store.publish(
        candidate,
        expected_revision=base,
        review=Review(
            candidate_sha256=candidate.fingerprint,
            policy_version=candidate.policy_version,
            reviewer="next.concept_review",
            passed=True,
            report={
                "section_contributions": used,
                "checks": checks,
                "input_snapshot_sha256": digest,
                "generation": generation,
                **({'foundation': foundation, 'core_coverage': {
                    'policy': 'core-coverage-v1',
                    'sections': [{'section_id': s.section_id, 'body_sha256': sha(s.markdown),
                                  'contribution_ids': used[s.section_id]} for s in sections]}} if concise_mode else {}),
                **({'targeted_correction':{'base_revision':base, 'findings':repair_findings}} if repair_findings else {}),
            },
        ),
        lease=lease,
        guard=guard,
        accepted=accepted,
    )
    return {"status": "PUBLISHED", **result}
