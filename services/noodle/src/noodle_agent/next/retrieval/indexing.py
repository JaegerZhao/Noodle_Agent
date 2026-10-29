"""Atomic generation activation over disposable lexical/vector projections."""

import json
import sqlite3
from functools import lru_cache
from pathlib import Path

import numpy as np
from sqlalchemy import text

from ..contracts import canonical, sha
from ..eligibility import eligible
from ..jobs import require_lease
from ..planning import QualityFailure
from ..repository import Conflict, identifier, insert, now, one
from .chunks import CHUNK_POLICY, compile_chunks
from .lexical import searchable
from .vectors import ExactVectors, load_matrix, matrix_bytes, normalize, profile_for


@lru_cache(maxsize=128)
def lexical_bounds(database: str, generation: str, chunk_ids: tuple[str, ...]):
    """Bound FTS postings to an immutable generation without dropping old indexes.

    A generation is inserted in one transaction. Validate the fast contiguous
    range, and fall back to exact bounds for imported/interleaved generations.
    Original generation and scope predicates remain mandatory in the query.
    """
    if not chunk_ids:
        return (0, -1)
    with sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True) as c:
        last = c.execute('SELECT rowid FROM knowledge_fts WHERE generation=? ORDER BY rowid DESC LIMIT 1', (generation,)).fetchone()
        if last is None:
            return None
        high = last[0]
        low = high - len(chunk_ids) + 1
        found = c.execute('SELECT generation,chunk_id FROM knowledge_fts WHERE rowid BETWEEN ? AND ?', (low, high)).fetchall()
        if len(found) == len(chunk_ids) and all(g == generation for g, _ in found) and {key for _, key in found} == set(chunk_ids):
            return (low, high)
        return c.execute('SELECT min(rowid),max(rowid) FROM knowledge_fts WHERE generation=?', (generation,)).fetchone()


def request_index(store, cause=None):
    with store.repository.transaction() as c:
        pending = one(
            c, "SELECT desired_generation,completed_generation FROM index_work_state WHERE id=1"
        )
        queued = one(
            c,
            "SELECT id FROM events WHERE event_type='INDEX_BUILD_REQUESTED' AND status='QUEUED' LIMIT 1",
        )
        if (
            pending["desired_generation"] > pending["completed_generation"]
            and queued
        ):
            return {"requested_generation": pending["desired_generation"], "coalesced": True}
        c.exec_driver_sql(
            "UPDATE index_work_state SET desired_generation=desired_generation+1 WHERE id=1"
        )
        generation = c.exec_driver_sql(
            "SELECT desired_generation FROM index_work_state WHERE id=1"
        ).scalar_one()
        store.repository.event(
            c,
            "INDEX_BUILD_REQUESTED",
            "knowledge-index",
            None,
            {"generation": generation, "cause": cause},
            key=f"index-demand:{generation}",
        )
    return {"requested_generation": generation}


class Indexer:
    def __init__(self, store, embedding=None, backend=None, allow_test_embeddings=False):
        self.store = store
        self.embedding = embedding
        self.backend = backend or ExactVectors()
        self.profile = profile_for(embedding, allow_test_embeddings)

    def mode_key(self, mode):
        if mode == "HYBRID" and self.profile is None:
            raise QualityFailure(
                "Embedding profile is not configured; select KEYWORD_ONLY explicitly"
            )
        return (
            mode + ":" + (self.profile["id"] if mode == "HYBRID" else "none") + ":" + CHUNK_POLICY
        )

    def build(self, mode="HYBRID", lease=None, demand=None):
        key = self.mode_key(mode)
        store = self.store
        with store.db.engine.connect() as c:
            epoch = c.exec_driver_sql(
                "SELECT validity_epoch FROM storage_contract WHERE id=1"
            ).scalar_one()
            notes = eligible(c)
            head = one(
                c,
                "SELECT generation FROM index_heads WHERE mode_key=:m AND backend=:b",
                m=key,
                b=self.backend.name,
            )
            old_head = head["generation"] if head else None
            state = one(c, "SELECT * FROM index_work_state WHERE id=1")
        if demand is not None and state["desired_generation"] != demand:
            return {"status": "SUPERSEDED"}
        chunks = []
        compiled, reused = 0, 0
        for note in notes:
            binding = {"revision": note["revision_id"], "title": note["title"],
                       "markdown": note["markdown_sha256"], "policy": CHUNK_POLICY}
            ref = f"indexes/compiled/{sha(canonical(binding))}.json"
            digest_ref = ref + ".sha256"
            if store.path(ref).exists() and store.path(digest_ref).exists():
                value = json.loads(store.read(ref, store.path(digest_ref).read_text(encoding="utf-8")))
                if value["binding"] != binding:
                    raise QualityFailure("Chunk compilation cache binding differs")
                chunks.extend(value["chunks"])
                reused += 1
            else:
                values = compile_chunks(store, store.get_note(note["id"]))
                content = canonical({"binding": binding, "chunks": values}).encode()
                store.put(ref, content)
                store.put(digest_ref, sha(content).encode())
                chunks.extend(values)
                compiled += 1
        generation = identifier()
        ids = [c["id"] for c in chunks]
        matrix = np.empty(
            (0, self.profile["dimensions"] if mode == "HYBRID" else 0), dtype=np.float32
        )
        if mode == "HYBRID" and chunks:
            vectors = [None] * len(chunks)
            missing = []
            for i, chunk in enumerate(chunks):
                ref = f"vectors/cache/{self.profile['id']}/{sha(chunk['embedding_text'])}.npy"
                chunk["vector_ref"] = ref
                digest_ref = ref + ".sha256"
                if store.path(ref).exists() and store.path(digest_ref).exists():
                    digest = store.path(digest_ref).read_text(encoding="utf-8")
                    store.read(ref, digest)
                    vectors[i] = normalize(
                        np.load(store.path(ref), allow_pickle=False), self.profile["dimensions"]
                    )[0]
                else:
                    missing.append(i)
            for start in range(0, len(missing), 20):
                batch = missing[start : start + 20]
                raw = self.embedding.embed([chunks[i]["embedding_text"] for i in batch])
                if len(raw) != len(batch):
                    raise QualityFailure("Embedding response count mismatch")
                values = normalize(raw, self.profile["dimensions"])
                for i, value in zip(batch, values, strict=True):
                    value_bytes = matrix_bytes(value[None, :])
                    store.put(chunks[i]["vector_ref"], value_bytes)
                    store.put(chunks[i]["vector_ref"] + ".sha256", sha(value_bytes).encode())
                    vectors[i] = value
            matrix = np.asarray(vectors, dtype=np.float32)
        manifest = [
            {k: v for k, v in chunk.items() if k not in {"text", "embedding_text"}}
            for chunk in chunks
        ]
        data = canonical(manifest).encode()
        manifest_ref = f"indexes/{generation}/manifest.json"
        store.put(manifest_ref, data)
        matrix_ref = None
        matrix_digest = None
        if mode == "HYBRID":
            content = matrix_bytes(matrix)
            matrix_digest = sha(content)
            matrix_ref = f"indexes/{generation}/vectors.npy"
            store.put(matrix_ref, content)
            self.backend.build(generation, ids, matrix)
        chunk_rows = [{k: chunk[k] for k in ('id', 'revision_id', 'section_id', 'start_offset', 'end_offset', 'sha256')} for chunk in chunks]
        metadata_rows = [{"id": chunk["id"], "position": chunk["position"], "tokens": chunk["tokens"],
            "meta": canonical({k: v for k, v in chunk.items() if k not in {"text", "embedding_text"}})} for chunk in chunks]
        fts_rows = [{"g": generation, "id": chunk["id"],
            "title": searchable(chunk["title"] + " " + chunk["section_title"]),
            "body": searchable(chunk["text"])} for chunk in chunks]
        entry_rows = [{"chunk_id": chunk["id"], "profile_id": self.profile["id"],
            "generation": generation, "status": "READY", "vector_ref": chunk["vector_ref"]} for chunk in chunks] if mode == "HYBRID" else []
        with store.repository.transaction() as c:
            if lease:
                require_lease(c, lease)
            actual = c.exec_driver_sql(
                "SELECT validity_epoch FROM storage_contract WHERE id=1"
            ).scalar_one()
            current = one(
                c,
                "SELECT generation FROM index_heads WHERE mode_key=:m AND backend=:b",
                m=key,
                b=self.backend.name,
            )
            valid = {n["revision_id"] for n in eligible(c, [n["revision_id"] for n in notes])}
            if valid != {n["revision_id"] for n in notes} or (current["generation"] if current else None) != old_head:
                raise Conflict("Content or active index changed before activation")
            if mode == "HYBRID":
                profile = self.profile
                c.execute(
                    text(
                        "INSERT OR IGNORE INTO embedding_profiles(id,provider,model,dimensions,normalization,text_policy,config_json) VALUES(:id,:provider,:model,:dimensions,:normalization,:text_policy,:config_json)"
                    ),
                    {**profile, "config_json": canonical(profile["config"])},
                )
            insert(
                c,
                "index_generations",
                id=generation,
                mode_key=key,
                profile_id=self.profile["id"] if mode == "HYBRID" else None,
                backend=self.backend.name,
                manifest_ref=manifest_ref,
                manifest_sha256=sha(data),
                matrix_ref=matrix_ref,
                matrix_sha256=matrix_digest,
                status="READY",
                created_at=now(),
            )
            if chunks:
                c.execute(
                    text(
                        "INSERT OR IGNORE INTO chunks(id,revision_id,section_id,start_offset,end_offset,sha256) VALUES(:id,:revision_id,:section_id,:start_offset,:end_offset,:sha256)"
                    ),
                    chunk_rows,
                )
                c.execute(
                    text(
                        "INSERT OR IGNORE INTO chunk_metadata(chunk_id,position,tokens,metadata_json) VALUES(:id,:position,:tokens,:meta)"
                    ),
                    metadata_rows,
                )
                c.execute(
                    text(
                        "INSERT INTO knowledge_fts(generation,chunk_id,title,body) VALUES(:g,:id,:title,:body)"
                    ),
                    fts_rows,
                )
                if mode == "HYBRID":
                    c.execute(text("INSERT INTO index_entries(chunk_id,profile_id,generation,status,vector_ref) VALUES(:chunk_id,:profile_id,:generation,:status,:vector_ref)"), entry_rows)
            c.execute(
                text(
                    "INSERT INTO index_heads(mode_key,backend,generation) VALUES(:m,:b,:g) ON CONFLICT(mode_key,backend) DO UPDATE SET generation=:g"
                ),
                {"m": key, "b": self.backend.name, "g": generation},
            )
            if demand is not None:
                c.execute(
                    text("UPDATE index_work_state SET completed_generation=max(completed_generation,:g) WHERE id=1"),
                    {"g": demand},
                )
        if mode == 'HYBRID':
            from ..pipeline_state import record_index_milestones
            record_index_milestones(store, self.profile['id'])
        return {
            "status": "READY",
            "generation": generation,
            "chunks": len(chunks),
            "notes": len(notes),
            "backend": self.backend.name,
            "profile_id": self.profile["id"] if mode == "HYBRID" else None,
            "revision_ids": sorted(valid),
            "compiled_revisions": compiled,
            "reused_compiled_revisions": reused,
            "snapshot_epoch": epoch,
            "activated_epoch": actual,
        }

    def head(self, mode):
        key = self.mode_key(mode)
        with self.store.db.engine.connect() as c:
            row = one(
                c,
                "SELECT g.* FROM index_heads h JOIN index_generations g ON g.id=h.generation WHERE h.mode_key=:m AND h.backend=:b",
                m=key,
                b=self.backend.name,
            )
        if not row and mode == "KEYWORD_ONLY":
            # Hybrid generations already publish the same FTS rows and unit manifest.
            # Keyword search does not depend on an embedding model or vector matrix.
            with self.store.db.engine.connect() as c:
                row = one(
                    c,
                    "SELECT g.* FROM index_heads h JOIN index_generations g ON g.id=h.generation "
                    "WHERE h.backend=:backend AND h.mode_key LIKE 'HYBRID:%' "
                    "AND substr(h.mode_key,-length(:policy))=:policy ORDER BY g.created_at DESC LIMIT 1",
                    backend=self.backend.name,
                    policy=CHUNK_POLICY,
                )
        return dict(row) if row else None

    def snapshot(self, mode):
        row = self.head(mode)
        if not row:
            return None, None, None
        manifest = json.loads(self.store.read(row["manifest_ref"], row["manifest_sha256"]))
        row['fts_rowid_bounds'] = lexical_bounds(
            str((self.store.root / 'noodle.db').resolve()), row['id'], tuple(m['id'] for m in manifest)
        )
        matrix = (
            load_matrix(str(self.store.path(row["matrix_ref"])), row["matrix_sha256"])
            if mode == "HYBRID" and row["matrix_ref"]
            else None
        )
        if matrix is not None and (
            matrix.shape[0] != len(manifest) or matrix.shape[1] != self.profile["dimensions"]
        ):
            raise QualityFailure("Matrix and manifest differ")
        return dict(row), manifest, matrix
