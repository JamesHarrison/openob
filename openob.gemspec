# -*- encoding: utf-8 -*-
$:.push File.expand_path("../lib", __FILE__)
require "openob/version"

Gem::Specification.new do |s|
  s.name        = "openob"
  s.version     = OpenOB::VERSION
  s.authors     = ["James Harrison"]
  s.email       = ["james@talkunafraid.co.uk"]
  s.homepage    = "http://www.talkunafraid.co.uk/"
  s.summary     = %q{OpenOB is a system for reliable real time audio link management}
  s.description = %q{OpenOB is a system for reliable real time low latency audio link management, targeted at broadcast users, based around ffmpeg and friends.}
  s.files         = `git ls-files`.split("\n")
  s.test_files    = `git ls-files -- {test,spec,features}/*`.split("\n")
  s.executables   = `git ls-files -- bin/*`.split("\n").map{ |f| File.basename(f) }
  s.require_paths = ["lib"]
  s.add_dependency "daemons", "~> 1.1.4"
  s.add_dependency "slop", "~> 2.1.0"
  s.add_dependency "haml", "~> 3.1.3"
  s.add_dependency "sass", "~> 3.1.10"
  s.add_dependency "sinatra", "~> 1.3.1"
  s.add_dependency "thin", "~ 1.2.11"
end
