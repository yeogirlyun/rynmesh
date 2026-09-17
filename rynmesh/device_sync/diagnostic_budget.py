"""Optional refusal diagnostics must not consume the pairing protocol's file budget."""
from ..crypto import canonical_json

MAX_REJECTION_BYTES = 2 * 1024 * 1024
PAIRING_HEADROOM = 64 * 1024


def bound_rejections(data, *, max_plaintext):
    sections = []
    for pair in data['pairs'].values():
        transfer = pair.get('transfer', {})
        if transfer.get('rejected'):
            sections.append((transfer, transfer.pop('rejected')))
    if not sections:
        return
    # Compute available space without copying private pairing/key material. Always
    # restore the sections, including when canonical serialization fails.
    try:
        available = min(MAX_REJECTION_BYTES, max(0, max_plaintext - len(canonical_json(data)) - PAIRING_HEADROOM))
    finally:
        for transfer, section in sections:
            transfer['rejected'] = section
    if sum(len(canonical_json(section)) for _, section in sections) <= available:
        return
    entries = [(transfer, scope, entry) for transfer, section in sections for scope, entry in section.items()]
    quota = max(0, available // max(1, len(entries)) - 256)
    for transfer, scope, entry in entries:
        if quota == 0:
            transfer['rejected'].pop(scope)
            transfer['rejected_truncated'] = True
            continue
        used, rows, overflow = 0, {}, []
        for identifier, code in sorted(entry.get('rows', {}).items()):
            cost = len(canonical_json({identifier: code}))
            if used + cost > quota:
                break
            rows[identifier] = code
            used += cost
        for identifier in sorted(entry.get('overflow', [])):
            cost = len(canonical_json(identifier)) + 1
            if used + cost > quota:
                break
            overflow.append(identifier)
            used += cost
        truncated = (bool(entry.get('overflow_truncated')) or len(rows) < len(entry.get('rows', {}))
                     or len(overflow) < len(entry.get('overflow', [])))
        transfer['rejected'][scope] = {'rows': rows, 'count': len(rows) + len(overflow),
                                      **({'overflow': overflow} if overflow else {}),
                                      **({'overflow_truncated': True} if truncated else {})}
    for transfer, _ in sections:
        if not transfer['rejected']:
            transfer.pop('rejected')
