"""Cohort reporting: order creation dates, latest status, calendar-day media spend."""
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import os

from order_analytics import money, net_order_value


def date_range(args):
    zone = ZoneInfo(os.getenv('ANALYTICS_TIMEZONE', 'Asia/Karachi'))
    today = datetime.now(zone).date()
    start = date.fromisoformat(args.get('start') or today.replace(day=1).isoformat())
    end = date.fromisoformat(args.get('end') or today.isoformat())
    if end < start or (end - start).days > 730:
        raise ValueError('Choose an ordered date range of at most 731 days.')
    return start, end, datetime.combine(start, datetime.min.time(), zone), datetime.combine(end + timedelta(days=1), datetime.min.time(), zone)


def ratio(a, b):
    return float(a / b) if a is not None and b else None


def match_order(order, filters):
    attr = order['attribution']
    for key in ('channel', 'campaign_id', 'group_id', 'ad_id'):
        if filters.get(key) and (attr.get(key) or ('Unattributed' if key == 'campaign_id' else '')) != filters[key]:
            return False
    for key in ('normalized_status', 'payment_method', 'courier', 'currency'):
        if filters.get(key) and order.get(key) != filters[key]:
            return False
    return not filters.get('product') or any(i['item_id'] == filters['product'] for i in order['items'])


def order_metrics(orders, product=None):
    result = dict(gross_orders=len(orders), delivered=0, cancelled=0, in_process=0, delivered_revenue=0., cancelled_value=0.)
    for order in orders:
        status = order['normalized_status']
        result[{'Delivered':'delivered', 'Cancelled':'cancelled', 'In process':'in_process'}[status]] += 1
        value = net_order_value(order)
        cancelled_value = money(order['order_value'])
        if product:
            cancelled_value = sum((money(i['value']) for i in order['items'] if i['item_id'] == product), money(0))
            refunds = sum((money(i['value']) for r in order['refund_details'] for i in r['items'] if i['item_id'] == product), money(0))
            value = max(money(0), cancelled_value - refunds)
        if status == 'Delivered':
            result['delivered_revenue'] += float(value)
        if status == 'Cancelled':
            result['cancelled_value'] += float(cancelled_value)
    finals = result['delivered'] + result['cancelled']
    result['delivery_rate'] = ratio(result['delivered'], finals)
    result['cancellation_rate'] = ratio(result['cancelled'], finals)
    return result


def enrich(metrics, ads, ga, spend_available=True, behavior_available=True):
    metrics = dict(metrics)
    for key in ('spend', 'impressions', 'clicks', 'platform_purchases', 'platform_purchase_value'):
        metrics[key] = sum(float(r.get(key) or 0) for r in ads) if spend_available else None
    if spend_available and any(r.get('platform_purchases') is None for r in ads):
        metrics['platform_purchases'] = metrics['platform_purchase_value'] = None
    for key in ('product_views', 'add_to_carts'):
        metrics[key] = sum(float(r.get(key) or 0) for r in ga) if behavior_available else None
    metrics.update(cpc=ratio(metrics['spend'], metrics['clicks']), cpm=ratio(metrics['spend'], (metrics['impressions'] or 0) / 1000),
                   cost_per_delivered=ratio(metrics['spend'], metrics['delivered']),
                   delivered_roas=ratio(metrics['delivered_revenue'], metrics['spend']),
                   view_to_order_rate=ratio(metrics['gross_orders'], metrics['product_views']))
    return metrics


def ga_channel(source_medium):
    source, _, medium = source_medium.partition(' / ')
    if source in ('facebook', 'facebook.com', 'm.facebook.com', 'instagram', 'instagram.com') and medium in ('paid', 'paid_social', 'cpc'):
        return 'meta'
    return 'google' if source == 'google' and medium in ('cpc', 'paid', 'ppc', 'paid_search') else (source or 'Unattributed')


def product_identity(external_id, orders):
    import re
    matches = set()
    composite = re.fullmatch(r'shopify_[A-Z]{2}_(\d+)_(\d+)', external_id or '')
    for order in orders:
        for item in order['items']:
            # GA4's Shopify item ID includes product and viewed variant. Product
            # reporting must roll every variant up to the stable product ID; a
            # customer can view one variant and buy another.
            if external_id in {item['item_id'],item.get('variant_id'),item.get('sku')} or (composite and composite.group(1) == item['item_id']):
                matches.add(item['item_id'])
    return matches.pop() if len(matches)==1 else external_id


def report(orders, snapshots, filters, start, end):
    selected = [o for o in orders if match_order(o, filters)]
    currencies = sorted(set(o['currency'] for o in orders) | {r.get('currency') for s in snapshots if s['report_kind']=='ads' for r in s['rows'] if r.get('currency')})
    currency = filters.get('currency') or (currencies[0] if len(currencies) == 1 else 'PKR')
    selected = [o for o in selected if o['currency'] == currency]
    ads, ga, product_ga, sessions = [], [], [], []
    availability = defaultdict(set)
    refreshed = {}
    for s in snapshots:
        availability[(s['source'], s['report_kind'])].add(str(s['report_date']))
        refreshed[s['source']] = max(refreshed.get(s['source'], ''), str(s['fetched_at']))
        for raw in s['rows']:
            row = dict(raw)
            if s['report_kind'] == 'ads':
                if row.get('currency') == currency and all(not filters.get(k) or row.get(k) == filters[k] for k in ('channel','campaign_id','group_id','ad_id')):
                    ads.append(row)
                continue
            if s['report_kind'] not in ('events', 'products', 'sessions'):
                continue
            row['channel'] = ga_channel(row.get('sessionSourceMedium', ''))
            row['campaign_id'] = row.get('sessionCampaignId', '')
            if not row['campaign_id'].isdigit():
                candidate = row.get('sessionCampaignName', '')
                row['campaign_id'] = candidate if candidate.isdigit() else ''
            if any(filters.get(k) and (row.get(k) or 'Unattributed') != filters[k] for k in ('channel', 'campaign_id')):
                continue
            if s['report_kind'] == 'events':
                row['product_views'] = row['eventCount'] if row['eventName'] == 'view_item' else 0
                row['add_to_carts'] = row['eventCount'] if row['eventName'] == 'add_to_cart' else 0
                ga.append(row)
            elif s['report_kind'] == 'products':
                row['itemId'] = product_identity(row['itemId'],orders)
                row.update(product_views=row['itemsViewed'], add_to_carts=row['itemsAddedToCart'])
                if not filters.get('product') or row['itemId'] == filters['product']:
                    product_ga.append(row)
            else:
                sessions.append(row)
    day_count = (end-start).days+1
    channels = [filters['channel']] if filters.get('channel') in ('google','meta') else ['google','meta']
    spend_ok = all(len(availability[(c, 'ads')]) == day_count for c in channels)
    ga_ok = all(len(availability[('ga4', k)]) == day_count for k in ('events','products','sessions'))
    order_filter = any(filters.get(k) for k in ('normalized_status','payment_method','courier'))
    media_filter_ok = not order_filter and not filters.get('product')
    spend_ok = spend_ok and media_filter_ok
    behavior_ok = ga_ok and not order_filter and not filters.get('group_id') and not filters.get('ad_id')
    top = enrich(order_metrics(selected), ads, product_ga if filters.get('product') else ga, spend_ok, behavior_ok)
    campaign_keys = sorted({(o['attribution'].get('channel', 'Unattributed'), o['attribution'].get('campaign_id') or '') for o in selected} |
                           {(r['channel'],r['campaign_id']) for r in ads + ga})
    campaigns = []
    for channel, campaign in campaign_keys:
        campaign_spend_ok = channel in ('google', 'meta') and len(availability[(channel, 'ads')]) == day_count and media_filter_ok
        group_orders = [o for o in selected if o['attribution'].get('channel', 'Unattributed') == channel and (o['attribution'].get('campaign_id') or '') == campaign]
        group_ads = [a for a in ads if a['channel'] == channel and a['campaign_id'] == campaign]
        group_ga = [g for g in (product_ga if filters.get('product') else ga) if g['channel'] == channel and g['campaign_id'] == campaign]
        name = next((a['campaign_name'] for a in reversed(group_ads)), campaign or 'Unattributed')
        row = dict(channel=channel, campaign_id=campaign, name=name,
                   **enrich(order_metrics(group_orders), group_ads, group_ga, campaign_spend_ok and bool(campaign), behavior_ok), children=[])
        group_ids = sorted({a.get('group_id','') for a in group_ads} | {o['attribution'].get('group_id','') for o in group_orders})
        for gid in group_ids:
            child_ads = [a for a in group_ads if a.get('group_id','') == gid]
            child_orders = [o for o in group_orders if o['attribution'].get('group_id','') == gid]
            child = dict(channel=channel,campaign_id=campaign,group_id=gid,name=next((a['group_name'] for a in child_ads if a.get('group_name')), gid or 'Unattributed group'),
                         **enrich(order_metrics(child_orders),child_ads,[],campaign_spend_ok,False),children=[])
            for aid in sorted({a.get('ad_id','') for a in child_ads} | {o['attribution'].get('ad_id','') for o in child_orders}):
                leaf_ads = [a for a in child_ads if a.get('ad_id','') == aid]
                leaf_orders = [o for o in child_orders if o['attribution'].get('ad_id','') == aid]
                child['children'].append(dict(channel=channel,campaign_id=campaign,group_id=gid,ad_id=aid,
                    name=next((a['ad_name'] for a in leaf_ads if a.get('ad_name')),aid or 'Unattributed ad'),
                    **enrich(order_metrics(leaf_orders),leaf_ads,[],campaign_spend_ok,False)))
            row['children'].append(child)
        campaigns.append(row)
    products = []
    product_ids = sorted({i['item_id'] for o in selected for i in o['items']} | {g['itemId'] for g in product_ga})
    for pid in product_ids:
        if filters.get('product') and filters['product'] != pid:
            continue
        matching = [o for o in selected if any(i['item_id'] == pid for i in o['items'])]
        rows = [g for g in product_ga if g['itemId'] == pid]
        name = next((i['item_name'] for o in matching for i in o['items'] if i['item_id']==pid), next((g['itemName'] for g in rows),pid))
        products.append(dict(product_id=pid,name=name,**enrich(order_metrics(matching,pid),[],rows,False,behavior_ok)))
    options = {key:sorted({str(o.get(key) or '') for o in orders} - {''}) for key in ('payment_method','courier')}
    options.update(channel=sorted({o['attribution'].get('channel','Unattributed') for o in orders} | {'meta','google'}),
                   currency=currencies, product=sorted({(i['item_id'], i['item_name']) for o in orders for i in o['items']}),
                   campaign_id=[('Unattributed','Unattributed')]+sorted({(r['campaign_id'],r['campaign_name']) for s in snapshots if s['report_kind']=='ads' for r in s['rows']}),
                   group_id=sorted({(r['group_id'],r['group_name']) for s in snapshots if s['report_kind']=='ads' for r in s['rows'] if r['group_id']}),
                   ad_id=sorted({(r['ad_id'],r['ad_name']) for s in snapshots if s['report_kind']=='ads' for r in s['rows'] if r['ad_id']}))
    warnings = []
    warnings.append('Cash on delivery: cancelled or finally returned orders contribute zero delivered revenue. Cancelled value is submitted order value, not cash refunded. Their campaign advertising cost remains included. Being Return stays In process until the final return is confirmed.')
    warnings.append('Delivered revenue is based on courier delivery and order adjustments; it does not confirm courier cash remittance. ROAS excludes product costs, courier fees and return charges. GA4 refunds reverse recorded purchases and do not prove a cash refund.')
    if not spend_ok:
        available = [c for c in channels if len(availability[(c, 'ads')]) == day_count]
        missing = [c for c in channels if c not in available]
        detail = (' Available campaign rows still show complete ' + ', '.join(available).title() + ' data; overall spend is withheld because ' + ', '.join(missing).title() + ' is unavailable.') if available and missing and media_filter_ok else ''
        warnings.append('Overall ad spend is unavailable for some channels, dates, or this order/product filter; combined profitability ratios are withheld.' + detail)
    if not behavior_ok: warnings.append('GA4 data unavailable for some dates or this filter; dashes mean unavailable, not zero.')
    warnings.append('Product ad spend is unallocated: campaign spend cannot be assigned to individual products without evidence. Product views aggregate Shopify variants to the product ID. Product revenue excludes shipping and tax.')
    warnings.append('Orders use creation dates and their latest status. Ad spend uses activity dates; recent cohorts are still maturing. Product order counts overlap for multi-product orders.')
    totals_sessions = sum(r.get('sessions',0) for r in sessions)
    engaged = sum(r.get('engagedSessions',0) for r in sessions)
    funnel = {event:sum(r['eventCount'] for r in ga if r['eventName']==event) if behavior_ok and not filters.get('product') else None for event in ('view_item','add_to_cart','begin_checkout','purchase','refund')}
    funnel.update(sessions=totals_sessions if behavior_ok and not filters.get('product') else None,
                  engaged_sessions=engaged if behavior_ok and not filters.get('product') else None,
                  engagement_rate=ratio(engaged,totals_sessions) if behavior_ok and not filters.get('product') else None,
                  purchase_revenue=sum(r.get('purchaseRevenue',0) for r in sessions) if behavior_ok and not filters.get('product') and currency==os.getenv('ANALYTICS_CURRENCY','PKR') else None,
                  submitted_orders=top['gross_orders'])
    warnings.append('Shopify submitted orders are the operational order count. GA4 purchase events are browser-side behavioral signals and may be lower because of consent, blocking, draft/admin orders, or collection timing.')
    return dict(kpis=top,campaigns=campaigns,products=products,orders=[dict(shopify_order_id=o['shopify_order_id'],order_number=o['order_number'],
        created_at=str(o['created_at']),normalized_status=o['normalized_status'],raw_shopify_status=o['raw_shopify_status'],
        raw_courier_status=o['raw_courier_status'],order_value=float(o['order_value']),refunded_value=float(o['refunded_value']),
        currency=o['currency'],courier=o['courier'],payment_method=o['payment_method'],items=o['items'],
        campaign_id=o['attribution'].get('campaign_id',''),group_id=o['attribution'].get('group_id',''),
        ad_id=o['attribution'].get('ad_id',''),channel=o['attribution'].get('channel','Unattributed')) for o in selected],
        options=options,warnings=warnings,currency=currency,refreshed=refreshed,funnel=funnel)
