## 5.0.1

* Lowered the Python floor to 3.9 so OpenOB runs on pre-Bookworm distros (Debian 11 Bullseye = Python 3.9, GStreamer 1.18). All `X | Y` annotations are already gated by `from __future__ import annotations` so the change is metadata-only on the source side.
* Added a tiny GStreamer-version-compat shim: `request_pad_simple` (1.20+) falls back to `get_request_pad` (1.18 and earlier) so `rtpbin` pad-request works across the full 1.18 → current range.
* Verified end-to-end on both Debian 12 / GStreamer 1.22 / Python 3.11 *and* Debian 11 / GStreamer 1.18 / Python 3.9.
* Widened the `redis` dependency to `>=3` (no upper bound) so OS-supplied installs (Debian 11 ships redis-py 3.5.x) coexist with the latest releases.

## 5.0.0

* Revival release: codebase modernised for current Python and GStreamer
* Python 3.10+ required (Python 2 support dropped); type hints added across the project
* GStreamer 1.22+ required: replaced deprecated `get_request_pad` with `request_pad_simple`, switched to canonical hyphenated property names (`auto-multicast`, `multicast-group`, `post-messages`)
* redis-py 5.x compatible: switched from `StrictRedis` to `Redis`, dropped removed `charset` kwarg, fixed boolean type-coercion bug in `LinkConfig.get`
* Fixed Python 2 tuple-style `raise` in `LinkConfig.commit_changes`
* Configuration host now accepts `host:port` form for non-default Redis ports
* New `--test_wave` flag on `openob tx -a test` to select audiotestsrc waveform (sine, pink-noise, white-noise, etc.)
* New `wav` audio output mode on `openob rx` (`-a wav --wav_file PATH`) for capturing the decoded stream to a WAV file — used by the test harness
* Switched packaging from `setup.py` to `pyproject.toml` (PEP 621)
* New end-to-end test harness in `tests/` covering pink-noise round-trip over both PCM and Opus, asserting captured WAV is non-silent
* Replaced Travis CI with GitHub Actions running tests in a Docker Compose container; same `docker compose run --rm tests` works locally on any platform

## 4.0.0-dev

* Upgraded GStreamer libraries to ^1.0
* Added Python 3 compatability

## 3.1

* Improved command line interface (Jonty Sewell)
* Bugfix for PCM mode operation (Jonty Sewell)

## 3.0.3

* Packaging bugfixes

## 3.0.0

* Released

## 3.0.0a1

* Refactored the Manager into Node, LinkConfig, AudioInterface and Logger classes
* Refactored logging to use Python's logging framework
* Removed audio level feedback on the command line
* Moved configuration into objects for shared and node-specific (audio interface) values
* Removed PulseAudio element support (except through the 'auto' interface type)
* Removed variable size input queue, since this was an attempt to fix the audiorate related glitches but made no difference
* Removed CELT support; only Opus is now supported for compressed audio
* Removed colorama dependency
* Enabled support for disabling automatic connection under JACK (Chris Roberts)
* Added support for asking the audio interface for a sample rate (Chris Roberts)

## 2.3.6

* Fixed level messages reporting incorrect channel counts

## 2.3.5

* Added multicast support (Jonty Sewell)
* Added automatic audio interface selection support (Jonty Sewell)

## 2.3.4

* Minor bugfix

## 2.3.3

* Added support for Opus features:

    * Discontinuous transmission
    * In-band Forward Error Correction (and loss % assumption)
    * Frame size selection

## <2.3.3

* Check the git changelog for these changes.
