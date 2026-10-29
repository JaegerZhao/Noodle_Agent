"""Model-only evidence projections; immutable full fragments remain in the store."""

import copy

from .contracts import canonical, sha
from .segmentation import tokens


class EvidencePacket:
    def __init__(self, specs):
        self.forward = {}
        for spec in specs:
            for fragment in spec['fragments']:
                key = fragment['fragment_id']
                self.forward.setdefault(key, 'F' + str(len(self.forward)))
        self.reverse = {v: k for k, v in self.forward.items()}
        self.input_hash = sha(canonical(specs))

    def remap(self, value, reverse=False):
        mapping = self.reverse if reverse else self.forward
        if isinstance(value, dict):
            return {k: self.remap(v, reverse) for k, v in value.items()}
        if isinstance(value, list):
            return [self.remap(v, reverse) for v in value]
        return mapping.get(value, value) if isinstance(value, str) else value

    def project(self, specs):
        values = copy.deepcopy(specs)
        for spec in values:
            spec['fragments'] = [{'fragment_id': f['fragment_id'], 'text': f['text'],
                'title': f['title'], 'section_title': f['section_title']} for f in spec['fragments']]
            for image in spec.get('images', {}).values():
                image.pop('evidence_ids', None)
        return self.remap(values)

    def restore_review(self, value):
        for key in value.chapters:
            chapter = value.chapters[key]
            chapter.supported_fragment_ids = [self.reverse.get(v, v) for v in chapter.supported_fragment_ids]
            for finding in [*chapter.findings, *chapter.data_checks]:
                finding.source_ids = [self.reverse.get(v, v) for v in finding.source_ids]

    def receipt(self, original):
        return {'policy': 'evidence-packet-v1', 'input_sha256': self.input_hash, 'references': self.reverse,
            'original_estimated_tokens': tokens(canonical(original)), 'projected_estimated_tokens': tokens(canonical(self.project(original)))}
