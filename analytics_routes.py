import os
from functools import wraps
from flask import Blueprint, jsonify, redirect, render_template, request, session
from analytics_reporting import date_range, report
from analytics_store import load_orders, load_reports

analytics = Blueprint('analytics', __name__)


def authenticated(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('admin_portal_authenticated'):
            return (jsonify(error='Authentication required'), 401) if request.path.startswith('/api/') else redirect('/admin_portal')
        if os.getenv('ORDER_ANALYTICS_ENABLED') != 'true':
            return (jsonify(error='Analytics migration and activation are required'), 503) if request.path.startswith('/api/') else render_template('analytics.html', enabled=False)
        return fn(*args, **kwargs)
    return wrapper


@analytics.after_request
def private_response(response):
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
    return response


@analytics.get('/analytics')
@analytics.get('/marketing-performance')
@authenticated
def page():
    return render_template('analytics.html', enabled=True)


@analytics.get('/api/analytics')
@authenticated
def data():
    try:
        start, end, since, until = date_range(request.args)
        result = report(load_orders(since, until), load_reports(start,end), request.args, start,end)
        result['funnel']['users'] = None
        if os.getenv('GOOGLE_APPLICATION_CREDENTIALS'):
            try:
                from analytics_integrations import unique_users
                result['funnel']['users'] = unique_users(start,end,request.args)
            except Exception:
                result['warnings'].append('Unique users could not be retrieved from GA4 for this range.')
        return jsonify(result)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except Exception:
        return jsonify(error='Analytics data could not be loaded. Check migration and database connectivity.'), 503
