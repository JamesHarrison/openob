from __future__ import annotations

import argparse
import logging

from openob.audio_interface import AudioInterface
from openob.link_config import LinkConfig
from openob.logger import LoggerFactory
from openob.node import Node


class _HelpAction(argparse._HelpAction):

    def __call__(self, parser, namespace, values, option_string=None):
        parser.print_help()

        subparsers_actions = [
            action for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        for subparsers_action in subparsers_actions:
            for choice, subparser in subparsers_action.choices.items():
                print("Subparser '{}'".format(choice))
                print(subparser.format_help())

        parser.exit()


_OPUS_BITRATES = [
    16, 24, 32, 48, 64, 96, 128, 192, 256, 384,
    # qext / high-rate territory (Opus quality extensions, libopus 1.3+)
    512, 768, 1024, 1536, 2048,
]
_OPUS_FRAMESIZES = [2, 5, 10, 20, 40, 60]
_TEST_WAVES = [
    'sine', 'square', 'saw', 'triangle', 'silence',
    'white-noise', 'pink-noise', 'sine-table', 'ticks',
    'gaussian-noise', 'red-noise', 'blue-noise', 'violet-noise',
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='openob',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        add_help=False,
    )

    parser.add_argument('-v', '--verbose', action='store_const',
                        help='Increase logging verbosity',
                        const=logging.DEBUG, default=logging.INFO)
    parser.add_argument('-h', '--help', action=_HelpAction, help='Show help')

    parser.add_argument('config_host', type=str,
                        help="The configuration server for this OpenOB Node")
    parser.add_argument('node_name', type=str,
                        help="The node name for this end")
    parser.add_argument('link_name', type=str,
                        help="The link name this OpenOB Manager is operating on; "
                             "must be the same on both Nodes")

    subparsers = parser.add_subparsers(help="The link mode to operate in on this end")

    parser_tx = subparsers.add_parser(
        'tx', formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser_tx.add_argument('receiver_host', type=str,
                           help="The receiver for this transmitter. The machine at "
                                "this address must be running an rx-mode Manager "
                                "for this link name")
    parser_tx.add_argument('-a', '--audio_input', type=str,
                           choices=['auto', 'alsa', 'jack', 'test'],
                           default='auto',
                           help="The audio source type for this end of the link")
    parser_tx_alsa = parser_tx.add_argument_group('alsa', 'Options when using ALSA source type')
    parser_tx_alsa.add_argument('-d', '--alsa_device', type=str, default='hw:0',
                                help="The ALSA device to connect to for input")
    parser_tx_jack = parser_tx.add_argument_group('jack', 'Options when using JACK source type')
    parser_tx_jack.add_argument('-jn', '--jack_name', type=str, default='openob',
                                help="JACK port name root")
    parser_tx_jack.add_argument('-aj', '--jack_auto', action='store_false',
                                help="Disable auto connection for JACK inputs")
    parser_tx_jack.add_argument('-jp', '--jack_port_pattern', type=str, default=None,
                                help="JACK port pattern")
    parser_tx_test = parser_tx.add_argument_group('test', 'Options when using test source type')
    parser_tx_test.add_argument('--test_wave', type=str, choices=_TEST_WAVES,
                                default='sine',
                                help="audiotestsrc waveform to generate")
    parser_tx.add_argument('-r', '--samplerate', type=int, default=0,
                           help="Set the sample rate to request from the input (Hz). "
                                "0 = let the source/audioconvert negotiate.")
    parser_tx.add_argument('-c', '--channels', type=int, default=0,
                           help="Pin the input channel count via the capsfilter "
                                "(typical: 1 for mono, 2 for stereo). 0 = let the "
                                "source/audioconvert negotiate; useful when an ALSA "
                                "card defaults to mono and you want to force stereo.")
    parser_tx.add_argument('-e', '--encoding', type=str, choices=['pcm', 'opus'],
                           default='opus',
                           help="The audio encoding type for this link; PCM for "
                                "linear audio (16-bit), or Opus for encoded audio")
    parser_tx.add_argument('-p', '--port', type=int, default=3000,
                           help="The base port to use for audio transport. This "
                                "port must be accessible on the receiving host")
    parser_tx.add_argument('-m', '--multicast', action='store_true', dest='multicast',
                           help="Start this transmitter in multicast mode, enabling "
                                "multiple clients to connect at once using the "
                                "address specified in reciever_host")
    parser_tx.add_argument('--no-multicast', action='store_false', dest='multicast',
                           help="Start this transmitter in unicast mode (default)")
    parser_tx.add_argument('-j', '--jitter_buffer', type=int, default=40,
                           help="The size of the jitter buffer in milliseconds. "
                                "Affects latency; may be reduced to 5-10ms on fast "
                                "reliable networks, or increased for poor networks "
                                "like 3G")
    parser_tx_opus = parser_tx.add_argument_group('opus', 'Opus encoder options')
    parser_tx_opus.add_argument('-b', '--bitrate', type=int, default=128,
                                help="Bitrate if using CELT/Opus (in kbit/s)",
                                choices=_OPUS_BITRATES)
    parser_tx_opus.add_argument('-l', '--loss', type=int, default=0,
                                help="Expected packet loss percentage for Opus, "
                                     "between 0 and 100",
                                choices=range(0, 100), metavar='LOSS')
    parser_tx_opus.add_argument('--dtx', action='store_true', dest='dtx',
                                help="Enable Opus Discontinuous Transmission support")
    parser_tx_opus.add_argument('--no-dtx', action='store_false', dest='dtx',
                                help="Disable Opus Discontinuous Transmission support "
                                     "(default)")
    parser_tx_opus.add_argument('--qext', action='store_true', dest='qext',
                                help="Enable Opus Quality Extensions (qext) for "
                                     "high-bitrate music. Designed for >=1024 kbps; "
                                     "requires a GStreamer/libopus build that exposes "
                                     "the 'enable-qext' property on opusenc (libopus "
                                     ">=1.3 era).")
    parser_tx_opus.add_argument('--no-qext', action='store_false', dest='qext',
                                help="Disable Opus Quality Extensions (default)")
    parser_tx_opus.add_argument('--fec', action='store_true', dest='fec',
                                help="Enable Opus Inband Forward Error Correction "
                                     "support (default)")
    parser_tx_opus.add_argument('--no-fec', action='store_false', dest='fec',
                                help="Disable Opus Inband Forward Error Correction "
                                     "support")
    parser_tx_opus.add_argument('--complexity', type=int, default=9,
                                help="Opus Computational Complexity, between 0 and "
                                     "10 - reduce on CPU-constrained devices",
                                choices=range(0, 10))
    parser_tx_opus.add_argument('--framesize', type=int, default=20,
                                help="Opus frame size (ms)",
                                choices=_OPUS_FRAMESIZES)
    parser_tx.set_defaults(mode='tx', fec=True, dtx=False, qext=False, multicast=False)

    parser_rx = subparsers.add_parser(
        'rx', formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser_rx.add_argument('-a', '--audio_output', type=str,
                           choices=['auto', 'alsa', 'jack', 'test', 'wav'],
                           default='auto',
                           help="The audio output type for this end of the link")
    parser_rx_alsa = parser_rx.add_argument_group('alsa', 'Options when using ALSA output type')
    parser_rx_alsa.add_argument('-d', '--alsa_device', type=str, default='hw:0',
                                help="The ALSA device to connect to for input")
    parser_rx_jack = parser_rx.add_argument_group('jack', 'Options when using JACK output type')
    parser_rx_jack.add_argument('-jn', '--jack_name', type=str, default='openob',
                                help="JACK port name root")
    parser_rx_jack.add_argument('-aj', '--jack_auto', action='store_false',
                                help="Disable auto connection for JACK inputs")
    parser_rx_jack.add_argument('-jp', '--jack_port_pattern', type=str, default=None,
                                help="JACK port pattern")
    parser_rx_wav = parser_rx.add_argument_group('wav', 'Options when using WAV file output type')
    parser_rx_wav.add_argument('--wav_file', type=str, default=None,
                               help="Path to write the captured WAV file")

    parser_rx.set_defaults(mode='rx')

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    opts = parser.parse_args(argv)

    if not hasattr(opts, 'mode'):
        parser.print_help()
        parser.exit(2)

    if opts.mode == 'rx' and opts.audio_output == 'wav' and not opts.wav_file:
        parser.error("--wav_file is required when --audio_output=wav")

    LoggerFactory(level=opts.verbose)
    logger = LoggerFactory().getLogger("cli")

    try:
        from importlib.metadata import version
        ob_version = version("OpenOB")
    except Exception:
        ob_version = "unknown"

    if opts.mode == "tx":
        logger.info(
            "OpenOB %s starting: node=%s link=%s mode=tx config=%s receiver=%s "
            "encoding=%s bitrate=%dkbps audio_input=%s",
            ob_version, opts.node_name, opts.link_name, opts.config_host,
            opts.receiver_host, opts.encoding, opts.bitrate, opts.audio_input,
        )
    else:
        logger.info(
            "OpenOB %s starting: node=%s link=%s mode=rx config=%s audio_output=%s",
            ob_version, opts.node_name, opts.link_name, opts.config_host,
            opts.audio_output,
        )

    link_config = LinkConfig(opts.link_name, opts.config_host)
    link_config.set_from_argparse(opts)

    audio_interface = AudioInterface.from_argparse(opts)

    node = Node(opts.node_name)
    node.run_link(link_config, audio_interface)


if __name__ == "__main__":
    main()
