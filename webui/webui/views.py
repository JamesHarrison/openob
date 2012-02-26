from pyramid.view import view_config
from pyramid.httpexceptions import HTTPFound
import yaml
import redis
import string

@view_config(route_name='home', renderer='templates/index.pt')
def index(request):
    static_conf = yaml.load(open("../config.yml", 'r'))
    config = redis.Redis(static_conf['configuration_host'])
    return {'static_conf':static_conf, 'config': config}

@view_config(route_name='save_tx',renderer='templates/index.pt')
def save_tx(request):
    static_conf = yaml.load(open("../config.yml", 'r'))
    static_conf['tx']['receiver_address'] = request.params['tx_receiver_address']
    return HTTPFound(location='/')

@view_config(route_name='tx_levels',renderer='json')
def tx_levels(request):
    static_conf = yaml.load(open("../config.yml", 'r'))
    config = redis.Redis(static_conf['configuration_host'])
    return {
      'rms_left':config.lrange((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":rms:left"),0,300),
      'rms_right':config.lrange((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":rms:right"),0,300),
      'peak_left':config.lrange((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":peak:left"),0,300),
      'peak_right':config.lrange((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":peak:right"),0,300),
      'timestamp':config.lindex((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":utc_timestamps"),0)
      }
@view_config(route_name='rx_levels',renderer='json')
def rx_levels(request):
    static_conf = yaml.load(open("../config.yml", 'r'))
    config = redis.Redis(static_conf['configuration_host'])
    return {
      'rms_left':config.lrange((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":rms:left"),0,300),
      'rms_right':config.lrange((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":rms:right"),0,300),
      'peak_left':config.lrange((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":peak:left"),0,300),
      'peak_right':config.lrange((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":peak:right"),0,300),
      'timestamp':config.lindex((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":utc_timestamps"),0)
      }