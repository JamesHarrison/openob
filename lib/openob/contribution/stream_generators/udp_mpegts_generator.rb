# Generates a nice and simple MPEG Transport Stream and sends it over UDP
require 'pty'
class OpenOB::Contribution::UdpMPEGTSGenerator < OpenOB::Contribution::StreamGenerator
  def initialize(options={})
    defaults = {:hostname=>'localhost', :port=>15000, :pkt_size=>1360, :buffer_size=>nil, :codec=>'libmp3lame', :bitrate=>192, :input=>'-f alsa -i hw:0,0', :channels=>1}
    @params = defaults.merge(options)
    OpenOB::LOGGER.info("UDP MPEG-TS Stream Generator - Configured with #{@params.inspect}")
  end
  def launch
    loop do
      codec_config = "-acodec #{@params[:codec]} -ab #{@params[:bitrate]}k -ac #{@params[:channels]}"
      ffmpeg_cmd = "ffmpeg #{@params[:input]} #{codec_config} -f mpegts udp://#{@params[:hostname]}:#{@params[:port]}?pkt_size=#{@params[:pkt_size]}#{@params[:buffer_size] ? "&buffer_size=#{@params[:buffer_size]}" : ""}"
      OpenOB::LOGGER.info("Starting ffmpeg with command '#{ffmpeg_cmd}'")
      Open3.popen3(ffmpeg_cmd) do |stdin, stdout, stderr, wait_thr|
        pid = wait_thr.pid
        OpenOB::LOGGER.info("ffmpeg started with PID #{pid}")
        stderr.read.split("\n").each do |line|
          OpenOB::LOGGER.debug "ffmpeg-#{pid}: #{line}"
        end
        exit_status = wait_thr.value
        OpenOB::LOGGER.error("ffmpeg exited with status #{exit_status.inspect}")
      end
    end
  end
end