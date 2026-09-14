"""Synthetic local preview only. Runs the actual application, routes and report engine."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def seed():
    from datetime import date, timedelta
    from analytics_store import sync_order, sync_tracking, save_report
    today = date.today()
    for idx in range(36):
        day = today - timedelta(days=idx % max(1, today.day))
        channel = ['meta','google','Unattributed'][idx % 3]
        attrs = {'utm_source':'facebook' if channel=='meta' else 'google','utm_medium':'paid_social' if channel=='meta' else 'cpc',
                 'utm_campaign':'123' if channel=='meta' else '321','utm_term':'456','utm_content':'789'} if channel!='Unattributed' else {}
        order = dict(id=900000+idx,name=f'DEMO-{idx+1:03}',created_at=day.isoformat()+'T12:00:00+05:00',updated_at=day.isoformat()+'T12:00:00+05:00',
            currency='PKR',total_price='3200',subtotal_price='3000',total_discounts='0',
            total_shipping_price_set={'shop_money':{'amount':'200'}},financial_status='pending',fulfillment_status='fulfilled',
            payment_gateway_names=['Cash on Delivery'],fulfillments=[dict(status='success',tracking_number=f'DEMO{idx}',tracking_company='Leopards')],
            line_items=[dict(id=910000+idx,product_id=501+idx%3,variant_id=601+idx%3,title=['Oak serving tray','Ceramic vase','Linen cushion'][idx%3],price='3000',quantity=1)],
            note_attributes=[dict(name='ss_'+k,value=v) for k,v in attrs.items()])
        sync_order(order,source='synthetic_preview',emit=False)
        sync_tracking(f'DEMO{idx}',{'status':['Delivered','Returned to Shipper','Being Return','Delivered'][idx%4]},emit=False)
    for n in range(today.day):
        day=(today-timedelta(days=n)).isoformat()
        for channel,campaign,name in [('meta','123','Home essentials · prospecting'),('google','321','Search · everyday living')]:
            save_report(channel,day,'ads',[dict(channel=channel,campaign_id=campaign,campaign_name=name,group_id='456',group_name='Home & living',ad_id='789',ad_name='Make room for beautiful things',
                spend=450 if channel=='meta' else 250,impressions=18000,clicks=420,platform_purchases=8,platform_purchase_value=25600,currency='PKR')])
        base=dict(sessionSourceMedium='facebook / paid_social',sessionCampaignId='(not set)',sessionCampaignName='123')
        save_report('ga4',day,'events',[dict(base,eventName=k,eventCount=v) for k,v in [('view_item',240),('add_to_cart',38),('begin_checkout',15),('purchase',8),('refund',2)]])
        save_report('ga4',day,'sessions',[dict(base,totalUsers=270,sessions=300,engagedSessions=180,purchaseRevenue=25600)])
        save_report('ga4',day,'products',[dict(base,itemId='501',itemName='Oak serving tray',itemsViewed=240,itemsAddedToCart=38,itemsPurchased=8,itemRevenue=24000)])


if __name__=='__main__':
    if '127.0.0.1:55432' not in os.environ.get('DATABASE_URL',''):
        raise SystemExit('Preview only runs against the isolated local test database on port 55432')
    os.environ['INITIALIZE_APP']='false'
    os.environ['ORDER_ANALYTICS_ENABLED']='true'
    os.environ['APP_SECRET_KEY']='synthetic-preview-only'
    os.environ['ADMIN_PORTAL_PASSWORD']='preview-only'
    seed()
    import main
    main.app.run(host='127.0.0.1',port=5055,debug=False)
