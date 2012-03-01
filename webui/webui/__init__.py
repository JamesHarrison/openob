from pyramid.config import Configurator

def main(global_config, **settings):
    """ This function returns a Pyramid WSGI application.
    """
    config = Configurator(settings=settings)
    config.add_static_view('static', 'static', cache_max_age=3600)
    config.add_route('home', '/')
    config.add_route('save', '/save')
    config.add_route('save_confighost', '/save_confighost')
    config.add_route('tx_levels', '/tx_levels.json')
    config.add_route('rx_levels', '/rx_levels.json')
    config.scan()
    return config.make_wsgi_app()
