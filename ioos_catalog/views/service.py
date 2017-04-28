import json
import urlparse
from datetime import datetime, timedelta
from pymongo import DESCENDING
from flask.ext.wtf import Form
from flask import render_template, redirect, url_for, request, flash, jsonify, Response, g
from flask import make_response
from wtforms import TextField, IntegerField, SelectField, BooleanField
from bson import json_util, ObjectId

from ioos_catalog.models.harvests import HarvestStatus
from ioos_catalog import app, db, queue, support_jsonp, requires_auth
from ioos_catalog.models.stat import Stat
from ioos_catalog.tasks.stat import ping_service_task
from ioos_catalog.tasks.reindex_services import reindex_services
from ioos_catalog.tasks.harvest import harvest
from ioos_catalog.util import build_links


class ServiceForm(Form):
    name = TextField(u'Name')
    url = TextField(u'URL')
    service_id = TextField(u'Service ID')
    service_type = SelectField(u'Service Type', choices=[(u'WMS', u'WMS'),
                                                         (u'DAP', u'DAP'),
                                                         (u'WCS', u'WCS'),
                                                         (u'SOS', u'SOS')])
    data_provider = TextField(u'Data Provider')
    geophysical_params = TextField(u'Geophysical Parameters')
    contact = TextField(u'Contact Emails',
                        description="A list of emails separated by commas")
    interval = IntegerField(u'Update Interval', description="In seconds")
    active = BooleanField(u'Active', description="Service is active")


@app.route('/services/', defaults={'filter_provider': None, 'filter_type': None, 'oformat': None}, methods=['GET'])
@app.route('/services/filter/', defaults={'filter_provider': None, 'filter_type': None, 'oformat': None}, methods=['GET'])
@app.route('/services/filter/<path:filter_provider>', defaults={'filter_type': None, 'oformat': None}, methods=['GET'])
@app.route('/services/filter/<path:filter_provider>/<filter_type>', defaults={'oformat': None}, methods=['GET'])
@app.route('/services/filter/<path:filter_provider>/<filter_type>/<oformat>', methods=['GET'])
@support_jsonp
def services(filter_provider, filter_type, oformat):
    # it's hard to get flask to route correctly with paths/json - fixup for
    # "/" providers with no oformat
    if oformat == "null":
        filter_provider = "/".join([filter_provider, filter_type])
        filter_type = "null"
        oformat = None

    filters = {'active': True}
    titleparts = []

    if filter_provider is not None and filter_provider != "none":
        titleparts.append(filter_provider)
        filters['data_provider'] = {'$in': filter_provider.split(',')}

    if filter_type is not None and filter_type != "none":
        titleparts.append(filter_type)
        filters['service_type'] = {'$in': filter_type.split(',')}

    # build title
    titleparts.append("Services")
    g.title = " ".join(titleparts)

    f = ServiceForm()
    services = list(db.Service.find(filters))
    sids = [s._id for s in services]
    latest_stats = db.Harvest.find(
        {'service_id': {'$in': sids}})
    latest_stats = {p.service_id: p for p in latest_stats}
    # mapping of service ids to summary stats about that service, or blanked
    # versions
    service_stats = {}

    for s in services:
        service_stats[s._id] = {'last_operational_status': 0,
                                'last_response_time': None,
                                'last_response_code': None,
                                'last_update': None,
                                'harvest_status': u'No harvest attempted',
                                'harvest_rate': '0/0',
                                'avg_response_time': None}

        if s._id in latest_stats:
            stat = latest_stats[s._id]

            service_stats[s._id][
                'last_operational_status'] = stat.harvest_successful
            app.logger.error("UPDATE STATUS: %s", stat.harvest_successful)
            service_stats[s._id]['last_update'] = stat.harvest_date
            service_stats[s._id]['harvest_status'] = stat.harvest_status
            service_stats[s._id]['harvest_rate'] = stat.success_rate()

    if oformat is not None and oformat == 'json':
        resp = json.dumps({'services': [dict(dict(s).items(
        ) + service_stats[s._id].items()) for s in services]}, default=json_util.default)
        return Response(resp, mimetype='application/json')

    # get TLD grouped statistics
    tld_stats = {}

    tld_groups = db.Service.group_by_tld(sids)
    for k, v in tld_groups.iteritems():
        tld_stats[k] = {'ok': 0, 'total': 0}
        for sid in v:
            tld_stats[k]['total'] += 1
            if sid in latest_stats and latest_stats[sid].harvest_successful == HarvestStatus.SUCCESS:
                tld_stats[k]['ok'] += 1

    # get list of unique providers in system
    providers = db["services"].distinct('data_provider')

    return render_template('services.html', services=services, service_stats=service_stats, form=f, tld_stats=tld_stats, providers=providers, filters=filters)


@app.template_filter('status_icon')
def status_icon_helper(status_val):
    if status_val:
        return "<span class=\"glyphicon glyphicon-ok\"></span>"
    return "<span class=\"glyphicon glyphicon-exclamation-sign\"></span>"


@app.route('/services/<ObjectId:service_id>', methods=['GET'])
def show_service(service_id):
    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)

    service = db.Service.find_one({'_id': service_id})

    g.title = service.name

    # Organize datasets by type.  Include the UID and _id of each dataset in
    # the output so we can link to them.
    datasets = db.Dataset.aggregate([
        {'$match': {'services.url': service.url}},
        {'$group': {'_id': '$services.asset_type',
                    'datasets': {'$push': {'uid': '$uid', '_id': '$_id'}}}}
    ])

    # get cc/metamap
    metadata_parent = db.Metadata.find_one({'ref_id': service._id})
    metadatas = {}
    if metadata_parent:
        metadatas = {m['checker']: m for m in metadata_parent.metadata if m[
            'service_id'] == service._id}

    # get rolling ping window
    ping_data = {'good': [], 'bad': []}
    last_ping = {'time': None,
                 'response_time': None,
                 'operational_status': None}

    harvest = db.Harvest.find_one({'service_id': service._id})

    return render_template('show_service.html',
                           service=service,
                           datasets=datasets,
                           harvest=harvest,
                           metadatas=metadatas)


@app.route('/services/', methods=['POST'])
@requires_auth
def add_service():
    f = ServiceForm()
    service = db.Service()
    # if f.validate():
    f.populate_obj(service)
    url = urlparse.urlparse(service.url)
    service.tld = url.hostname
    service.save()

    flash("Service '%s' Registered" % service.name, 'success')
    return redirect(url_for('services'))


@app.route('/services/<ObjectId:service_id>', methods=['POST'])
@requires_auth
def edit_service_submit(service_id):
    f = ServiceForm()
    service = db.Service.find_one({'_id': service_id})

    #@TODO: validation
    f.populate_obj(service)

    url = urlparse.urlparse(service.url)
    service.tld = url.hostname
    service.save()

    flash("Service '%s' updated" % service.name, 'success')
    return redirect(url_for('show_service', service_id=service_id))


@app.route('/services/<ObjectId:service_id>/delete', methods=['POST'])
@requires_auth
def delete_service(service_id):
    service = db.Service.find_one({'_id': service_id})
    service.delete()

    flash("Deleted service %s" % service.name)
    return redirect(url_for('services'))


@app.route('/services/<ObjectId:service_id>/ping', methods=['GET'])
@requires_auth
def ping_service(service_id):
    ret = ping_service_task(service_id)
    flash("Ping returned: %s" % ret)
    return redirect(url_for('show_service', service_id=service_id))


@app.route('/services/<ObjectId:service_id>/harvest', methods=['GET'])
@requires_auth
def harvest_service(service_id):
    s = db.Service.find_one({'_id': service_id})

    queue.enqueue_call(harvest, args=(service_id,), timeout=500)
    #h = harvest(service_id, ignore_active=True)
    flash("Harvest queued")
    return redirect(url_for('show_service', service_id=service_id))


@app.route('/services/<ObjectId:service_id>/start_monitoring', methods=['POST'])
@requires_auth
def start_monitoring_service(service_id):
    s = db.Service.find_one({'_id': service_id})
    assert s is not None

    s.active = True
    s.save()

    flash("Started monitoring the '%s' service" % s.name)
    return redirect(url_for('show_service', service_id=service_id))


@app.route('/services/<ObjectId:service_id>/stop_monitoring', methods=['POST'])
@requires_auth
def stop_monitoring_service(service_id):
    s = db.Service.find_one({'_id': service_id})
    assert s is not None

    s.active = False
    s.save()

    flash("Stopped monitoring the '%s' service" % s.name)
    return redirect(url_for('show_service', service_id=service_id))


@app.route('/services/<ObjectId:service_id>/start_harvesting', methods=['POST'])
@requires_auth
def start_harvesting_service(service_id):
    s = db.Service.find_one({'_id': service_id})
    assert s is not None

    s.active = True
    s.save()

    flash("Started harvesting the '%s' service" % s.name)
    return redirect(url_for('show_service', service_id=service_id))


@app.route('/services/<ObjectId:service_id>/stop_harvesting', methods=['POST'])
@requires_auth
def stop_harvesting_service(service_id):
    s = db.Service.find_one({'_id': service_id})
    assert s is not None

    s.active = False
    s.save()

    flash("Stopped harvesting the '%s' service" % s.name)
    return redirect(url_for('show_service', service_id=service_id))


@app.route('/services/<ObjectId:service_id>/edit', methods=['GET'])
@requires_auth
def edit_service(service_id):
    service = db.Service.find_one({'_id': service_id})
    g.title = "Editing " + service.name
    f = ServiceForm(obj=service)
    return render_template('edit_service.html', service=service, form=f)


@app.route('/services/reindex', methods=['GET'])
@requires_auth
def reindex():
    queue.enqueue(reindex_services)
    return jsonify({"message": "queued"})


@app.route('/services/feed.xml', methods=['GET'])
def atom_feed():
    services = list(db.Service.find({'service_type': {'$ne': 'DAP'}}))

    for s in services:
        # Make the default TO the default for the FGDC feed... always.
        # if s.contact is None or s.contact != "":
        s.contact = app.config.get("MAIL_DEFAULT_TO")

    return Response(render_template('feed.xml', services=services), mimetype='text/xml')


@app.route('/services/devfeed.xml', methods=['GET'])
def dev_atom_feed():
    services = list(db.Service.find())

    for s in services:
        # if s.contact is None or s.contact != "":
        s.contact = app.config.get("MAIL_DEFAULT_TO")

    return Response(render_template('feed.xml', services=services), mimetype='text/xml')


@app.route('/services/daily', methods=['GET'], defaults={'year': None, 'month': None, 'day': None})
@app.route('/services/daily/<int:year>/<int:month>/<int:day>', methods=['GET'])
def daily(year, month, day):
    end_time = None
    if year is not None and month is not None and day is not None:
        end_time = datetime.strptime(
            "%s/%s/%s" % (year, month, day), "%Y/%m/%d")

    failed_services, services, end_time, start_time = db.Service.get_failures_in_time_range(
        end_time=end_time)
    g.title = "Daily Report (%s)" % end_time.strftime("%Y-%m-%d")
    return render_template("daily_service_report_page.html", services=services, failed_services=failed_services, start_time=start_time, end_time=end_time)


def get_search_terms():
    '''
    Returns a tuple containing the query and page_query dictionaries.
    :return: A tuple containing the query and page_query dictionaries
    :rtype: tuple
    '''
    query = {}
    page_query = {}
    search_term = request.args.get('search', None)
    provider = request.args.get('data_provider', None)
    service_type = request.args.get('service_type', None)
    active = request.args.get('active', 'true')
    if search_term:
        query['$text'] = {'$search': search_term, '$language': 'en'}
        page_query['search'] = search_term
    if provider:
        query['data_provider'] = provider
        page_query['data_provider'] = provider
    if service_type:
        query['service_type'] = service_type
        page_query['service_type'] = service_type
    if active == 'true':
        query['active'] = True
    elif active == 'false':
        query['active'] = False
        page_query['active'] = 'false'
    elif active in ('all', 'any'):
        page_query['active'] = 'all'

    return query, page_query


@app.route('/api/service', methods=['GET'])
def get_services():
    '''
    GET /api/service{?page,search,data_provider,service_type,active}

    Returns a JSON list of services for the query specified.
    '''
    from ioos_catalog.views.dataset import get_page_info
    page_limit, page = get_page_info()
    query, page_query = get_search_terms()

    cursor = db.Service.find(query)

    start = page_limit * page
    links = build_links(cursor.count(), page, page_limit, page_query)

    response = list(cursor[start:start + page_limit])
    app.logger.info("slice: (%s, %s)", start, start + page_limit)
    response = make_response(json.dumps(response, default=json_util.default))
    response.headers['Content-Type'] = 'application/json; charset=utf-8'
    response.headers['Link'] = ', '.join(links)
    response.headers['X-Total'] = cursor.count()
    response.headers['X-Per-Page'] = page_limit
    return response


@app.route('/api/service/<string:service_id>', methods=['GET'])
def get_service(service_id):
    '''
    GET /api/service/{service_id}

    Returns a JSON object of a service record
    '''
    try:
        service_id = ObjectId(service_id)
    except:
        return jsonify(error="ValueError", message="Invalid ObjectId"), 400

    service = db.Service.find_one({"_id": service_id})
    if service is None:
        return jsonify(error="NotFound", message="No service for this id"), 404
    response = make_response(json.dumps(service, default=json_util.default))
    return response
