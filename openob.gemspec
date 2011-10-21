# -*- encoding: utf-8 -*-
$:.push File.expand_path("../lib", __FILE__)
require "openob/version"

Gem::Specification.new do |s|
  s.name        = "openob"
  s.version     = Openob::VERSION
  s.authors     = ["James Harrison"]
  s.email       = ["james@talkunafraid.co.uk"]
  s.homepage    = "http://www.talkunafraid.co.uk/"
  s.summary     = %q{OpenOB is a system for reliable real time audio link management}
  s.description = %q{OpenOB is a system for reliable real time audio link management, targeted at broadcast users}
  s.files         = `git ls-files`.split("\n")
  s.test_files    = `git ls-files -- {test,spec,features}/*`.split("\n")
  s.executables   = `git ls-files -- bin/*`.split("\n").map{ |f| File.basename(f) }
  s.require_paths = ["lib"]
end
