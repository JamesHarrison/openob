require "bundler/setup"
Bundler.setup(:default)
# OpenOB
module OpenOB
  module Contribution
    
  end
end
LOGGER = Logger.new(STDERR)
LOGGER.level = Logger::DEBUG
require 'open3'
require 'contribution/stream_generator'
require 'contribution/stream_generators/udp_mpegts_generator'
