require "openob/version"
require "logger"
require "daemons"
module OpenOB
  LOGGER = Logger.new(STDERR)
  LOGGER.level = Logger::WARN
  # Your code goes here...
end
