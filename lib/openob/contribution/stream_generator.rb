# Subclasses of this are the actual stream generators. They are what the 
# contribution system uses to actually spit out data at the endpoint.
# Essentially this is where we wrap ffmpeg and friends.
class OpenOB::Contribution::StreamGenerator
  def initialize
    raise NotImplementedError, "Don't use the StreamGenerator class directly."
  end
end