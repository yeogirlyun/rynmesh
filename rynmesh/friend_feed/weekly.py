"""Private, deterministic week-to-date projection; never a public digest input."""
from datetime import UTC, datetime, timedelta

MAX_WEEKLY = 100


def weekly_recap(timeline, *, now):
    current = datetime.fromtimestamp(now, UTC)
    start = (current - timedelta(days=current.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    start_unix = start.timestamp()
    unique = {}
    incomplete = False
    for feed in timeline:
        incomplete |= bool(feed['next_cursor'] or feed['error_code'] or feed['checked_at'] is None)
        for entry in feed['rows']:
            if not start_unix <= entry['published_at'] <= now:
                continue
            key = (feed['peer_id'], entry['id'])
            prior = unique.get(key)
            if prior and prior['revision'] >= entry['revision']:
                continue
            # Only verified timeline metadata. No body fetch, source URL, prompt,
            # private library path or model-generated summary enters this view.
            unique[key] = {'relationship_id': feed['relationship_id'], 'peer_id': feed['peer_id'],
                           'node_name': feed['node_name'], 'publication_id': entry['id'],
                           'revision': entry['revision'], 'published_at': entry['published_at'],
                           'title': entry['card']['title'], 'checked_at': feed['checked_at'],
                           'access_unconfirmed': bool(feed['error_code'])}
    rows = sorted(unique.values(), key=lambda row: (-row['published_at'], row['peer_id'], row['publication_id']))
    return {'timezone': 'UTC', 'week_start': start.isoformat(), 'as_of': current.isoformat(),
            'items': rows[:MAX_WEEKLY], 'available_count': len(rows),
            'incomplete': incomplete or len(rows) > MAX_WEEKLY}
