from pyramid.view import view_config
from pyramid.httpexceptions import HTTPFound
from pyramid.renderers import render_to_response
import yaml
import redis
import string

@view_config(route_name='home', renderer='templates/index.pt')
def index(request):
    static_conf = yaml.load(open("../config.yml", 'r'))
    if static_conf['configuration_host'] != "":
        config = redis.Redis(static_conf['configuration_host'])
    else:
        return render_to_response('webui:templates/configserver.pt', {'static_conf':static_conf}, request=request)
    return {'static_conf':static_conf, 'config': config}

@view_config(route_name='save')
def save(request):
    static_conf = yaml.load(open("../config.yml", 'r'))
    static_conf['tx']['receiver_address'] = request.params['tx_receiver_address']
    static_conf['tx']['configuration_name'] = request.params['tx_configuration_name']
    static_conf['tx']['audio_connection'] = request.params['tx_audio_connection']
    static_conf['tx']['alsa_device'] = request.params['tx_alsa_device']
    static_conf['tx']['bitrate'] = int(request.params['tx_bitrate'])*1000
    static_conf['tx']['base_port'] = request.params['tx_base_port']
    static_conf['tx']['jitter_buffer_size'] = request.params['tx_jitter_buffer']
    if request.params['tx_link_mode'] == 'celt':
        static_conf['tx']['payloader']['tx'] = 'rtpceltpay'
        static_conf['tx']['payloader']['rx'] = 'rtpceltdepay'
        static_conf['tx']['encoder']['tx'] = 'celtenc'
        static_conf['tx']['encoder']['rx'] = 'celtdec'
    elif request.params['tx_link_mode'] == 'pcm':
        static_conf['tx']['payloader']['tx'] = 'rtpL16pay'
        static_conf['tx']['payloader']['rx'] = 'rtpL16depay'
        static_conf['tx']['encoder']['tx'] = 'identity'
        static_conf['tx']['encoder']['rx'] = 'identity'
    static_conf['rx']['configuration_name'] = request.params['rx_configuration_name']
    static_conf['rx']['audio_connection'] = request.params['rx_audio_connection']
    static_conf['rx']['alsa_device'] = request.params['rx_alsa_device']
    yaml.dump(static_conf, file('../config.yml', 'w'))
    return HTTPFound(location='/')
@view_config(route_name='save_confighost')
def save_confighost(request):
    static_conf = yaml.load(open("../config.yml", 'r'))
    static_conf['configuration_host'] = request.params['configuration_host']
    yaml.dump(static_conf, file('../config.yml', 'w'))
    return HTTPFound(location='/')

@view_config(route_name='tx_levels',renderer='json')
def tx_levels(request):
    static_conf = yaml.load(open("../config.yml", 'r'))
    config = redis.Redis(static_conf['configuration_host'])
    caps = config.get((static_conf['caps_key']+static_conf['tx']['configuration_name']))
    return {
      'rms_left':config.lrange((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":rms:left"),0,600),
      'rms_right':config.lrange((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":rms:right"),0,600),
      'peak_left':config.lrange((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":peak:left"),0,600),
      'peak_right':config.lrange((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":peak:right"),0,600),
      'timestamp':config.lindex((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":utc_timestamps"),0),
      'payloader_name': static_conf['tx']['payloader']['tx'],
      'encoder_name': static_conf['tx']['encoder']['tx'],
      'caps': caps
      }
@view_config(route_name='rx_levels',renderer='json')
def rx_levels(request):
    static_conf = yaml.load(open("../config.yml", 'r'))
    config = redis.Redis(static_conf['configuration_host'])
    caps = config.get((static_conf['caps_key']+static_conf['rx']['configuration_name']))
    base_port = int(config.get((static_conf['port_key']+static_conf['rx']['configuration_name'])))
    buffer_size = int(config.get((static_conf['buffer_size_key']+static_conf['rx']['configuration_name'])))
    depayloader_name = config.get((static_conf['depayloader_key']+static_conf['rx']['configuration_name']))
    decoder_name = config.get((static_conf['decoder_key']+static_conf['rx']['configuration_name']))
    return {
      'rms_left':config.lrange((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":rms:left"),0,600),
      'rms_right':config.lrange((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":rms:right"),0,600),
      'peak_left':config.lrange((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":peak:left"),0,600),
      'peak_right':config.lrange((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":peak:right"),0,600),
      'timestamp':config.lindex((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":utc_timestamps"),0),
      'caps': caps,
      'depayloader_name': depayloader_name,
      'decoder_name': decoder_name
      }