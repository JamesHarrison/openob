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
