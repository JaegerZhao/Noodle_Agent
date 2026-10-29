"""Packet-local routing keys distinguish domains without changing stored identities."""

from ..contracts import canonical, sha


def reference_keys(catalog, declarations=()):
    values = [(f'C{i}', c['domain'], c.get('definition', {}).get('identity_key', c['id']))
              for i, c in enumerate(catalog)]
    values += [(e.ref, e.domain, e.identity_key) for e in declarations]
    domains = {}
    for _, domain, key in values:
        domains.setdefault(key, set()).add(domain)
    return {ref: 'object_' + sha(canonical([domain, key]))[:24] if len(domains[key]) > 1 else key
            for ref, domain, key in values}
