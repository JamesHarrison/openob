require 'thin'
require 'sinatra/base'
require 'haml'
require 'sass'
class OpenOB::Contribution::WebUI < Sinatra::Base
  set :sessions, true

  get '/' do
    OpenOB::LOGGER.info "Handling GET to /"
    'Hello world!'
  end
end