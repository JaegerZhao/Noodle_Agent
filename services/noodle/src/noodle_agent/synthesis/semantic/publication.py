from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from sqlalchemy import func, select

from ...composition import Component
from ...db import (
    BackboneCorpus,
    BlobObject,
    Concept,
    ConceptBuildRecord,
    ConceptContribution,
    ConceptModule,
    ConceptModuleAsset,
    ConceptModuleVersion,
    ConceptRelation,
    ConceptReleaseModule,
    ConceptVersion,
    NoteAsset,
    Synthesis,
    SynthesisNode,
    SynthesisReferenceSource,
    SynthesisVersion,
    VisualAsset,
)
from ..blueprints import get_blueprint
from ..domain import (
    SemanticSynthesisReport,
)
from ..machine_learning_blueprint import ConceptSpec
from .common import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    _json,
    _sha256_text,
)


class PublicationModule(Component):
    def _compile_one_locked(self, prepared: dict[str, Any]) -> dict[str, Any]:
        spec: ConceptSpec = prepared["spec"]
        modules: list[dict[str, Any]] = prepared["modules"]
        if not modules:
            raise ValueError(
                f"No source modules mapped to {self.owner.blueprint.stable_concept_key(spec)}"
            )
        input_payload = {
            "spec": spec,
            "modules": [
                {
                    "block_key": item["block_key"],
                    "semantic_role": item["semantic_role"],
                    "sources": sorted(
                        (
                            source["note_version_id"],
                            source["content_sha256"],
                            source.get("contribution_role", "FOUNDATION"),
                        )
                        for source in item["sources"]
                    ),
                }
                for item in modules
            ],
        }
        input_hash = _sha256_text(_json(input_payload))
        with self.owner.db.session() as session:
            concept = session.scalar(
                select(Concept).where(
                    Concept.stable_key == self.owner.blueprint.stable_concept_key(spec)
                )
            )
            record = session.scalar(
                select(ConceptBuildRecord).where(
                    ConceptBuildRecord.concept_id == concept.id,
                    ConceptBuildRecord.input_sha256 == input_hash,
                    ConceptBuildRecord.prompt_version == PROMPT_VERSION,
                )
            )
            if record is not None:
                concept.active_version_id = record.concept_version_id
                concept.status = "PUBLISHED"
                return {
                    "concept_id": concept.id,
                    "concept_version_id": record.concept_version_id,
                    "reused": True,
                    "fallback": False,
                    "fallback_detail": "",
                }

        draft, fallback, fallback_detail = self.owner._write_draft(spec, modules)
        with self.owner.db.session() as session:
            concept = session.scalar(
                select(Concept).where(
                    Concept.stable_key == self.owner.blueprint.stable_concept_key(spec)
                )
            )
            module_versions: list[tuple[ConceptModule, ConceptModuleVersion]] = []
            contribution_count = 0
            for module_draft, seed in zip(draft.modules, modules, strict=True):
                module = session.scalar(
                    select(ConceptModule).where(
                        ConceptModule.concept_id == concept.id,
                        ConceptModule.module_key == module_draft.block_key,
                    )
                )
                if module is None:
                    module = ConceptModule(
                        concept_id=concept.id,
                        module_key=module_draft.block_key,
                        semantic_role=module_draft.semantic_role,
                    )
                    session.add(module)
                    session.flush()
                else:
                    module.semantic_role = module_draft.semantic_role
                    module.visibility = "ACTIVE"
                note_version_ids = [item["note_version_id"] for item in seed["sources"]]
                contribution_snapshot = [
                    {
                        "contribution_id": item.get("contribution_id", ""),
                        "note_version_id": item["note_version_id"],
                        "source_ref": item.get("source_ref", ""),
                        "role": item.get("contribution_role", "FOUNDATION"),
                    }
                    for item in seed["sources"]
                ]
                contribution_count += len(contribution_snapshot)
                assets = (
                    session.execute(
                        select(NoteAsset, VisualAsset, BlobObject)
                        .join(VisualAsset, VisualAsset.id == NoteAsset.visual_asset_id)
                        .join(BlobObject, BlobObject.id == VisualAsset.blob_id)
                        .where(NoteAsset.note_version_id.in_(note_version_ids))
                        .order_by(NoteAsset.order_index)
                    ).all()
                    if note_version_ids
                    else []
                )
                unique_assets: list[tuple[NoteAsset, VisualAsset, BlobObject]] = []
                seen_assets: set[str] = set()
                for row in assets:
                    if row[1].id in seen_assets:
                        continue
                    seen_assets.add(row[1].id)
                    unique_assets.append(row)
                stored_payload = module_draft.payload.model_dump()
                stored_payload["_contributions"] = contribution_snapshot
                module_hash = _sha256_text(
                    _json(
                        {
                            "draft": module_draft.model_dump(),
                            "contributions": contribution_snapshot,
                            "asset_ids": [row[1].id for row in unique_assets],
                        }
                    )
                )
                module_version = session.scalar(
                    select(ConceptModuleVersion).where(
                        ConceptModuleVersion.module_id == module.id,
                        ConceptModuleVersion.content_sha256 == module_hash,
                    )
                )
                if module_version is None:
                    version_no = (
                        session.scalar(
                            select(func.max(ConceptModuleVersion.version_no)).where(
                                ConceptModuleVersion.module_id == module.id
                            )
                        )
                        or 0
                    ) + 1
                    module_version = ConceptModuleVersion(
                        module_id=module.id,
                        version_no=version_no,
                        render_type=module_draft.render_type,
                        title=module_draft.title,
                        payload_json=_json(stored_payload),
                        plain_text=module_draft.plain_text(),
                        origin="ATOMIC_SYNTHESIZED",
                        lock_state="OPEN",
                        content_sha256=module_hash,
                        created_by=self.owner.agent.name,
                    )
                    session.add(module_version)
                    session.flush()
                module_versions.append((module, module_version))
                for order, (note_asset, visual, _blob) in enumerate(unique_assets):
                    placement = session.scalar(
                        select(ConceptModuleAsset).where(
                            ConceptModuleAsset.module_version_id == module_version.id,
                            ConceptModuleAsset.visual_asset_id == visual.id,
                        )
                    )
                    if placement is None:
                        session.add(
                            ConceptModuleAsset(
                                module_version_id=module_version.id,
                                visual_asset_id=visual.id,
                                order_index=order,
                                placement=note_asset.placement,
                                caption=visual.caption,
                                alt_text=note_asset.alt_text,
                            )
                        )

            release_hash = _sha256_text(
                _json(
                    {
                        "title": draft.title_zh,
                        "summary": draft.summary,
                        "scope": draft.scope.model_dump(),
                        "modules": [item.id for _module, item in module_versions],
                    }
                )
            )
            release = session.scalar(
                select(ConceptVersion).where(
                    ConceptVersion.concept_id == concept.id,
                    ConceptVersion.content_sha256 == release_hash,
                    ConceptVersion.schema_version == SCHEMA_VERSION,
                )
            )
            if release is None:
                version_no = (
                    session.scalar(
                        select(func.max(ConceptVersion.version_no)).where(
                            ConceptVersion.concept_id == concept.id
                        )
                    )
                    or 0
                ) + 1
                release = ConceptVersion(
                    concept_id=concept.id,
                    version_no=version_no,
                    title=draft.title_zh,
                    original_title=spec["title"],
                    scope=_json(draft.scope.model_dump()),
                    summary=draft.summary,
                    content_sha256=release_hash,
                    schema_version=SCHEMA_VERSION,
                    prompt_version=PROMPT_VERSION,
                    status="PUBLISHED",
                )
                session.add(release)
                session.flush()
                for order, (module, module_version) in enumerate(module_versions):
                    session.add(
                        ConceptReleaseModule(
                            concept_version_id=release.id,
                            module_id=module.id,
                            module_version_id=module_version.id,
                            sort_order=order,
                        )
                    )
            concept.canonical_name = draft.title_zh
            concept.original_name = spec["title"]
            concept.active_version_id = release.id
            concept.status = "PUBLISHED"
            concept.visibility = "ACTIVE"
            session.add(
                ConceptBuildRecord(
                    concept_id=concept.id,
                    concept_version_id=release.id,
                    input_sha256=input_hash,
                    prompt_version=PROMPT_VERSION,
                    model_name=self.owner.agent.name,
                )
            )
            return {
                "concept_id": concept.id,
                "concept_version_id": release.id,
                "reused": False,
                "fallback": fallback,
                "fallback_detail": fallback_detail,
                "contributions": contribution_count,
            }

    def _refresh_synthesis_releases(
        self,
        concept_versions: dict[str, str],
        *,
        source_version_id: str | None = None,
        source_role: str = "SUPPORT",
        exclude_synthesis_id: str | None = None,
    ) -> list[str]:
        """Clone active releases that reference an updated global Concept."""

        refreshed: list[str] = []
        if not concept_versions:
            return refreshed
        with self.owner.db.session() as session:
            syntheses = session.scalars(
                select(Synthesis).where(
                    Synthesis.visibility == "ACTIVE",
                    Synthesis.active_version_id.is_not(None),
                )
            ).all()
            for synthesis in syntheses:
                if synthesis.id == exclude_synthesis_id:
                    continue
                old_version = session.get(SynthesisVersion, synthesis.active_version_id)
                if old_version is None:
                    continue
                old_nodes = session.scalars(
                    select(SynthesisNode)
                    .where(SynthesisNode.synthesis_version_id == old_version.id)
                    .order_by(SynthesisNode.sort_order)
                ).all()
                if not any(node.concept_id in concept_versions for node in old_nodes):
                    continue
                target_versions = {
                    node.id: concept_versions.get(node.concept_id, node.concept_version_id)
                    for node in old_nodes
                }
                if all(target_versions[node.id] == node.concept_version_id for node in old_nodes):
                    continue
                release_hash = _sha256_text(
                    _json(
                        [
                            (
                                node.stable_key,
                                node.parent_id,
                                target_versions[node.id],
                                node.sort_order,
                            )
                            for node in old_nodes
                        ]
                    )
                )
                existing = session.scalar(
                    select(SynthesisVersion).where(
                        SynthesisVersion.synthesis_id == synthesis.id,
                        SynthesisVersion.content_sha256 == release_hash,
                    )
                )
                if existing is not None:
                    synthesis.active_version_id = existing.id
                    refreshed.append(existing.id)
                    continue
                version_no = (
                    session.scalar(
                        select(func.max(SynthesisVersion.version_no)).where(
                            SynthesisVersion.synthesis_id == synthesis.id
                        )
                    )
                    or 0
                ) + 1
                new_version = SynthesisVersion(
                    synthesis_id=synthesis.id,
                    backbone_corpus_id=old_version.backbone_corpus_id,
                    version_no=version_no,
                    title=old_version.title,
                    summary=old_version.summary,
                    status="PUBLISHED",
                    coverage_json=old_version.coverage_json,
                    content_sha256=release_hash,
                )
                session.add(new_version)
                session.flush()
                node_ids: dict[str, str] = {}
                for old_node in old_nodes:
                    new_node = SynthesisNode(
                        synthesis_version_id=new_version.id,
                        stable_key=old_node.stable_key,
                        parent_id=node_ids.get(old_node.parent_id or ""),
                        title=old_node.title,
                        original_title=old_node.original_title,
                        level=old_node.level,
                        sort_order=old_node.sort_order,
                        node_kind=old_node.node_kind,
                        concept_id=old_node.concept_id,
                        concept_version_id=target_versions[old_node.id],
                        source_locator_json=old_node.source_locator_json,
                    )
                    session.add(new_node)
                    session.flush()
                    node_ids[old_node.id] = new_node.id
                references = session.scalars(
                    select(SynthesisReferenceSource).where(
                        SynthesisReferenceSource.synthesis_version_id == old_version.id
                    )
                ).all()
                referenced_ids = {item.source_version_id for item in references}
                for reference in references:
                    session.add(
                        SynthesisReferenceSource(
                            synthesis_version_id=new_version.id,
                            source_version_id=reference.source_version_id,
                            source_role=reference.source_role,
                        )
                    )
                if source_version_id and source_version_id not in referenced_ids:
                    session.add(
                        SynthesisReferenceSource(
                            synthesis_version_id=new_version.id,
                            source_version_id=source_version_id,
                            source_role=source_role,
                        )
                    )
                synthesis.active_version_id = new_version.id
                refreshed.append(new_version.id)
        return refreshed

    def refresh_shared_concept_versions(self) -> dict[str, Any]:
        """Repin active Syntheses to each shared Concept's active version.

        The operation is deterministic and does not call an LLM. It is useful
        after importing historical data, repairing a partial build, or changing
        a Concept's active version outside the normal contribution path.
        """

        with self.owner.db.session() as session:
            concepts = session.scalars(
                select(Concept).where(
                    Concept.visibility == "ACTIVE",
                    Concept.active_version_id.is_not(None),
                )
            ).all()
            active_versions: dict[str, str] = {}
            skipped: list[dict[str, str]] = []
            for concept in concepts:
                version = session.get(ConceptVersion, concept.active_version_id)
                if (
                    version is None
                    or version.concept_id != concept.id
                    or version.status != "PUBLISHED"
                ):
                    skipped.append(
                        {
                            "concept_id": concept.id,
                            "stable_key": concept.stable_key,
                            "active_version_id": concept.active_version_id or "",
                        }
                    )
                    continue
                active_versions[concept.id] = version.id
        refreshed = self.owner._refresh_synthesis_releases(active_versions)
        return {
            "status": "REFRESHED",
            "active_concepts": len(active_versions),
            "refreshed_synthesis_versions": refreshed,
            "skipped_concepts": skipped,
        }

    def _publish_synthesis(
        self,
        corpus_id: str,
        source_version_id: str,
        *,
        allow_replace_legacy: bool,
        compiled_versions: dict[str, str],
        failures: list[dict[str, str]],
    ) -> tuple[str, str, int]:
        with self.owner.db.session() as session:
            corpus = session.get(BackboneCorpus, corpus_id)
            synthesis = session.scalar(
                select(Synthesis).where(Synthesis.stable_key == self.owner.blueprint.synthesis_key)
            )
            if synthesis is None:
                synthesis = Synthesis(
                    stable_key=self.owner.blueprint.synthesis_key,
                    domain_key=self.owner.blueprint.domain_key,
                    title=self.owner.blueprint.title,
                    description=(
                        "按稳定知识主题组织、跨教材复用，并由来源 AtomicNote 持续补充的语义 Synthesis。"
                    ),
                )
                session.add(synthesis)
                session.flush()
            synthesis.visibility = "ACTIVE"
            previous_active_version_id = synthesis.active_version_id
            concepts = {
                item.stable_key: item
                for item in session.scalars(
                    select(Concept).where(
                        Concept.stable_key.in_(
                            [
                                self.owner.blueprint.stable_concept_key(item)
                                for item in self.owner.blueprint.concepts
                            ]
                        )
                    )
                ).all()
            }
            selected_versions = {
                item.stable_key: compiled_versions.get(item.id, item.active_version_id or "")
                for item in concepts.values()
            }
            release_hash = _sha256_text(
                _json(
                    {
                        "groups": self.owner.blueprint.groups,
                        "concepts": [
                            (
                                item["key"],
                                selected_versions[self.owner.blueprint.stable_concept_key(item)],
                                item["parent_key"],
                            )
                            for item in self.owner.blueprint.concepts
                        ],
                        "failed": sorted(item["concept_key"] for item in failures),
                    }
                )
            )
            version = session.scalar(
                select(SynthesisVersion).where(
                    SynthesisVersion.synthesis_id == synthesis.id,
                    SynthesisVersion.content_sha256 == release_hash,
                )
            )
            if version is None:
                version_no = (
                    session.scalar(
                        select(func.max(SynthesisVersion.version_no)).where(
                            SynthesisVersion.synthesis_id == synthesis.id
                        )
                    )
                    or 0
                ) + 1
                coverage = {
                    "concept_nodes": len(self.owner.blueprint.concepts),
                    "published_concepts": len(compiled_versions),
                    "fresh_concepts": len(compiled_versions),
                    "failed_concepts": len(failures),
                    "groups": len(self.owner.blueprint.groups),
                    "coverage": round(
                        len(compiled_versions) / len(self.owner.blueprint.concepts),
                        4,
                    ),
                    "semantic_backbone": True,
                }
                release_status = (
                    "PUBLISHED"
                    if not failures
                    and coverage["fresh_concepts"] == len(self.owner.blueprint.concepts)
                    else "PARTIAL"
                )
                version = SynthesisVersion(
                    synthesis_id=synthesis.id,
                    backbone_corpus_id=corpus.id,
                    version_no=version_no,
                    title=self.owner.blueprint.release_title,
                    summary=self.owner.blueprint.summary,
                    status=release_status,
                    coverage_json=_json(coverage),
                    content_sha256=release_hash,
                )
                session.add(version)
                session.flush()
                node_ids: dict[str, str] = {}
                sort_order = 0
                for group in self.owner.blueprint.groups:
                    parent_id = node_ids.get(group["parent_key"] or "")
                    node = SynthesisNode(
                        synthesis_version_id=version.id,
                        stable_key=f"{self.owner.blueprint.concept_namespace}-group:{group['key']}",
                        parent_id=parent_id,
                        title=group["title"],
                        original_title=group["title"],
                        level=1,
                        sort_order=sort_order,
                        node_kind="GROUP",
                        source_locator_json=_json({"semantic": True}),
                    )
                    session.add(node)
                    session.flush()
                    node_ids[group["key"]] = node.id
                    sort_order += 1
                    for spec in [
                        item
                        for item in self.owner.blueprint.concepts
                        if item["parent_key"] == group["key"]
                    ]:
                        sort_order = self.owner._append_concept_node(
                            session,
                            version.id,
                            spec,
                            concepts,
                            selected_versions,
                            node_ids,
                            sort_order,
                            level=2,
                        )
                session.add(
                    SynthesisReferenceSource(
                        synthesis_version_id=version.id,
                        source_version_id=source_version_id,
                        source_role="FOUNDATION",
                    )
                )
            if version.status == "PUBLISHED" or previous_active_version_id is None:
                synthesis.active_version_id = version.id

            for spec in self.owner.blueprint.concepts:
                if spec["parent_key"] in {item["key"] for item in self.owner.blueprint.concepts}:
                    parent_spec = next(
                        item
                        for item in self.owner.blueprint.concepts
                        if item["key"] == spec["parent_key"]
                    )
                    source = concepts[self.owner.blueprint.stable_concept_key(parent_spec)]
                    target = concepts[self.owner.blueprint.stable_concept_key(spec)]
                    relation = session.scalar(
                        select(ConceptRelation).where(
                            ConceptRelation.source_concept_id == target.id,
                            ConceptRelation.target_concept_id == source.id,
                            ConceptRelation.relation_type == "PART_OF",
                        )
                    )
                    if relation is None:
                        session.add(
                            ConceptRelation(
                                source_concept_id=target.id,
                                target_concept_id=source.id,
                                relation_type="PART_OF",
                                source_ref=self.owner.blueprint.synthesis_key,
                            )
                        )
            if allow_replace_legacy and self.owner.blueprint.legacy_synthesis_key:
                legacy = session.scalar(
                    select(Synthesis).where(
                        Synthesis.stable_key == self.owner.blueprint.legacy_synthesis_key
                    )
                )
                if legacy is not None:
                    legacy.visibility = "HIDDEN"
            return synthesis.id, version.id, len(self.owner.blueprint.concepts)

    def _append_concept_node(
        self,
        session,
        synthesis_version_id: str,
        spec: ConceptSpec,
        concepts: dict[str, Concept],
        selected_versions: dict[str, str],
        node_ids: dict[str, str],
        sort_order: int,
        *,
        level: int,
    ) -> int:
        concept = concepts[self.owner.blueprint.stable_concept_key(spec)]
        node = SynthesisNode(
            synthesis_version_id=synthesis_version_id,
            stable_key=concept.stable_key,
            parent_id=node_ids.get(spec["parent_key"]),
            title=concept.canonical_name,
            original_title=spec["title"],
            level=level,
            sort_order=sort_order,
            node_kind="CONCEPT",
            concept_id=concept.id,
            concept_version_id=selected_versions[concept.stable_key],
            source_locator_json=_json(
                {"semantic": True, "source_prefixes": spec["source_prefixes"]}
            ),
        )
        session.add(node)
        session.flush()
        node_ids[spec["key"]] = node.id
        sort_order += 1
        for child in [
            item for item in self.owner.blueprint.concepts if item["parent_key"] == spec["key"]
        ]:
            sort_order = self.owner._append_concept_node(
                session,
                synthesis_version_id,
                child,
                concepts,
                selected_versions,
                node_ids,
                sort_order,
                level=level + 1,
            )
        return sort_order

    def build(
        self, corpus_id: str, source_version_id: str, *, concurrency: int = 4
    ) -> SemanticSynthesisReport:
        with self.owner.db.session() as session:
            corpus = session.get(BackboneCorpus, corpus_id)
            if corpus is None:
                raise ValueError(f"Unknown corpus_id: {corpus_id}")
            self.owner.blueprint = get_blueprint(corpus.domain_key)
        self.owner._ensure_concepts()
        prepared, _section_keys = self.owner._prepare_inputs(corpus_id, source_version_id)
        self.owner._register_foundation_contributions(prepared)
        prepared = {
            spec["key"]: self.owner._prepared_from_contributions(self.owner._global_spec(spec))
            for spec in self.owner.blueprint.concepts
        }
        results: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
            futures = {
                executor.submit(self.owner._compile_one, prepared[spec["key"]]): spec
                for spec in self.owner.blueprint.concepts
            }
            for future in as_completed(futures):
                spec = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    failures.append({"concept_key": spec["key"], "error": str(exc)})
        compiled_versions = {item["concept_id"]: item["concept_version_id"] for item in results}
        synthesis_id, synthesis_version_id, concept_count = self.owner._publish_synthesis(
            corpus_id,
            source_version_id,
            allow_replace_legacy=not failures,
            compiled_versions=compiled_versions,
            failures=failures,
        )
        refreshed_synthesis_versions = (
            self.owner._refresh_synthesis_releases(
                compiled_versions,
                source_version_id=source_version_id,
                source_role="FOUNDATION",
                exclude_synthesis_id=synthesis_id,
            )
            if not failures
            else []
        )
        concept_ids = [item["concept_id"] for item in results]
        with self.owner.db.session() as session:
            active_version_ids = [
                item.active_version_id
                for item in session.scalars(select(Concept).where(Concept.id.in_(concept_ids)))
                if item.active_version_id
            ]
            active_module_ids = select(ConceptReleaseModule.module_id).where(
                ConceptReleaseModule.concept_version_id.in_(active_version_ids)
            )
            module_count = (
                session.scalar(
                    select(func.count())
                    .select_from(ConceptReleaseModule)
                    .where(ConceptReleaseModule.concept_version_id.in_(active_version_ids))
                )
                if active_version_ids
                else 0
            ) or 0
            contribution_count = (
                session.scalar(
                    select(func.count(ConceptContribution.id)).where(
                        ConceptContribution.module_id.in_(active_module_ids),
                        ConceptContribution.decision_status == "ACCEPTED",
                    )
                )
                if active_version_ids
                else 0
            ) or 0
        fallbacks = [
            {
                "concept_id": item["concept_id"],
                "detail": item.get("fallback_detail", ""),
            }
            for item in results
            if item.get("fallback")
        ]
        return SemanticSynthesisReport(
            synthesis_id=synthesis_id,
            synthesis_version_id=synthesis_version_id,
            status="PUBLISHED" if not failures else "PARTIAL",
            concepts=concept_count,
            groups=len(self.owner.blueprint.groups),
            modules=module_count,
            contributions=contribution_count,
            reused_builds=sum(bool(item.get("reused")) for item in results),
            refreshed_synthesis_versions=refreshed_synthesis_versions,
            fallbacks=fallbacks,
            failed=failures,
        )
