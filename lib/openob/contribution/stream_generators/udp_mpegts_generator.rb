# Generates a
class OpenOB::Contribution::UdpMPEGTSGenerator < OpenOB::Contribution::StreamGenerator
  def initialize(params={})
    defaults = {:hostname=>'localhost', :port=>15000, :pkt_size=>1360, :buffer_size=>nil, :codec=>'libmp3lame', :bitrate=>192, :input=>'-f alsa -i hw:0,0', :channels=>1}
    @params = defaults.merge(params)
  end
  def launch
    loop do
      codec_config = "-acodec #{params[:codec]} -ab #{params[:bitrate]}k -ac #{params[:channels]}"
      ffmpeg_cmd = "ffmpeg #{params[:input]} #{codec_config} -f mpegts udp://#{params[:hostname]}:#{params[:port]}?pkt_size=#{params[:pkt_size]}#{params[:buffer_size] ? "&buffer_size=#{params[:buffer_size]}" : ""}"
      LOGGER.info("Starting ffmpeg with command '#{ffmpeg_cmd}'")
      Open3.popen2e(ffmpeg_cmd) do |stdin, stdouterr, wait_thr|
        pid = wait_thr.pid
        
        exit_status = wait_thr.value # Process::Status object
      end
    end
  end
end