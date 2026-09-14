# Order and marketing analytics handoff

Implementation is in the existing Flask application, behind `ORDER_ANALYTICS_ENABLED=true`. The authenticated pages are `/analytics` and `/marketing-performance`; `/api/analytics` uses the existing admin session. Production has not been deployed or backfilled by this task. Live reporting and GA4 validation require the credentials below.

## COD and current order states

The ledger has exactly three current statuses. Shopify cancellation takes precedence; otherwise courier delivery means Delivered, final return/cancellation means Cancelled, and active states mean In process. `Being Return` stays In process. Multi-shipment orders remain In process until every shipment has a compatible final outcome; mixed delivered/returned shipments are not counted as fully delivered. Raw statuses remain available for investigation. Manual workflow labels such as Packed or Manufactured do not prove courier delivery.

Cancelled COD orders contribute no delivered revenue. Their submitted value is not a cash refund, and their campaign spend remains in campaign profitability. Delivery does not prove that the courier has remitted cash to the merchant. ROAS is revenue/ad spend, not net profit: inventory costs, courier charges and return fees are not supplied by these integrations.

Gross orders include non-test submitted orders. Delivery and cancellation rates exclude In process. Order dates are creation dates in `ANALYTICS_TIMEZONE`, with an inclusive end date; statuses are the latest known states. Media spend uses activity dates. This is cohort reporting, not delivery-date reporting. Reconcile Shopify using the same creation range, timezone, currency and test-order exclusions.

Delivered revenue uses the smaller of Shopify current total (when available) and original total minus refund adjustments, bounded at zero. This avoids subtracting a refund twice when Shopify already adjusted its current total. Successful refund transactions determine monetary adjustments; pending/failed transactions are not counted. COD item returns without monetary transactions may still carry Shopify item adjustments. Product revenue excludes tax/shipping and subtracts explicit item refunds; custom order-level adjustments cannot be assigned to products without evidence.

## Storage and synchronization

`order_analytics` is keyed by Shopify order ID. Per-order transaction locks serialize Shopify and courier updates, including first insertion. `order_status_transitions` preserves changes. `webhook_receipts` rejects duplicate delivery IDs. Shopify timestamps reject stale payloads; courier observations are independently ordered. Order synchronization runs before the existing active-order cache removes closed/cancelled orders.

Existing Shopify sessions, order pagination, tracking fetches and courier caches are reused. Added webhook subscriptions: `orders/cancelled`, `fulfillments/create`, `fulfillments/update`, `refunds/create`; existing create/update handlers also synchronize the ledger. Related webhooks fetch the full current order. Invalid signatures fail, synchronization failures return retryable errors, and empty/failed courier responses do not overwrite a valid status.

`analytics_reports` stores one daily snapshot per source/report kind, retaining names as reported on each date. The pre-existing 001 migration also defines specialized snapshot tables; this implementation uses the generic table from 002. Missing/partial coverage shows unavailable values instead of false zeroes. GA4 distinct users are queried for the selected whole range rather than summed across days. Meta reach is stored at daily ad level and is not incorrectly summed into unique campaign reach.

Attribution uses saved order attributes first, then Shopify landing/referrer and customer-journey evidence. Numeric stable IDs and exact GCLID matches join orders to ads. Names do not establish a match. Unavailable attribution stays Unattributed. Product GA4 identifiers match exact product IDs, variants, SKUs, or verified Shopify composite IDs; ambiguous identifiers are not guessed.

## Credentials and configuration

Use Railway variables or mounted secrets; never commit credentials or paste them into logs. `.env.example` lists the exact variable names.

| Service | Required access/configuration |
| --- | --- |
| Existing Railway app | Existing `DATABASE_URL`, `APP_SECRET_KEY`, `ADMIN_PORTAL_PASSWORD`, Shopify session configuration and courier credentials. Keep production startup initialization enabled. |
| Feature | `ORDER_ANALYTICS_ENABLED=false` until migrations finish; `ANALYTICS_TIMEZONE=Asia/Karachi`, `ANALYTICS_CURRENCY=PKR`. Verify platform reporting timezones agree before comparing daily totals. |
| Shopify `psgv0a-qk` | Existing order read access and webhook registration; `read_all_orders` approval for history older than 60 days, in addition to `read_orders`. `SHOPIFY_WEBHOOK_SECRET` must match the webhook signing application. Theme edit access is needed to install the snippet. Customer-journey availability depends on Shopify's data and app permissions. |
| GA4 `491963638` | Analytics Data API is enabled and the service account has Viewer access. Live reporting validation succeeded. `GA4_MEASUREMENT_ID` (`G-...`) and `GA4_API_SECRET` are still required for server-side final-state events; the property ID is not the measurement ID. |
| Google Ads | Google Ads API is enabled and the service account has **Read-only** access to customer `3776479482`, but Cloud project `670780824153` currently has Test access. Apply for Explorer or higher access before live production-account reporting will work. Optional manager `GOOGLE_ADS_LOGIN_CUSTOMER_ID`; `GOOGLE_ADS_USE_PROTO_PLUS=true`. The GA4-linked customer `7576092690` is cancelled and is not substituted. |
| Meta | A 60-day `ads_read` system-user token for **Sleek analytics** is installed in Railway for account `356232020087034` in business `838669770883289`. `META_AD_ACCOUNT_ID`, `META_BUSINESS_ID`, and tested `META_API_VERSION=v26.0` are configured. Rotate the token by November 13, 2026. Reporting does not require changing campaigns. |

Railway inspection identified project **Sleek Space Dashboard**, service **TheS.S**, production, with Postgres. Project ID: `26286953-673e-409d-98cf-05f5fcbd6926`; service ID: `2573a7e3-8fd6-4b53-8326-7317e05467f6`. Its deployed commit matched repository commit `e622099` during the original inspection. Google and Meta reporting variables are now present as masked production variables; their values are not stored in source or documentation.

### Google access setup follow-up — September 13, 2026

Google [sunset developer tokens on September 9, 2026](https://developers.google.com/google-ads/api/docs/api-policy/developer-token). The integration now uses `google-ads>=32,<33`, explicitly removes legacy developer tokens from client configuration, and sanitizes configuration failures as well as API failures. Cloud project production approval is still required; upgrading the SDK does not grant access.

The signed-in Cloud project `gen-lang-client-0502093918` (Default Gemini Project) has no OAuth clients. Its existing Gemini-only API key/service account was not reused. A dedicated `sleek-space-analytics@gen-lang-client-0502093918.iam.gserviceaccount.com` identity was created with no project-wide IAM role or domain-wide delegation, granted Google Ads Read-only and GA4 Viewer access, and installed in Railway through `GOOGLE_REPORTING_SERVICE_ACCOUNT_JSON`. Unused OAuth variables remain omitted.

Verification after the SDK update: **48 tests passed**, including four new offline Google authentication/configuration tests. The old temporary PGlite process stopped responding; the successful complete rerun used a fresh isolated in-memory database on localhost port 55433. This is not a live Google API validation. Changes remain local, uncommitted and undeployed.

### Approved credential setup — September 14, 2026

After explicit user approval, created `sleek-space-analytics@gen-lang-client-0502093918.iam.gserviceaccount.com` with no project-wide IAM roles or delegated users. Verified saved Google Ads **Read only** access on `377-647-9482` and GA4 **Viewer** access on property `491963638`. The Ads account picker shows the older `757-609-2690` account as cancelled. No account linkage, billing, campaign, bidding or conversion-goal changes were made.

Google's first key-creation page returned an unknown error and the inventory still showed no keys. Reopening Manage keys from the service-account inventory (without the malformed `;edit=true/keys` route) succeeded. A single JSON key was downloaded and local file permissions restricted to mode 0600. Its contents were sent through stdin to Railway's `GOOGLE_REPORTING_SERVICE_ACCOUNT_JSON` variable on the verified production **TheS.S** service, with `--skip-deploys`. Customer/property ID variables were also set. The integration now reads this secret directly in memory for both clients; it takes precedence over file/OAuth configuration and restricts the token endpoint to Google's official endpoint. Do not paste the JSON into a command-line argument or commit it.

Google Ads API and Analytics Data API were enabled on September 14. GA4 live reporting validation succeeded and returned 908 sessions for September 13, 2026. Google Ads authentication reaches the API, but the project is approved only for test accounts; Explorer, Basic or Standard access is required for live customer `3776479482`. The Google key is for reporting, not Measurement Protocol event submission; `GA4_MEASUREMENT_ID` and `GA4_API_SECRET` remain outstanding.

Latest verification: **50 tests passed** against an isolated in-memory test database, including secret-JSON scope and token-endpoint tests. Credential settings were saved without deployment; source changes are still local and uncommitted.

### Meta reporting connection — September 14, 2026

Target is **AOD `838669770883289`**, ad account **`356232020087034`**. The published app **Ad Account Stats `1309227864488870`** is owned by **Alk'22 `658649282109445`**. AOD's request for access to that exact app was automatically approved because the signed-in user is an app administrator. No app transfer or replacement app was used. The app's quick-token UI was not used, and Alk'22's existing Conversions API system user was left unchanged.

AOD now has Employee system user **Sleek analytics**, ID `61594004265779`, assigned only **View performance** on AOD/2. Campaign management, Creative Hub and full-control permissions remain off. A reload verified one assigned asset: **AOD/2 Partial access (View performance)**. A verified 60-day token was generated through the AOD system-user flow with only `ads_read` and saved as a masked Railway variable. The token value is not stored in source or documentation.

The Meta reader limits requests to the configured account's metadata, insights and ads endpoints, disables redirects, validates API version/account configuration, rejects malformed responses and repeated pagination cursors, and never follows token-bearing pagination URLs. Daily snapshots reject duplicate ads and currency mismatches and preserve CPC/CPM. Ten tests cover these boundaries. Live application-level verification succeeded against Graph/Marketing API `v26.0`: identity matched AOD/2, account status was active, currency was PKR and timezone was Asia/Karachi. A 30-day read-only insights query returned 180 daily-ad rows across 9 campaigns, 14 ad sets and 18 ads.

The read-only URL audit found 109 ads whose current tags do not exactly equal the requested ID-based convention: 6 Active, 56 Paused, 25 Ad-set paused, 20 Campaign paused and 2 With issues. To minimize disruption, any future live proposal should initially cover only these six Active ads:

| Ad ID | Ad name | Campaign ID | Ad-set ID |
| --- | --- | --- | --- |
| `120253586185010635` | Beanbag – Carousal Only | `120253541691060635` | `120253541691080635` |
| `120253541691070635` | Beanbag | `120253541691060635` | `120253541691080635` |
| `120253297135660635` | Muses Lamp | `120253297135650635` | `120253297135670635` |
| `120253479075290635` | Leopard Table | `120253479075280635` | `120253479075300635` |
| `120253402634110635` | Chess | `120253402634090635` | `120253402634100635` |
| `120253403029870635` | Deer | `120253403029860635` | `120253403029880635` |

All six currently have empty URL tags. No live ad, bidding, conversion or creative setting was changed. The 103 inactive/problem ads should stay unchanged unless deliberately reactivated and reviewed. Latest complete local suite: **60 tests passed** using a disposable in-memory PostgreSQL-compatible database.

## Deployment and migration

1. Back up production Postgres. Install `requirements.txt` using the existing Python/Railway build.
2. Deploy code with `ORDER_ANALYTICS_ENABLED=false`. Run `python analytics_cli.py migrate` in the same Railway environment. It transactionally applies 001 and 002 under a migration lock and can be rerun. The 001 migration files were already present locally and were preserved.
3. Supply credentials, then enable `ORDER_ANALYTICS_ENABLED=true` and restart the existing app. Startup registers the required Shopify webhook topics. Check registration success and Shopify webhook delivery logs.
4. Run `python analytics_cli.py backfill --start YYYY-MM-DD --end YYYY-MM-DD --refresh-couriers` for the required history. Dates are inclusive. Use manageable ranges and inspect `journey_lookup_failures`. Backfill never emits historical GA4 events, including through reused courier hooks.
5. Run `python analytics_cli.py reports --start YYYY-MM-DD --end YYYY-MM-DD --source all`. Source can be `ga4`, `meta` or `google`; failures produce aggregate counts and nonzero exit status. Then run `python analytics_cli.py google-clicks --start YYYY-MM-DD --end YYYY-MM-DD` for available recent click data (last 89 days).
6. Configure Railway jobs using the same environment: `reports` nightly (defaults to refreshing the last seven days), `google-clicks` for recent dates, `refresh-couriers` every 15–30 minutes, and `dispatch` every few minutes after event validation. Choose schedules consistent with API quotas. The CLI exists; these production jobs have not been created.
7. Open `/analytics` in an authenticated admin session and complete the live acceptance checks below before treating it as reconciled reporting.

Operational rollback: set `ORDER_ANALYTICS_ENABLED=false`, stop analytics jobs and restore the previous application deployment. Keep the ledger/outbox tables to preserve audit and duplicate-prevention history. The analytics-only webhook URLs can remain disabled during a short rollback; remove their exact subscriptions only for a permanent rollback, preserving existing order webhooks.

Destructive schema rollback is optional and requires a backup and stopped writers: apply `migrations/002_analytics_integrity.rollback.sql`, then `migrations/001_order_analytics.rollback.sql` in that order. These remove analytics records, transitions, receipts, snapshots and queued/sent-event history. Restoring only code does not require dropping tables. Never re-enable dispatch after dropping event history without restoring/reconciling it, since previously sent events could be repeated.

## Storefront attribution installation

Copy `storefront/snippets/ss-attribution.liquid` into the active theme and render it once in `layout/theme.liquid` before `</body>`:

```liquid
{% render 'ss-attribution', measurement_id: 'G-YOUR-WEB-STREAM-ID' %}
```

The snippet gates collection on Shopify analytics consent, preserves first touch plus the latest non-direct touch for 90 days, captures actual GA client/session IDs when available, and writes `ss_*` and `ss_first_*` cart attributes that become order note attributes. Revocation clears saved attribution and records denied consent. Standard checkout waits for the attribute update with a form-attribute fallback. The snippet never initializes another GA tag or sends customer data.

Live theme installation has not been performed. Test consent granted/denied/revoked, a second visit, cart changes, regular checkout and mobile checkout. Accelerated checkout/dynamic buy buttons may bypass cart writes; validate the actual theme paths before relying on coverage. Inspect the resulting Shopify order attributes and dashboard attribution. Existing orders without evidence cannot be reconstructed reliably.

## GA4 events and privacy

Final transitions queue `order_delivered` or `order_cancelled`. A GA4 `refund` reverses a prior GA4 purchase only when an exact transaction report match proves it existed. This accounting reversal does not claim money was refunded on an unpaid COD order. Partial refunds contain only the relevant amount/items; a later cancellation reverses the remaining recorded purchase value. Replays and repeated courier states do not queue another metric.

The event allowlist contains transaction ID, value/currency, catalog items, normalized status, limited courier/payment/cancellation enums and numeric campaign IDs. It excludes customer names, phones, addresses, emails, arbitrary order notes, landing/referrer URLs and click IDs. Custom item names are suppressed; catalog names with obvious email/phone/URL patterns are suppressed. Actual GA client ID and recorded analytics consent are required. Private order drill-down responses are authenticated, `no-store`, and protected by a same-origin CSP with no external tracking requests.

The outbox uses unique order/event/final-version keys. `validate-events` checks pending payloads against Google's Measurement Protocol validation endpoint and leaves accepted payloads in `validated`. `release-validated` returns those to pending for `dispatch`. Validation does not ingest events. Dispatch validates again before collection. Events older than 71 hours expire, leaving margin inside Google's 72-hour window; backfill never fabricates old events. See [Google's Measurement Protocol guidance](https://developers.google.com/analytics/devguides/collection/protocol/ga4/sending-events).

Transport outcomes are deliberately explicit: pending → attempting → sent / invalid / uncertain / expired. A crash after claiming or a collection timeout needs manual reconciliation. Measurement Protocol has no universal exactly-once acknowledgement; automatically retrying uncertain sends could duplicate events. Inspect a specific event against GA4 before any manual retry; do not bulk reset uncertain/attempting rows. Automated tests mock validation/collection; real validation and DebugView remain pending credentials.

Do not designate `order_cancelled` or `refund` as positive key events or advertising conversions. No bidding or conversion-goal changes were made. A future proposal to optimize for delivered orders must list exact conversion actions, primary/secondary settings, affected campaigns and attribution windows before changing live goals.

## Meta URL review

`python analytics_cli.py meta-url-audit` produces an exact ad/creative ID inventory with current and proposed URL tags once Meta credentials are supplied. It performs no writes. Review active/performance-sensitive ads and preserve unrelated existing URL parameters before applying the smallest safe update. Shared creatives and embedded destination parameters require inspection; the CLI proposal is not permission to overwrite them wholesale.

Required future convention:

```text
utm_source=facebook&utm_medium=paid_social&utm_campaign={{campaign.id}}&utm_term={{adset.id}}&utm_content={{ad.id}}
```

No live ads were changed. The exact affected-ad list and post-save verification remain pending API access. Do not change campaign bidding as part of URL standardization.

## Verification and remaining limits

Final local result (2026-09-14): **60 tests passed**, including database-backed signed webhooks and destructive rollback/reapply confined to a disposable in-memory PostgreSQL-compatible schema. Python compilation, JavaScript syntax and tracked diff whitespace checks passed. The actual authenticated Flask page was visually inspected at 1280×800 desktop and 392×844 mobile CSS viewports, with no document-level horizontal overflow. Browser scale required adjusting the viewport tool's dimensions; dimensions were verified from the rendered page and temporary overrides were reset.

Synthetic UI checks: 36 gross orders = 18 Delivered + 9 Cancelled + 9 In process; Cancelled + Cash on Delivery filter returned 9 with zero delivered revenue; campaign + product returned 12; the same combination on a single date returned 1; clicking Cancelled opened 9 drill-down orders; Reset returned all 36. The Reset button shadowing the native form method was fixed during this check. Preview data is synthetic and must not be interpreted as store performance.

Run `python -m pip install -r requirements-dev.txt`, set `TEST_DATABASE_URL` to a disposable PostgreSQL database, then `python -m pytest -q`. Each database test uses its own temporary schema. The root `test.py` is an unrelated interactive infinite-input script, not an existing automated suite; `pytest.ini` explicitly selects `tests/`.

The automated suite covers all twelve requested order/report cases plus stale updates, multi-shipment handling, consent/PII exclusion, pending refunds, COD cancelled revenue/spend, signed webhook replay, related-order fetching, migration reapplication/rollback, Meta purchase alias deduplication, date boundaries, exact purchase evidence, and ambiguous GA4 network outcomes. Local database testing used an ephemeral PGlite PostgreSQL-compatible server; production multi-worker lock contention still needs verification on Railway Postgres.

Live read-only samples inspected: Shopify order `PK2827A01` was pending payment and unfulfilled, consistent with In process. Courier tracking `10068610605830` for `PK2716A01` showed final `RETURN SUBMITTED` while the old dashboard context still showed Need Attention; the new classifier correctly treats the final courier result as Cancelled. These are sample observations, not a completed historical reconciliation. No customer details are included here.

Before activation, compare representative new, delivered, cancelled, Being Return and partial-refund orders against Shopify and courier sources; reconcile full date-range counts and amounts; replay a signed webhook; validate real GA4 payloads; inspect DebugView with a recent consented test order; and inspect outbound payloads for PII. Verify GA4 product/event dimension compatibility and account timezone/currency using actual reporting responses.

Product-level advertising spend/ROAS remains unavailable where an exact ad-to-product mapping is absent. Status/payment/courier filters also withhold spend rather than allocate campaign spend arbitrarily. Ad set/ad behavioral metrics are unavailable without matching GA4 dimensions. Google campaign-only residuals preserve spend for types such as Performance Max but may lack platform purchase metrics. These limitations are visible in the screen; they should not be interpreted as zeros.

## Changed files

| Files | Purpose |
| --- | --- |
| `main.py`, `templates/base.html` | Existing synchronization/webhook hooks, startup test guard, analytics navigation. |
| `order_analytics.py`, `analytics_store.py` | Normalized ledger, attribution, refunds, audit and event outbox. |
| `analytics_integrations.py`, `analytics_reporting.py`, `analytics_routes.py`, `analytics_cli.py` | API ingestion, report formulas/filters, authenticated routes, operational commands. |
| `migrations/002_analytics_integrity.sql`, corresponding rollback | Additional ledger/outbox fields and daily report storage; existing 001 files reused unchanged. |
| `queries/order_attribution.graphql`, `storefront/snippets/ss-attribution.liquid` | Shopify journey fallback and first-party attribution capture; validated through Shopify skills during implementation. |
| `templates/analytics.html`, `static/analytics.css`, `static/analytics.js` | Responsive screen, KPI cards, sortable expandable tables and order drill-down. |
| `.env.example`, `.gitignore`, `requirements.txt`, `requirements-dev.txt`, `pytest.ini` | Configuration, secret exclusions, integration/test dependencies. |
| `tests/conftest.py`, `tests/test_analytics.py`, `tests/test_analytics_boundaries.py`, `tests/preview.py` | Isolated tests and synthetic preview of the actual application. |
| `ANALYTICS.md` | Deployment, rollback, credentials, verification and remaining manual work. |
