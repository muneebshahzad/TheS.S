"""Read-only reporting integrations. Credentials never appear in exception messages."""
import os
from datetime import date, timedelta
import requests
from analytics_store import purchase_evidence, save_report

META_UTM = 'utm_source=facebook&utm_medium=paid_social&utm_campaign={{campaign.id}}&utm_term={{adset.id}}&utm_content={{ad.id}}'


def sync_shopify_journey(order_id):
    from pathlib import Path
    from shopify_protected_data import get_graphql_endpoint, get_graphql_token
    from analytics_store import save_journey
    token = get_graphql_token()
    if not token:
        raise RuntimeError('Shopify GraphQL access is not configured')
    query = (Path(__file__).parent / 'queries' / 'order_attribution.graphql').read_text()
    result = request_json('POST',get_graphql_endpoint(),headers={'X-Shopify-Access-Token':token},
                          json={'query':query,'variables':{'id':'gid://shopify/Order/'+str(order_id)}})
    if result.get('errors'):
        raise RuntimeError('Shopify journey query failed; check order permissions')
    journey = ((result.get('data') or {}).get('order') or {}).get('customerJourneySummary')
    # An empty summary is still an authoritative lookup result. Persisting the
    # checked marker prevents every idempotent backfill from querying Shopify
    # again for orders that have no customer-journey data.
    save_journey(order_id, journey or {})


def request_json(method, url, **kwargs):
    for attempt in range(3):
        try:
            response = requests.request(method, url, timeout=60, **kwargs)
        except requests.RequestException:
            raise RuntimeError('Reporting connection failed') from None
        if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            import time
            time.sleep(min(2 ** attempt, 4))
            continue
        if not response.ok:
            raise RuntimeError('Reporting API returned HTTP ' + str(response.status_code))
        return response.json()


def google_token(scope):
    import google.auth
    from google.auth.transport.requests import Request
    credentials = google_reporting_credentials([scope])
    if credentials is None:
        credentials, _ = google.auth.default(scopes=[scope])
    credentials.refresh(Request())
    return credentials.token


def google_reporting_credentials(scopes):
    raw = os.getenv('GOOGLE_REPORTING_SERVICE_ACCOUNT_JSON')
    if not raw:
        return None
    import json
    from google.oauth2.service_account import Credentials
    try:
        info = json.loads(raw)
        if info.get('type') != 'service_account' or info.get('token_uri') != 'https://oauth2.googleapis.com/token':
            raise ValueError('Invalid service account')
        return Credentials.from_service_account_info(info, scopes=scopes)
    except Exception:
        raise RuntimeError('Google reporting service-account configuration is invalid') from None


def ga_report(day, dimensions, metrics, end=None, dimension_filter=None):
    token = google_token('https://www.googleapis.com/auth/analytics.readonly')
    url = 'https://analyticsdata.googleapis.com/v1beta/properties/' + os.getenv('GA4_PROPERTY_ID', '491963638') + ':runReport'
    rows, offset = [], 0
    while True:
        payload = dict(dateRanges=[dict(startDate=day, endDate=end or day)],
                       dimensions=[dict(name=d) for d in dimensions], metrics=[dict(name=m) for m in metrics],
                       limit=10000, offset=offset, keepEmptyRows=True, currencyCode=os.getenv('ANALYTICS_CURRENCY','PKR'))
        if dimension_filter:
            payload['dimensionFilter'] = dimension_filter
        data = request_json('POST', url, headers={'Authorization': 'Bearer ' + token}, json=payload)
        metadata = data.get('metadata', {})
        if metadata.get('subjectToThresholding') or metadata.get('dataLossFromOtherRow') or metadata.get('samplingMetadatas'):
            raise RuntimeError('GA4 report is thresholded, sampled or aggregated into other; not saved as complete')
        for row in data.get('rows', []):
            rows.append(dict(zip(dimensions + metrics, [v['value'] for v in row.get('dimensionValues', [])] + [float(v['value']) for v in row['metricValues']])))
        offset += len(data.get('rows', []))
        if offset >= data.get('rowCount', 0):
            break
    return rows


def unique_users(start, end, filters):
    """Users are non-additive: query the selected range, never sum daily distincts."""
    if any(filters.get(k) for k in ('product','normalized_status','payment_method','courier','group_id','ad_id')):
        return None
    def exact(field, value):
        return {'filter':{'fieldName':field,'stringFilter':{'matchType':'EXACT','value':value}}}
    expressions = []
    channel = filters.get('channel')
    import re
    if channel and not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',channel):
        return None
    if filters.get('campaign_id') and not filters['campaign_id'].isdigit():
        return None
    if channel == 'meta':
        expressions.append({'orGroup':{'expressions':[exact('sessionSourceMedium',s+' / '+m) for s in ('facebook','facebook.com','m.facebook.com','instagram','instagram.com') for m in ('paid','paid_social','cpc')]}})
    elif channel == 'google':
        expressions.append({'orGroup':{'expressions':[exact('sessionSourceMedium','google / '+m) for m in ('cpc','ppc','paid','paid_search')]}})
    elif channel:
        expressions.append(exact('sessionSource',channel))
    if filters.get('campaign_id'):
        cid = filters['campaign_id']
        expressions.append({'orGroup':{'expressions':[exact('sessionCampaignId',cid),exact('sessionCampaignName',cid)]}})
    result = ga_report(str(start),[],['totalUsers'],end=str(end),dimension_filter={'andGroup':{'expressions':expressions}} if expressions else None)
    return result[0]['totalUsers'] if result else 0


def sync_ga(day):
    # Separate event, item, and session scopes to avoid invalid GA4 combinations or duplicated totals.
    campaign = ['sessionSourceMedium', 'sessionCampaignId', 'sessionCampaignName']
    events = ga_report(day, campaign + ['eventName'], ['eventCount'])
    sessions = ga_report(day, campaign, ['totalUsers', 'sessions', 'engagedSessions', 'purchaseRevenue'])
    products = ga_report(day, campaign + ['itemId', 'itemName'], ['itemsViewed', 'itemsAddedToCart', 'itemsPurchased', 'itemRevenue'])
    purchases = ga_report(day, ['transactionId'], ['ecommercePurchases'])
    for kind, rows in [('events', events), ('sessions', sessions), ('products', products), ('purchases', purchases)]:
        save_report('ga4', day, kind, rows)
    purchase_evidence(r['transactionId'] for r in purchases if r['ecommercePurchases'] > 0 and r['transactionId'] != '(not set)')


def meta_account_id():
    import re
    account = os.getenv('META_AD_ACCOUNT_ID', '356232020087034')
    if not re.fullmatch(r'[0-9]+', account):
        raise RuntimeError('Meta ad account ID must contain digits only')
    return account


def meta_get(path, params):
    import re
    version = os.getenv('META_API_VERSION', '')
    token = os.getenv('META_ACCESS_TOKEN', '')
    if not re.fullmatch(r'v[0-9]+\.0', version) or not token:
        raise RuntimeError('Meta reporting credentials or API version are not configured')
    account_path = 'act_' + meta_account_id()
    if path not in (account_path, account_path + '/insights', account_path + '/ads'):
        raise RuntimeError('Meta reporting endpoint is outside the configured ad account')
    result = request_json('GET', 'https://graph.facebook.com/' + version + '/' + path,
                          headers={'Authorization': 'Bearer ' + token}, params=params,
                          allow_redirects=False)
    if not isinstance(result, dict) or result.get('error'):
        raise RuntimeError('Meta reporting API returned an invalid response')
    return result


def meta_check():
    account = meta_account_id()
    data = meta_get('act_' + account, {'fields': 'id,account_id,name,currency,timezone_name,account_status'})
    if str(data.get('account_id')) != account or data.get('id') != 'act_' + account:
        raise RuntimeError('Meta reporting account identity did not match configuration')
    return dict(account_id=account, name=data.get('name'), currency=data.get('currency'),
                timezone=data.get('timezone_name'), account_status=data.get('account_status'),
                currency_matches=data.get('currency') == os.getenv('ANALYTICS_CURRENCY', 'PKR'),
                timezone_matches=data.get('timezone_name') == os.getenv('ANALYTICS_TIMEZONE', 'Asia/Karachi'))


def meta_pages(path, params):
    params = dict(params)
    rows, seen_cursors = [], set()
    while True:
        result = meta_get(path, params)
        if not isinstance(result.get('data'), list):
            raise RuntimeError('Meta reporting API returned incomplete data')
        rows.extend(result['data'])
        if not result.get('paging', {}).get('next'):
            break
        # Use the cursor on the same known API endpoint, not a returned token-bearing URL.
        cursor = result['paging'].get('cursors', {}).get('after')
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise RuntimeError('Meta reporting pagination did not advance')
        seen_cursors.add(cursor)
        params['after'] = cursor
    return rows


def sync_meta(day):
    import json
    day = date.fromisoformat(day).isoformat()
    fields = 'campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,impressions,reach,clicks,inline_link_clicks,spend,cpc,cpm,actions,action_values,account_currency'
    data = meta_pages('act_' + meta_account_id() + '/insights',
                      dict(fields=fields, level='ad', time_range=json.dumps(dict(since=day, until=day)), time_increment=1, limit=500))
    rows, seen_ads = [], set()
    for r in data:
        if r['ad_id'] in seen_ads:
            raise RuntimeError('Meta daily report contains duplicate ad rows')
        seen_ads.add(r['ad_id'])
        if r.get('account_currency') != os.getenv('ANALYTICS_CURRENCY', 'PKR'):
            raise RuntimeError('Meta reporting currency does not match dashboard currency')
        def action(field):
            values = {v['action_type']: float(v['value']) for v in r.get(field, [])}
            return values.get('offsite_conversion.fb_pixel_purchase', values.get('purchase', 0))
        rows.append(dict(channel='meta', campaign_id=r['campaign_id'], campaign_name=r['campaign_name'],
                         group_id=r['adset_id'], group_name=r['adset_name'], ad_id=r['ad_id'], ad_name=r['ad_name'],
                         impressions=int(r.get('impressions', 0)), reach=int(r.get('reach', 0)), clicks=int(r.get('clicks', 0)),
                         link_clicks=int(r.get('inline_link_clicks', 0)), spend=float(r.get('spend', 0)),
                         cpc=float(r['cpc']) if r.get('cpc') is not None else None,
                         cpm=float(r['cpm']) if r.get('cpm') is not None else None,
                         platform_purchases=action('actions'), platform_purchase_value=action('action_values'),
                         currency=r.get('account_currency', '')))
    save_report('meta', day, 'ads', rows)


def meta_url_audit():
    """Exact ad inventory/proposal only. No writes to campaigns, creatives, URLs or bidding."""
    ads = meta_pages('act_' + meta_account_id() + '/ads',
                     dict(fields='id,name,effective_status,campaign_id,adset_id,creative{id,url_tags}', limit=100))
    return [dict(ad_id=a['id'], ad_name=a['name'], status=a['effective_status'], campaign_id=a.get('campaign_id'),
                 group_id=a.get('adset_id'), creative_id=a.get('creative', {}).get('id'),
                 existing_url_tags=a.get('creative', {}).get('url_tags', ''), proposed_url_tags=META_UTM,
                 requires_review=True) for a in ads if a.get('creative', {}).get('url_tags', '') != META_UTM]


def google_ads_query(query):
    from google.ads.googleads.client import GoogleAdsClient
    from google.ads.googleads import config
    try:
        account = os.environ['GOOGLE_ADS_CUSTOMER_ID'].replace('-', '')
        if len(account) != 10 or not account.isascii() or not account.isdigit():
            raise ValueError('Invalid customer ID')
        credentials = google_reporting_credentials(['https://www.googleapis.com/auth/adwords'])
        if credentials is not None:
            manager = os.getenv('GOOGLE_ADS_LOGIN_CUSTOMER_ID', '').replace('-', '') or None
            if manager and (len(manager) != 10 or not manager.isascii() or not manager.isdigit()):
                raise ValueError('Invalid manager ID')
            client = GoogleAdsClient(credentials=credentials, use_proto_plus=True, login_customer_id=manager)
        else:
            settings = config.load_from_env()
            settings.pop('developer_token', None)
            client = GoogleAdsClient.load_from_dict(settings)
        return list(client.get_service('GoogleAdsService').search(customer_id=account, query=query))
    except Exception:
        raise RuntimeError('Google Ads query failed; check account access and API configuration') from None


def sync_google(day):
    # Purchases are reported separately with conversion category filtering. All conversions
    # includes leads and other goals and must never be relabelled as purchases.
    dimension = 'campaign.id,campaign.name,ad_group.id,ad_group.name,ad_group_ad.ad.id,ad_group_ad.ad.name'
    rows = google_ads_query(f"SELECT {dimension},customer.currency_code,metrics.impressions,metrics.clicks,metrics.cost_micros FROM ad_group_ad WHERE segments.date = '{day}'")
    conversions = google_ads_query(f"SELECT {dimension},metrics.all_conversions,metrics.all_conversions_value FROM ad_group_ad WHERE segments.date = '{day}' AND segments.conversion_action_category = 'PURCHASE'")
    totals = {}
    for r in conversions:
        key = (str(r.campaign.id), str(r.ad_group.id), str(r.ad_group_ad.ad.id))
        p, v = totals.get(key, (0, 0))
        totals[key] = (p + r.metrics.all_conversions, v + r.metrics.all_conversions_value)
    result = []
    for r in rows:
        key = (str(r.campaign.id), str(r.ad_group.id), str(r.ad_group_ad.ad.id))
        p, v = totals.get(key, (0, 0))
        result.append(dict(channel='google', campaign_id=key[0], campaign_name=r.campaign.name, group_id=key[1],
                           group_name=r.ad_group.name, ad_id=key[2], ad_name=r.ad_group_ad.ad.name,
                           impressions=r.metrics.impressions, clicks=r.metrics.clicks, spend=r.metrics.cost_micros / 1000000,
                           reach=None, link_clicks=None, platform_purchases=p, platform_purchase_value=v,
                           currency=r.customer.currency_code))
    # PMax and other campaign types may not expose ad_group_ad. Preserve their spend
    # as a campaign-only residual so account spend still reconciles.
    campaigns = google_ads_query(f"SELECT campaign.id,campaign.name,customer.currency_code,metrics.impressions,metrics.clicks,metrics.cost_micros FROM campaign WHERE segments.date = '{day}'")
    for r in campaigns:
        children = [v for v in result if v['campaign_id'] == str(r.campaign.id)]
        residual = r.metrics.cost_micros / 1000000 - sum(v['spend'] for v in children)
        if not children or residual > .01:
            result.append(dict(channel='google', campaign_id=str(r.campaign.id), campaign_name=r.campaign.name,
                group_id='', group_name='', ad_id='', ad_name='', impressions=max(0,r.metrics.impressions-sum(v['impressions'] for v in children)),
                clicks=max(0,r.metrics.clicks-sum(v['clicks'] for v in children)), spend=max(0,residual), reach=None, link_clicks=None,
                platform_purchases=None, platform_purchase_value=None, currency=r.customer.currency_code))
    save_report('google', day, 'ads', result)


def sync_google_clicks(day):
    from analytics_store import resolve_google_clicks
    if date.fromisoformat(day) < date.today() - timedelta(days=89):
        return
    rows = google_ads_query(f"SELECT click_view.gclid,campaign.id,ad_group.id FROM click_view WHERE segments.date = '{day}'")
    resolve_google_clicks([dict(gclid=r.click_view.gclid,campaign_id=str(r.campaign.id),group_id=str(r.ad_group.id)) for r in rows])


def days(start, end):
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    while start <= end:
        yield start.isoformat()
        start += timedelta(days=1)
