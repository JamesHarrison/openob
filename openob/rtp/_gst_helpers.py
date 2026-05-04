"""Internal GStreamer helpers shared by the tx and rx pipelines."""
from __future__ import annotations

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst  # noqa: E402

# Idempotent — safe to call when Gst is already initialised by tx.py/rx.py.
Gst.init(None)


# Hint at the Debian/Ubuntu package most likely missing for a given element.
# Not exhaustive — a best-effort nudge for the common cases.
_PLUGIN_PACKAGE_HINT: dict[str, str] = {
    'opusenc': 'gstreamer1.0-plugins-base',
    'opusdec': 'gstreamer1.0-plugins-base',
    'rtpopuspay': 'gstreamer1.0-plugins-good',
    'rtpopusdepay': 'gstreamer1.0-plugins-good',
    'rtpL16pay': 'gstreamer1.0-plugins-good',
    'rtpL16depay': 'gstreamer1.0-plugins-good',
    'rtpbin': 'gstreamer1.0-plugins-good',
    'udpsink': 'gstreamer1.0-plugins-good',
    'udpsrc': 'gstreamer1.0-plugins-good',
    'level': 'gstreamer1.0-plugins-good',
    'wavenc': 'gstreamer1.0-plugins-good',
    'audiotestsrc': 'gstreamer1.0-plugins-base',
    'jackaudiosrc': 'gstreamer1.0-plugins-good',
    'jackaudiosink': 'gstreamer1.0-plugins-good',
    'alsasrc': 'gstreamer1.0-plugins-base',
    'alsasink': 'gstreamer1.0-plugins-base',
    'pulsesrc': 'gstreamer1.0-pulseaudio',
    'pulsesink': 'gstreamer1.0-pulseaudio',
    'osxaudiosrc': 'gst-plugins-good (macOS — brew install gst-plugins-good)',
    'osxaudiosink': 'gst-plugins-good (macOS — brew install gst-plugins-good)',
    'wasapi2src': 'gst-plugins-good (Windows — bundled with the GStreamer MSI installer)',
    'wasapi2sink': 'gst-plugins-good (Windows — bundled with the GStreamer MSI installer)',
}


def request_pad(element, name: str):
    """
    Request a named pad from an element across GStreamer versions.

    GStreamer 1.20 added :py:meth:`request_pad_simple` as the new, preferred
    spelling and deprecated :py:meth:`get_request_pad`. We support both 1.18
    (Debian Bullseye) and 1.20+ (Bookworm onwards) by trying the new name
    first and falling back to the legacy one.
    """
    fn = getattr(element, 'request_pad_simple', None)
    if fn is not None:
        return fn(name)
    return element.get_request_pad(name)


def make_element(factory: str, name: str | None = None):
    """
    Create a GStreamer element or raise a useful error.

    ``Gst.ElementFactory.make`` returns ``None`` (and emits a `g_warning`
    nobody reads) when the plugin is missing. Wrapping it lets us turn that
    silent failure into a `RuntimeError` with a hint about which apt package
    to install.
    """
    elem = Gst.ElementFactory.make(factory, name)
    if elem is None:
        hint = _PLUGIN_PACKAGE_HINT.get(factory)
        suffix = f" (try installing {hint})" if hint else ""
        raise RuntimeError(
            f"GStreamer element {factory!r} is not available — the plugin "
            f"providing it is missing or failed to load{suffix}."
        )
    return elem


def channel_descriptor(n: int) -> str:
    """Render a channel count as 'mono', 'stereo', or 'N-channel'."""
    return {1: 'mono', 2: 'stereo'}.get(n, f'{n}-channel')


def struct_get(struct, key: str, default=None):
    """Read a field from a Gst.Structure-like object across PyGObject versions.

    PyGObject 3.50+ wraps ``Gst.Structure`` as a ``StructureWrapper`` that
    only exposes dict-style access — the typed getters (``get_int``,
    ``get_string``, ``get_boolean``) return tuples and were dropped from the
    wrapper. Older PyGObject only has the typed getters and ``get_value``.
    This helper papers over both: dict access first (also covers plain dicts
    that the unit tests pass in), then ``get_value``, then the typed getters
    so existing test fakes keep working.
    """
    if struct is None:
        return default
    try:
        if key in struct:
            return struct[key]
    except (TypeError, AttributeError):
        pass
    getter = getattr(struct, 'get_value', None)
    if callable(getter):
        try:
            value = getter(key)
        except Exception:
            value = None
        if value is not None:
            return value
    for typed in ('get_string', 'get_int', 'get_boolean'):
        fn = getattr(struct, typed, None)
        if not callable(fn):
            continue
        try:
            result = fn(key)
        except Exception:
            continue
        if isinstance(result, tuple) and len(result) == 2:
            ok, value = result
            if ok:
                return value
        elif result is not None:
            return result
    return default


def channels_from_pad(pad) -> tuple[str | None, int | None]:
    """
    Read negotiated caps from a pad and return ``(caps_str, n_channels)``.

    Returns ``(None, None)`` if the pad has no current caps yet (e.g. the
    pipeline hasn't reached PAUSED/PLAYING). ``n_channels`` is ``None`` when
    caps are present but the structure has no `channels` field.
    """
    if pad is None:
        return None, None
    caps = pad.get_current_caps()
    if caps is None or caps.get_size() == 0:
        return None, None
    struct = caps.get_structure(0)
    n = struct_get(struct, 'channels')
    return caps.to_string(), (int(n) if isinstance(n, int) else None)


def format_peaks(peaks) -> str:
    """
    Render a level-element ``peak`` array for debug logging. Handles 1
    (Level), 2 (Levels: L … R …), and N>2 (Levels: 0:… 1:… …) channel cases.
    """
    if len(peaks) == 1:
        return f'{peaks[0]:.2f}'
    if len(peaks) == 2:
        return f'L {peaks[0]:.2f} R {peaks[1]:.2f}'
    return ' '.join(f'{i}:{p:.2f}' for i, p in enumerate(peaks))
