# Windows local I/O

Picking a specific WASAPI device on a Windows host. Useful when there
are multiple USB audio interfaces attached and you don't want OpenOB to
follow whichever one Windows last picked as the system default.

## Prerequisites

- GStreamer development + runtime MSI installers from
  https://gstreamer.freedesktop.org/download/. Install the **same
  version** of both. `wasapi2src` and `wasapi2sink` come bundled in
  `gst-plugins-good`, which the standard MSI installs.
- Add the GStreamer `bin` directory to `PATH`.
- Python 3.9+ with PyGObject. The simplest path on Windows is to use
  the same MSYS2 / Conda combination that ships PyGObject pre-built;
  `pip install pygobject` from a vanilla Python interpreter will fight
  you over GLib headers.

## Discovering devices

```pwsh
openob list-devices --backend wasapi
```

You'll get something like:

```
WASAPI (Windows): 4 device(s)
  Source     [default]   'Microphone (Realtek Audio)'  id=\\?\SWD#MMDEVAPI#...
                       audio_input/audio_output = "wasapi"
                       audio_device = "Microphone (Realtek Audio)"
  Source                 'Microphone (Focusrite USB)'  id=\\?\SWD#MMDEVAPI#...
                       audio_input/audio_output = "wasapi"
                       audio_device = "Microphone (Focusrite USB)"
  ...
```

Copy a unique substring into `audio_device`. Substring matching is
case-insensitive — `"focusrite"` and `"Focusrite"` are equivalent.

## Why match by display name, not GUID

The MMDevice path id (`\\?\SWD#MMDEVAPI#...`) can churn when a device
driver is reinstalled, when Windows assigns a new instance id after a
USB-C dock disconnect, or when the device is moved between machines.
Display-name matching is stable across these — when a Focusrite shows
up again with a fresh GUID, the supervisor restart loop in OpenOB picks
it up by its name on the next pipeline rebuild.

## Exclusive-mode contention

Other applications (Zoom, Skype, DAWs) can hold a WASAPI device in
exclusive mode and lock OpenOB out. For production use, open the
Windows Sound control panel → device → Advanced and untick "Allow
applications to take exclusive control of this device".

## Running as a Windows Service

Audio device access from a Service session is locked down by default —
Services don't have an audio session attached to them in the way an
interactive logon does. If you need OpenOB to run unattended on
Windows, the supported paths are:

- Run interactively (RDP-detached) under a real user account.
- Use `nssm`/Task Scheduler with "Run only when user is logged on".
- Run a Windows-on-WSL2 container with audio passthrough (advanced).

A pure Windows Service deployment will succeed at building the
pipeline but will see zero packets in/out from WASAPI. This is a
Windows audio-stack constraint, not an OpenOB limitation.
