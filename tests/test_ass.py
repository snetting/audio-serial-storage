import os
import tempfile
import unittest
import wave

import numpy as np

import ass


def read_wav(path):
    with wave.open(path, "r") as wf:
        frames = wf.readframes(wf.getnframes())
        return np.frombuffer(frames, dtype=np.int16), wf.getframerate()


def resample(signal, factor):
    old_x = np.arange(len(signal))
    new_len = int(round(len(signal) * factor))
    new_x = np.linspace(0, len(signal) - 1, new_len)
    return np.interp(new_x, old_x, signal.astype(np.float64)).astype(np.int16)


def sinusoidal_speed_warp(signal, sample_rate, depth=0.002, rate=1.2):
    t = np.arange(len(signal)) / sample_rate
    speed = 1.0 + depth * np.sin(2 * np.pi * rate * t)
    positions = np.cumsum(speed)
    positions -= positions[0]
    positions *= (len(signal) - 1) / positions[-1]
    return np.interp(positions, np.arange(len(signal)), signal.astype(np.float64)).astype(np.int16)


class AssRoundTripTests(unittest.TestCase):
    def test_packet_preserves_source_file_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            source = os.path.join(td, "payload.bin")
            with open(source, "wb") as fh:
                fh.write(b"payload")
            os.chmod(source, 0o640)

            packet = ass.build_packet("payload.bin", b"payload", 0, source_path=source)
            parsed = ass.parse_packet(packet)

            self.assertEqual(parsed[0], "payload.bin")
            self.assertEqual(parsed[1], b"payload")
            self.assertEqual(parsed[8], 0o640)

    def test_interleave_round_trip(self):
        data = bytes(range(251))
        for depth in (1, 2, 4, 8, 16):
            with self.subTest(depth=depth):
                self.assertEqual(ass.deinterleave_bytes(ass.interleave_bytes(data, depth), depth), data)

    def test_default_audio_auto_detect_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            wav_path = os.path.join(td, "out.wav")
            packet = ass.build_packet("inline_data.txt", b"hello from ass", 0)
            ass.encode(packet, wav_path, bitrate=1200, mfsk=2)
            signal, sample_rate = read_wav(wav_path)

            result = ass.decode_signal(signal, bitrate=None, mfsk=None, sample_rate=sample_rate,
                                       interleave_depth=1, auto=True, write_output=False)

            self.assertIsNotNone(result)
            self.assertEqual(result.filename, "inline_data.txt")
            self.assertEqual(result.payload, b"hello from ass")
            self.assertEqual(result.bitrate, 1200)
            self.assertEqual(result.mfsk, 2)

    def test_decode_handles_small_constant_speed_error(self):
        with tempfile.TemporaryDirectory() as td:
            wav_path = os.path.join(td, "out.wav")
            packet = ass.build_packet("inline_data.txt", b"timing drift test", 0)
            ass.encode(packet, wav_path, bitrate=600, mfsk=2)
            signal, sample_rate = read_wav(wav_path)
            slowed = resample(signal, 1.015)

            result = ass.decode_signal(slowed, bitrate=600, mfsk=2, sample_rate=sample_rate,
                                       interleave_depth=1, auto=False, write_output=False)

            self.assertIsNotNone(result)
            self.assertEqual(result.payload, b"timing drift test")

    def test_decode_handles_modest_wow_flutter(self):
        with tempfile.TemporaryDirectory() as td:
            wav_path = os.path.join(td, "out.wav")
            packet = ass.build_packet("inline_data.txt", b"wow flutter test", 0)
            ass.encode(packet, wav_path, bitrate=600, mfsk=2)
            signal, sample_rate = read_wav(wav_path)
            warped = sinusoidal_speed_warp(signal, sample_rate)

            result = ass.decode_signal(warped, bitrate=600, mfsk=2, sample_rate=sample_rate,
                                       interleave_depth=1, auto=False, write_output=False)

            self.assertIsNotNone(result)
            self.assertEqual(result.payload, b"wow flutter test")


if __name__ == "__main__":
    unittest.main()
