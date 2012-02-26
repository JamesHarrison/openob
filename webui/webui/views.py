from pyramid.view import view_config
from pyramid.httpexceptions import HTTPFound
import yaml
import redis

@view_config(route_name='home', renderer='templates/index.pt')
def index(request):
    static_conf = yaml.load(open("../config.yml", 'r'))
    config = redis.Redis(static_conf['configuration_host'])
    return {'static_conf':static_conf, 'config': config}

@view_config(renderer='templates/index.pt')
def save_tx(request):
    static_conf = yaml.load(open("../config.yml", 'r'))
    static_conf['tx']['receiver_address'] = request.params['tx_receiver_address']
    return HTTPFound(location='/')