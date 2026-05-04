# macOS local I/O

Picking a specific CoreAudio device on a Mac mini or MacBook running
OpenOB, instead of riding the system default. Useful when several USB
or Thunderbolt audio interfaces are attached and you don't want OpenOB
to follow the system-wide "default output" toggle in Audio MIDI Setup.

## Prerequisites

- GStreamer + plugins. Homebrew is the path of least resistance:
  ```sh
  brew install gstreamer gst-plugins-base gst-plugins-good
  ```
  (`osxaudiosrc` and `osxaudiosink` ship in `gst-plugins-good`.)
- PyGObject for Python — `pip install pygobject` after installing
  `pkg-config` and `glib` via Homebrew.

## Discovering devices

SSH in (or just open a terminal) and run:

```sh
openob list-devices --backend coreaudio
```

You'll get something like:

```
CoreAudio (macOS): 3 device(s)
  Source     [default]   'Built-in Microphone'  id=49
                       audio_input/audio_output = "coreaudio"
                       audio_device = "Built-in Microphone"
  Source                 'Scarlett 2i2 USB'     id=72
                       audio_input/audio_output = "coreaudio"
                       audio_device = "Scarlett 2i2 USB"
  Sink       [default]   'Built-in Output'      id=51
  ...
```

Copy a unique substring of the display name into the `audio_device`
field of your link TOML. Substring matching is case-insensitive, so
`"scarl"` works the same as `"Scarlett 2i2 USB"`.

## Why match by display name, not numeric ID

CoreAudio AudioDeviceIDs are opaque uints that can change when a USB
device is unplugged and replugged, or when a different Mac (with
different USB topology) is used as a backup. Display-name matching is
stable across replug events — when a Scarlett momentarily disconnects,
the supervisor restart loop in OpenOB rebuilds the pipeline, the
DeviceMonitor enumerates again, and the same `audio_device = "Scarlett"`
match still finds it.

## What to do if there are two devices with the same name

Two cheap USB audio dongles will both report "USB Audio Device" as their
`iProduct` string. `openob list-devices` shows both with their numeric
IDs; pick the one you want and either rename the device in Audio MIDI
Setup, or fall back to the numeric id (e.g. `audio_device = "72"`).
