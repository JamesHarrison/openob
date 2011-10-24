require 'open3'
require 'openob/contribution/stream_generator'
require 'openob/contribution/stream_generators/udp_mpegts_generator'
require 'openob/contribution/webui'
# OpenOB
module OpenOB
  module Contribution
    def self.setup(params)
      OpenOB::LOGGER.info "Starting contribution link with parameters #{params.inspect}"
      stream_generator = OpenOB::Contribution::UdpMPEGTSGenerator.new({:hostname=>params[:hostname], :port=>params[:port], :input=>'-f alsa -i hw:0,0'})
      OpenOB::LOGGER.debug stream_generator.inspect
      webui = Thread.new do
        Thin::Server.start('0.0.0.0', params[:webport]) do
          use Rack::CommonLogger
          use Rack::ShowExceptions
          map '/' do
            run OpenOB::Contribution::WebUI.new()
          end
        end
      end
      OpenOB::LOGGER.info "WebUI starting at http://127.0.0.1:9000/"
      OpenOB::LOGGER.debug webui.inspect
      stream = Thread.new do
        stream_generator.launch
      end
      OpenOB::LOGGER.info "Stream generator launched"
      OpenOB::LOGGER.debug stream.inspect
      loop do
        OpenOB::LOGGER.info "WebUI state: #{webui.inspect} - ffmpeg state: #{stream.inspect}"
        sleep 5
      end
    end
  end
end
