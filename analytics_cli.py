"""Railway jobs: python analytics_cli.py --help. All output is aggregate only."""
import argparse
import json
import os
from pathlib import Path
from datetime import datetime, timezone


def backfill(start, end, refresh_couriers=False):
    # Reuse the application's Shopify session, pagination and courier integrations.
    os.environ['INITIALIZE_APP'] = 'false'
    # Existing courier helpers also call the ledger hook. Suppress those hooks in
    # this dedicated backfill process so no historical event can escape emit=False.
    os.environ['ORDER_ANALYTICS_ENABLED'] = 'false'
    import main
    from analytics_store import sync_order, sync_tracking
    main.setup_shopify()
    batch = main.shopify.Order.find(status='any', limit=250, created_at_min=start + 'T00:00:00+05:00', created_at_max=end + 'T23:59:59.999999+05:00')
    count = 0
    journey_failures = 0
    while batch:
        for order in batch:
            record = sync_order(order.to_dict(), source='backfill', emit=False)
            if not record['attribution'].get('campaign_id') and not record['attribution'].get('journey_checked'):
                try:
                    from analytics_integrations import sync_shopify_journey
                    sync_shopify_journey(record['shopify_order_id'])
                except Exception:
                    journey_failures += 1
            for number in record['shipments']:
                if number == '__unfulfilled__':
                    continue
                cached = main.get_tracking_summary_cache_only(number)
                if cached:
                    sync_tracking(number, cached, emit=False)
                if refresh_couriers:
                    summary = main.build_tracking_summary_payload(number)
                    if summary and summary.get('tracking_observed'):
                        sync_tracking(number, summary, emit=False)
            count += 1
        if not batch.has_next_page():
            break
        batch = batch.next_page()
    return {'orders_upserted': count, 'historical_events_emitted': 0, 'journey_lookup_failures':journey_failures}


def refresh_active():
    os.environ['INITIALIZE_APP'] = 'false'
    import main
    from analytics_store import transaction
    with transaction() as cur:
        cur.execute("SELECT DISTINCT jsonb_object_keys(shipments) AS number FROM order_analytics WHERE normalized_status='In process'")
        numbers = [r['number'] for r in cur.fetchall() if r['number'] != '__unfulfilled__']
    refreshed = 0
    # Courier requests are already concurrency-limited by the dashboard helper.
    # Commit each bounded batch so a disconnected job can be replayed safely
    # without losing all completed tracking observations.
    for offset in range(0, len(numbers), 40):
        refreshed += main.refresh_tracking_summaries_sync(
            numbers[offset:offset + 40], limit=0, fresh_seconds=0,
            deadline_seconds=90,
            sync_analytics=True,
        )
    return {'shipments_requested': len(numbers), 'shipments_refreshed': refreshed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['migrate','backfill','reports','google-clicks','dispatch','validate-events','release-validated','refresh-couriers','meta-url-audit','meta-check'])
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--source', choices=['ga4','meta','google','all'], default='all')
    parser.add_argument('--refresh-couriers', action='store_true')
    args = parser.parse_args()
    if args.command == 'reports' and not args.start and not args.end:
        from datetime import datetime,timedelta
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo(os.getenv('ANALYTICS_TIMEZONE', 'Asia/Karachi'))).date()
        args.end = today.isoformat()
        args.start = (today-timedelta(days=6)).isoformat()
    if args.command in ('backfill','reports','google-clicks'):
        if not args.start or not args.end:
            parser.error('--start and --end are required')
        from datetime import date
        if date.fromisoformat(args.start) > date.fromisoformat(args.end):
            parser.error('start must precede end')
    if args.command == 'migrate':
        from analytics_store import transaction
        with transaction() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(61872346)')
            for name in ('001_order_analytics.sql','002_analytics_integrity.sql'):
                text = (Path(__file__).parent / 'migrations' / name).read_text()
                cur.execute(text.replace('BEGIN;', '').replace('COMMIT;', ''))
        result = {'migrations_applied': 2}
    elif args.command == 'backfill':
        result = backfill(args.start,args.end,args.refresh_couriers)
    elif args.command == 'reports':
        from analytics_integrations import days, sync_ga, sync_meta, sync_google
        jobs = {'ga4':sync_ga,'meta':sync_meta,'google':sync_google}
        result = {}
        for source in jobs if args.source == 'all' else [args.source]:
            result[source] = {'days':0, 'failed':0}
            for day in days(args.start,args.end):
                try:
                    jobs[source](day)
                    result[source]['days'] += 1
                except Exception:
                    result[source]['failed'] += 1
    elif args.command == 'meta-check':
        from analytics_integrations import meta_check
        result = meta_check()
    elif args.command == 'meta-url-audit':
        from analytics_integrations import meta_url_audit
        result = meta_url_audit()
    elif args.command == 'google-clicks':
        from analytics_integrations import days,sync_google_clicks
        count=0
        for day in days(args.start,args.end):
            sync_google_clicks(day)
            count+=1
        result={'days_processed':count}
    elif args.command == 'refresh-couriers':
        result = refresh_active()
    elif args.command == 'release-validated':
        from analytics_store import transaction
        with transaction() as cur:
            cur.execute("UPDATE analytics_event_outbox SET dispatch_state='pending' WHERE dispatch_state='validated'")
            result = {'released':cur.rowcount}
    else:
        from analytics_store import dispatch_events
        result = dispatch_events(validate_only=args.command=='validate-events')
    print(json.dumps(result, default=str))
    if args.command == 'reports' and any(entry['failed'] for entry in result.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Analytics command failed. Check configured permissions, credentials and migrations; sensitive exception details suppressed.')
