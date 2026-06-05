#!/usr/bin/env python3
import argparse
import datetime
import lzma
import math
import os
import sys
import time
import wave
import zlib
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
from reedsolo import RSCodec, ReedSolomonError

SAMPLE_RATE = 44100
DEFAULT_BITRATE = 1200
DEFAULT_MFSK = 2

FREQ_0 = 1200
FREQ_1 = 2400

PREAMBLE_BITS = [1, 0] * 32
SYNC_WORD = 0xAA55AA55
END_MARKER_WORD = 0x55AA55AA
RESYNC_WORD64 = 0xD3C5BA7E1F2E9ACD

RS_NSYM = 32
RS_K = 255 - RS_NSYM
RS_CODEC = RSCodec(RS_NSYM)

LZMA_PRESET = 9 | lzma.PRESET_EXTREME
COMMON_BITRATES = [1200, 600, 300, 2400, 100, 80]
COMMON_MFSK = [2, 4, 8, 16]


@dataclass
class DecodeResult:
    filename: str
    payload: bytes
    crc: int
    backup_ts: int
    ctime: int
    mtime: int
    uid: int
    gid: int
    mode: int
    flags: int
    bitrate: int
    mfsk: int
    interleave_depth: int
    resync_markers: int
    demodulator: str
    rs_stats: dict
    score: float


ALLOW_BRUTE_FORCE_RECOVERY = False
ALLOW_OVERWRITE = False


def int_to_bits(value: int, nbits: int) -> list[int]:
    return [(value >> (nbits - 1 - i)) & 1 for i in range(nbits)]


def bits_to_int(bits: list[int]) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def get_mfsk_freqs(num_tones: int) -> list[float]:
    if num_tones == 2:
        return [FREQ_0, FREQ_1]
    span = FREQ_1 - FREQ_0
    return [FREQ_0 + i * span / (num_tones - 1) for i in range(num_tones)]


def bits_to_symbols(bits: list[int], mfsk: int) -> list[int]:
    bits_per_symbol = int(math.log2(mfsk))
    rem = len(bits) % bits_per_symbol
    if rem:
        bits = bits + [0] * (bits_per_symbol - rem)
    symbols = []
    for i in range(0, len(bits), bits_per_symbol):
        symbols.append(bits_to_int(bits[i:i + bits_per_symbol]))
    return symbols


def symbols_to_bits(symbols: list[int], mfsk: int) -> list[int]:
    bits_per_symbol = int(math.log2(mfsk))
    bits = []
    for sym in symbols:
        for i in reversed(range(bits_per_symbol)):
            bits.append((sym >> i) & 1)
    return bits


def bytes_to_bits(data: bytes) -> list[int]:
    bits = []
    for byte in data:
        for i in range(8):
            bits.append((byte >> (7 - i)) & 1)
    return bits


def bits_to_bytes(bits: list[int]) -> bytes:
    out = bytearray()
    usable = len(bits) - (len(bits) % 8)
    for i in range(0, usable, 8):
        out.append(bits_to_int(bits[i:i + 8]))
    return bytes(out)


def find_pattern(bits: list[int], pattern: list[int]) -> int | None:
    plen = len(pattern)
    for i in range(0, len(bits) - plen + 1):
        if bits[i:i + plen] == pattern:
            return i
    return None


def invert_bits(bits: list[int]) -> list[int]:
    return [bit ^ 1 for bit in bits]


def rs_encode_blocks(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), RS_K):
        out.extend(RS_CODEC.encode(data[i:i + RS_K]))
    return bytes(out)


def rs_decode_blocks(data: bytes) -> tuple[bytes, dict]:
    out = bytearray()
    total_blocks = 0
    symbols_corrected = 0
    uncorrectable_blocks = 0
    cw_len = RS_K + RS_NSYM

    for i in range(0, len(data), cw_len):
        cw = data[i:i + cw_len]
        if not cw:
            continue
        total_blocks += 1
        try:
            result = RS_CODEC.decode(cw)
            if isinstance(result, tuple):
                decoded = result[0]
                errata = result[-1] if len(result) >= 3 else []
                if isinstance(errata, (bytes, bytearray, list, tuple)):
                    symbols_corrected += len(errata)
            else:
                decoded = result
            out.extend(decoded)
        except ReedSolomonError:
            uncorrectable_blocks += 1
            out.extend(cw[:RS_K])

    return bytes(out), {
        "total_blocks": total_blocks,
        "symbols_corrected": symbols_corrected,
        "uncorrectable_blocks": uncorrectable_blocks,
    }


def interleave_bytes(data: bytes, depth: int) -> bytes:
    if depth <= 1:
        return data
    return b"".join(data[offset::depth] for offset in range(depth))


def deinterleave_bytes(data: bytes, depth: int) -> bytes:
    if depth <= 1:
        return data
    out = bytearray(len(data))
    idx = 0
    for offset in range(depth):
        count = max(0, (len(data) - 1 - offset) // depth + 1)
        out[offset::depth] = data[idx:idx + count]
        idx += count
    return bytes(out)


def build_packet(filename: str, data: bytes, flags: int, source_path: str | None = None) -> bytes:
    filename_bytes = filename.encode("utf-8")
    if len(filename_bytes) > 255:
        raise ValueError("Encoded filename must fit in 255 bytes")

    stat_path = source_path if source_path else filename
    if source_path and os.path.exists(stat_path):
        st = os.stat(stat_path)
        backup_ts = int(time.time())
        ctime = int(st.st_ctime)
        mtime = int(st.st_mtime)
        uid = st.st_uid
        gid = st.st_gid
        mode = st.st_mode & 0o777
    else:
        backup_ts = ctime = mtime = uid = gid = mode = 0

    crc = zlib.crc32(data) & 0xFFFFFFFF
    header = (
        b"ASS1"
        + bytes([len(filename_bytes)])
        + filename_bytes
        + backup_ts.to_bytes(4, "big")
        + ctime.to_bytes(4, "big")
        + mtime.to_bytes(4, "big")
        + uid.to_bytes(4, "big")
        + gid.to_bytes(4, "big")
        + mode.to_bytes(2, "big")
        + bytes([flags])
        + len(data).to_bytes(4, "big")
        + crc.to_bytes(4, "big")
    )
    return header + data


def parse_packet(packet: bytes) -> tuple:
    if len(packet) < 36 or packet[:4] != b"ASS1":
        raise ValueError("Invalid packet format or missing ASS1 magic")

    idx = 4
    name_len = packet[idx]
    idx += 1
    min_len = 4 + 1 + name_len + 31
    if len(packet) < min_len:
        raise ValueError("Packet too short for header")

    filename = packet[idx:idx + name_len].decode("utf-8")
    idx += name_len
    backup_ts = int.from_bytes(packet[idx:idx + 4], "big"); idx += 4
    ctime = int.from_bytes(packet[idx:idx + 4], "big"); idx += 4
    mtime = int.from_bytes(packet[idx:idx + 4], "big"); idx += 4
    uid = int.from_bytes(packet[idx:idx + 4], "big"); idx += 4
    gid = int.from_bytes(packet[idx:idx + 4], "big"); idx += 4
    mode = int.from_bytes(packet[idx:idx + 2], "big"); idx += 2
    flags = packet[idx]; idx += 1
    data_len = int.from_bytes(packet[idx:idx + 4], "big"); idx += 4
    crc = int.from_bytes(packet[idx:idx + 4], "big"); idx += 4
    if data_len > len(packet) - idx:
        raise ValueError("Payload length exceeds decoded packet")
    payload = packet[idx:idx + data_len]
    return filename, payload, crc, backup_ts, ctime, mtime, uid, gid, mode, flags


def generate_tone(frequency: float, duration: float, sample_rate: int) -> np.ndarray:
    count = max(1, int(sample_rate * duration))
    t = np.arange(count) / float(sample_rate)
    return (32767 * np.sin(2 * np.pi * frequency * t)).astype(np.int16)


def encode(data: bytes, outfile: str, bitrate: int, mfsk: int = 2,
           interleave_depth: int = 1, resync_interval: int = 0) -> None:
    print(f"[ENCODE] Packet size: {len(data)} bytes")
    data_rs = rs_encode_blocks(data)
    print(f"[ENCODE] RS-encode: {len(data)} -> {len(data_rs)} bytes "
          f"(parity +{len(data_rs) - len(data)} bytes)")

    if interleave_depth > 1:
        data_rs = interleave_bytes(data_rs, interleave_depth)
        print(f"[ENCODE] Interleaving: depth {interleave_depth} (not backward compatible)")
    if resync_interval > 0:
        print(f"[ENCODE] Resync markers: every {resync_interval} encoded bytes "
              "(not backward compatible)")

    bits_per_symbol = int(math.log2(mfsk))
    bits = PREAMBLE_BITS.copy() + int_to_bits(SYNC_WORD, 32)
    resync_bits = int_to_bits(RESYNC_WORD64, 64)

    for idx, byte in enumerate(data_rs, start=1):
        bits.extend(bytes_to_bits(bytes([byte])))
        if resync_interval > 0 and idx % resync_interval == 0:
            bits.extend(resync_bits)

    bits.extend(int_to_bits(END_MARKER_WORD, 32) * 3)
    bits.extend([0] * 32)
    rem = len(bits) % bits_per_symbol
    if rem:
        bits.extend([0] * (bits_per_symbol - rem))

    freqs = get_mfsk_freqs(mfsk)
    sym_duration = 1.0 / bitrate
    signal = np.concatenate([
        generate_tone(freqs[sym], sym_duration, SAMPLE_RATE)
        for sym in bits_to_symbols(bits, mfsk)
    ]).astype(np.int16)

    if outfile == "-":
        print(f"[ENCODE] Playing live with {mfsk}-FSK...")
        sd.play(signal / 32767.0, SAMPLE_RATE)
        sd.wait()
    else:
        with wave.open(outfile, "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(signal.tobytes())
        duration = len(signal) / SAMPLE_RATE
        print(f"[ENCODE] Written WAV to {outfile} "
              f"({duration:.2f}s @ {bitrate} symbols/s, {mfsk}-FSK)")


def goertzel_power(chunk: np.ndarray, freq: float, sample_rate: int) -> float:
    x = chunk.astype(np.float64)
    if len(x) > 1:
        x *= np.hanning(len(x))
    k = int(0.5 + (len(x) * freq) / sample_rate)
    omega = (2.0 * np.pi / len(x)) * k
    coeff = 2.0 * np.cos(omega)
    s_prev = 0.0
    s_prev2 = 0.0
    for sample in x:
        s = sample + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    return s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2


def symbol_at(signal: np.ndarray, center: float, sps: float, sample_rate: int, mfsk: int) -> tuple[int, list[float]]:
    half = max(4, int(round(sps * 0.45)))
    c = int(round(center))
    start = max(0, c - half)
    end = min(len(signal), c + half)
    chunk = signal[start:end]
    if len(chunk) < 2 * half:
        chunk = np.pad(chunk, (0, 2 * half - len(chunk)), "constant")
    powers = [goertzel_power(chunk, freq, sample_rate) for freq in get_mfsk_freqs(mfsk)]
    return int(np.argmax(powers)), powers


def demodulate_symbols(signal: np.ndarray, sample_rate: int, mfsk: int,
                       sps: float, phase: float, pll: bool) -> list[int]:
    symbols = []
    center = float(phase)
    sps_current = float(sps)
    integral = 0.0
    freqs = get_mfsk_freqs(mfsk)

    while center + max(4, sps_current * 0.5) < len(signal):
        sym, powers = symbol_at(signal, center, sps_current, sample_rate, mfsk)
        symbols.append(sym)

        if pll:
            offset = max(1.0, sps_current * 0.12)
            half = max(4, int(round(sps_current * 0.35)))
            c_early = int(round(center - offset))
            c_late = int(round(center + offset))
            early = signal[max(0, c_early - half):min(len(signal), c_early + half)]
            late = signal[max(0, c_late - half):min(len(signal), c_late + half)]
            if len(early) > 4 and len(late) > 4:
                p_early = goertzel_power(early, freqs[sym], sample_rate)
                p_late = goertzel_power(late, freqs[sym], sample_rate)
                err = (p_late - p_early) / (powers[sym] + 1e-9)
                err = max(-0.5, min(0.5, err))
                if abs(err) < 0.02:
                    err = 0.0
                integral = max(-0.02, min(0.02, integral + 0.0002 * err))
                sps_current += 0.02 * err + integral
                sps_current = max(sps * 0.94, min(sps * 1.06, sps_current))

        center += sps_current

    return symbols


def demodulate_symbols_fast(signal: np.ndarray, sample_rate: int, mfsk: int,
                            sps: float, phase: float) -> list[int]:
    """Fast fixed-clock detector for candidate validation on long files."""
    freqs = get_mfsk_freqs(mfsk)
    half = max(4, int(round(sps * 0.45)))
    win_len = 2 * half
    window = np.hanning(win_len)
    refs = []
    t = np.arange(win_len) / float(sample_rate)
    for freq in freqs:
        refs.append((
            np.sin(2 * np.pi * freq * t) * window,
            np.cos(2 * np.pi * freq * t) * window,
        ))

    symbols = []
    center = float(phase)
    while center + half < len(signal):
        c = int(round(center))
        start = c - half
        end = c + half
        if start < 0:
            chunk = np.pad(signal[0:end], (-start, 0), "constant")
        elif end > len(signal):
            chunk = np.pad(signal[start:len(signal)], (0, end - len(signal)), "constant")
        else:
            chunk = signal[start:end]
        x = chunk.astype(np.float64)
        powers = []
        for sin_ref, cos_ref in refs:
            i_val = float(np.dot(x, sin_ref))
            q_val = float(np.dot(x, cos_ref))
            powers.append(i_val * i_val + q_val * q_val)
        symbols.append(int(np.argmax(powers)))
        center += sps
    return symbols


def score_lock(signal: np.ndarray, sample_rate: int, bitrate: int, mfsk: int,
               rel_span: float = 0.08) -> tuple[float, float, float]:
    target_bits = PREAMBLE_BITS + int_to_bits(SYNC_WORD, 32)
    target_symbols = bits_to_symbols(target_bits, mfsk)
    nominal_sps = sample_rate / float(bitrate)
    best_score = -1.0
    best_sps = nominal_sps
    best_phase = nominal_sps / 2.0
    head = signal[:min(len(signal), int(sample_rate * 8))]

    for sps in np.linspace(nominal_sps * (1 - rel_span), nominal_sps * (1 + rel_span), 25):
        phase_trials = max(8, min(64, int(round(sps))))
        for phase in np.linspace(sps * 0.25, sps * 1.25, phase_trials):
            got = []
            center = phase
            for _ in target_symbols:
                if center + sps * 0.5 >= len(head):
                    break
                sym, _ = symbol_at(head, center, sps, sample_rate, mfsk)
                got.append(sym)
                center += sps
            if len(got) < len(target_symbols) // 2:
                continue
            score = sum(1 for a, b in zip(got, target_symbols) if a == b) / len(target_symbols)
            if score > best_score:
                best_score = score
                best_sps = float(sps)
                best_phase = float(phase)
    return best_score, best_sps, best_phase


def estimate_tone_speed_factor(signal: np.ndarray, sample_rate: int) -> float | None:
    n = min(len(signal), int(sample_rate * 2.0))
    if n < 4096:
        return None
    x = signal[:n].astype(np.float64) * np.hanning(n)
    spectrum = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    ratios = []
    for nominal in (FREQ_0, FREQ_1):
        lo = np.searchsorted(freqs, nominal * 0.90)
        hi = np.searchsorted(freqs, nominal * 1.10)
        if hi <= lo:
            continue
        band = spectrum[lo:hi]
        peak = lo + int(np.argmax(band))
        floor = float(np.median(band)) + 1e-9
        if float(spectrum[peak]) / floor < 4.0:
            continue
        ratios.append(float(freqs[peak]) / nominal)
    if len(ratios) != 2:
        return None
    if abs(ratios[0] - ratios[1]) > 0.02:
        return None
    return sum(ratios) / len(ratios)


def lock_grids(signal: np.ndarray, sample_rate: int, bitrate: int, mfsk: int) -> list[tuple[float, list[float]]]:
    lock_score, lock_sps, lock_phase = score_lock(signal, sample_rate, bitrate, mfsk)
    if lock_score < 0.70:
        return []

    nominal_sps = sample_rate / float(bitrate)
    centers = []
    speed_factor = estimate_tone_speed_factor(signal, sample_rate)
    if speed_factor:
        centers.append(nominal_sps / speed_factor)
    centers.extend([lock_sps, nominal_sps])

    grids = []
    seen = set()
    for center in centers:
        for sps in np.linspace(center * 0.992, center * 1.008, 5):
            key = round(float(sps), 3)
            if key in seen:
                continue
            seen.add(key)
            phases = [lock_phase]
            phases.extend(float(p) for p in np.linspace(sps * 0.35, sps * 1.15, 8))
            grids.append((float(sps), phases))
    return grids


def strip_resync_markers(bits: list[int]) -> tuple[list[int], int]:
    pattern = int_to_bits(RESYNC_WORD64, 64)
    out = []
    i = 0
    removed = 0
    while i <= len(bits) - len(pattern):
        if bits[i:i + len(pattern)] == pattern:
            removed += 1
            i += len(pattern)
        else:
            out.append(bits[i])
            i += 1
    out.extend(bits[i:])
    return out, removed


def trim_active_region(signal: np.ndarray, sample_rate: int) -> np.ndarray:
    if len(signal) < sample_rate // 10:
        return signal
    win = max(8, int(sample_rate * 0.05))
    x = signal.astype(np.float64)
    rms = np.sqrt(np.convolve(x * x, np.ones(win) / win, mode="same") + 1e-12)
    active = np.where(rms > 32767 * 0.01)[0]
    if active.size == 0:
        return signal
    pad = int(sample_rate * 0.2)
    start = max(0, int(active[0]) - pad)
    end = min(len(signal), int(active[-1]) + pad)
    if start == 0 and end == len(signal):
        return signal
    trimmed = signal[start:end].astype(np.int16)
    print(f"[DECODE] Trimmed silence: {len(signal) / sample_rate:.2f}s -> "
          f"{len(trimmed) / sample_rate:.2f}s")
    return trimmed


def decode_candidate(signal: np.ndarray, sample_rate: int, bitrate: int, mfsk: int,
                     interleave_depth: int, use_pll: bool) -> DecodeResult | None:
    demod_name = "pll" if use_pll else "fixed"
    best = None
    grids = lock_grids(signal, sample_rate, bitrate, mfsk)
    if use_pll:
        grids = grids[:3]
    for sps, phases in grids:
        for phase in phases:
            if use_pll:
                symbols = demodulate_symbols(signal, sample_rate, mfsk, sps, phase, pll=True)
            else:
                symbols = demodulate_symbols_fast(signal, sample_rate, mfsk, sps, phase)
            bits = symbols_to_bits(symbols, mfsk)
            sync_pos = find_pattern(bits, int_to_bits(SYNC_WORD, 32))
            if sync_pos is None:
                continue

            data_bits = bits[sync_pos + 32:]
            data_bits, removed = strip_resync_markers(data_bits)
            end_pattern = int_to_bits(END_MARKER_WORD, 32) * 3
            end_idx = find_pattern(data_bits, end_pattern)
            if end_idx is not None:
                data_bits = data_bits[:end_idx]

            raw_stream = bits_to_bytes(data_bits)
            decoded_packet = try_decode_raw_stream(raw_stream, interleave_depth, require_exact=end_idx is not None)
            if decoded_packet is None:
                continue

            raw_len, decoded, rs_stats = decoded_packet
            fname, payload, crc, backup_ts, ctime, mtime, uid, gid, mode, flags = parse_packet(decoded)
            score = (
                100
                + max(0, 20 - rs_stats["uncorrectable_blocks"] * 20)
                + (5 if end_idx is not None else 0)
                - max(0, len(raw_stream) - raw_len) * 0.001
            )
            result = DecodeResult(
                filename=fname,
                payload=payload,
                crc=crc,
                backup_ts=backup_ts,
                ctime=ctime,
                mtime=mtime,
                uid=uid,
                gid=gid,
                mode=mode,
                flags=flags,
                bitrate=bitrate,
                mfsk=mfsk,
                interleave_depth=interleave_depth,
                resync_markers=removed,
                demodulator=demod_name,
                rs_stats=rs_stats,
                score=score,
            )
            if best is None or result.score > best.score:
                best = result
            if result.score >= 120:
                return result

    return best


def possible_rs_lengths(raw_len: int) -> list[int]:
    lengths = []
    for n in range(raw_len, 0, -1):
        rem = n % (RS_K + RS_NSYM)
        if rem == 0 or rem >= RS_NSYM + 1:
            lengths.append(n)
    return lengths


def packet_total_length_from_header(decoded_prefix: bytes) -> int | None:
    if len(decoded_prefix) < 36 or decoded_prefix[:4] != b"ASS1":
        return None
    name_len = decoded_prefix[4]
    header_len = 36 + name_len
    if len(decoded_prefix) < header_len:
        return None
    payload_len = int.from_bytes(decoded_prefix[28 + name_len:32 + name_len], "big")
    return header_len + payload_len


def rs_encoded_length_for_decoded_length(decoded_len: int) -> int:
    full_blocks, tail = divmod(decoded_len, RS_K)
    encoded_len = full_blocks * (RS_K + RS_NSYM)
    if tail:
        encoded_len += tail + RS_NSYM
    return encoded_len


def try_decode_by_header(raw_stream: bytes, interleave_depth: int) -> tuple[int, bytes, dict] | None:
    if interleave_depth != 1:
        return None
    cw_len = RS_K + RS_NSYM
    max_probe = min(len(raw_stream), cw_len * 2)
    probe_len = cw_len
    while probe_len <= max_probe:
        decoded_prefix, stats = rs_decode_blocks(raw_stream[:probe_len])
        if stats["uncorrectable_blocks"]:
            return None
        packet_len = packet_total_length_from_header(decoded_prefix)
        if packet_len is not None:
            raw_len = rs_encoded_length_for_decoded_length(packet_len)
            if raw_len > len(raw_stream):
                return None
            decoded, final_stats = rs_decode_blocks(raw_stream[:raw_len])
            try:
                parsed = parse_packet(decoded)
            except Exception:
                return None
            payload = parsed[1]
            crc = parsed[2]
            if (zlib.crc32(payload) & 0xFFFFFFFF) == crc:
                return raw_len, decoded, final_stats
            return None
        probe_len += cw_len
    return None


def try_decode_raw_stream(raw_stream: bytes, interleave_depth: int,
                          require_exact: bool) -> tuple[int, bytes, dict] | None:
    by_header = try_decode_by_header(raw_stream, interleave_depth)
    if by_header:
        return by_header
    if not require_exact and len(raw_stream) > 2048 and not ALLOW_BRUTE_FORCE_RECOVERY:
        return None
    if require_exact:
        lengths = [len(raw_stream)]
    else:
        max_extra = 2 * (RS_K + RS_NSYM)
        min_len = max(1, len(raw_stream) - max_extra)
        lengths = [n for n in possible_rs_lengths(len(raw_stream)) if n >= min_len]
    for raw_len in lengths:
        raw = raw_stream[:raw_len]
        raw = deinterleave_bytes(raw, interleave_depth)
        decoded, rs_stats = rs_decode_blocks(raw)
        if rs_stats["uncorrectable_blocks"]:
            continue
        try:
            parsed = parse_packet(decoded)
        except Exception:
            continue
        payload = parsed[1]
        crc = parsed[2]
        if (zlib.crc32(payload) & 0xFFFFFFFF) == crc:
            return raw_len, decoded, rs_stats
    return None


def decode_signal(signal: np.ndarray, bitrate: int | None, mfsk: int | None,
                  sample_rate: int = SAMPLE_RATE, interleave_depth: int | None = 1,
                  auto: bool = False, write_output: bool = True,
                  brute_force_recovery: bool = False,
                  overwrite: bool = False) -> DecodeResult | None:
    global ALLOW_BRUTE_FORCE_RECOVERY
    global ALLOW_OVERWRITE
    old_brute_force = ALLOW_BRUTE_FORCE_RECOVERY
    old_overwrite = ALLOW_OVERWRITE
    ALLOW_BRUTE_FORCE_RECOVERY = brute_force_recovery
    ALLOW_OVERWRITE = overwrite
    signal = trim_active_region(signal, sample_rate)
    bitrates = [bitrate] if bitrate else COMMON_BITRATES
    mfsk_values = [mfsk] if mfsk else COMMON_MFSK
    depths = [interleave_depth] if interleave_depth else [1, 4, 8, 16]

    try:
        candidates = []
        total = sum(1 for br in bitrates for tones in mfsk_values if br <= max_reliable_bitrate(tones))
        idx = 0
        if brute_force_recovery:
            print("[DECODE] Brute-force recovery enabled; this can be slow on long recordings.")
        for br in bitrates:
            for tones in mfsk_values:
                max_br = max_reliable_bitrate(tones)
                if br > max_br:
                    continue
                idx += 1
                print(f"[DECODE] Candidate {idx}/{total}: {br} symbols/s, {tones}-FSK")
                for depth in depths:
                    for use_pll in (False, True):
                        result = decode_candidate(signal, sample_rate, br, tones, depth, use_pll)
                        if result:
                            candidates.append(result)
                            print(f"[DECODE] Candidate OK: {br} symbols/s, {tones}-FSK, "
                                  f"depth {depth}, {result.demodulator}")
                            if not auto or result.score >= 120:
                                return finish_decode(result, write_output)

        if not candidates:
            print("[DECODE] No valid decode candidate found.")
            return None

        best = max(candidates, key=lambda item: item.score)
        return finish_decode(best, write_output)
    finally:
        ALLOW_BRUTE_FORCE_RECOVERY = old_brute_force
        ALLOW_OVERWRITE = old_overwrite


def finish_decode(result: DecodeResult, write_output: bool) -> DecodeResult:
    payload = result.payload
    print(f"[DECODE] Selected: {result.bitrate} symbols/s, {result.mfsk}-FSK, "
          f"{result.demodulator}, interleave depth {result.interleave_depth}")
    print(f"[DECODE] RS blocks: {result.rs_stats['total_blocks']}, "
          f"symbols corrected: {result.rs_stats['symbols_corrected']}, "
          f"uncorrectable: {result.rs_stats['uncorrectable_blocks']}")
    print(f"[DECODE] Filename:         {result.filename}")
    print(f"[DECODE] Backup timestamp: {format_ts(result.backup_ts)}")
    print(f"[DECODE] Original ctime:   {format_ts(result.ctime)}")
    print(f"[DECODE] Original mtime:   {format_ts(result.mtime)}")
    print(f"[DECODE] UID: {result.uid}, GID: {result.gid}, Mode: {oct(result.mode)}, Flags: {result.flags}")
    print(f"[DECODE] CRC32: 0x{result.crc:08X}")
    if result.resync_markers:
        print(f"[DECODE] Stripped {result.resync_markers} resync marker(s)")

    if result.flags & 1:
        payload = lzma.decompress(payload)
        result.payload = payload
        print("[DECODE] Decompression successful.")

    if write_output:
        output_name = safe_output_filename(result.filename)
        with open(output_name, "wb") as fh:
            fh.write(payload)
        print(f"[DECODE] Wrote output file: {output_name}")
        try:
            if result.uid or result.gid:
                os.chown(output_name, result.uid, result.gid)
            if result.mode:
                os.chmod(output_name, result.mode)
            if result.mtime:
                os.utime(output_name, (result.mtime, result.mtime))
            print("[DECODE] Restored metadata where possible.")
        except PermissionError:
            print("[DECODE] Warning: insufficient privileges to restore owner/group.")
        except Exception as exc:
            print(f"[DECODE] Metadata restore failed: {exc}")
    return result


def safe_output_filename(filename: str) -> str:
    basename = os.path.basename(filename) or "decoded_output.bin"
    if ALLOW_OVERWRITE or not os.path.exists(basename):
        return basename
    root, ext = os.path.splitext(basename)
    for i in range(1, 10_000):
        candidate = f"{root}.{i}{ext}"
        if not os.path.exists(candidate):
            print(f"[DECODE] Output exists; writing {candidate} instead of overwriting {basename}")
            return candidate
    raise RuntimeError(f"Could not find a free output filename for {basename}")


def format_ts(value: int) -> str:
    if value <= 0:
        return "not recorded"
    return str(datetime.datetime.fromtimestamp(value))


def decode_wav(filename: str, bitrate: int | None, mfsk: int | None,
               interleave_depth: int | None, auto: bool,
               brute_force_recovery: bool = False,
               overwrite: bool = False) -> DecodeResult | None:
    with wave.open(filename, "r") as wf:
        sample_rate = wf.getframerate()
        channels = wf.getnchannels()
        frames = wf.readframes(wf.getnframes())
    signal = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        signal = signal.reshape(-1, channels)[:, 0]
    print(f"[DECODE] Loading WAV: {filename} @ {sample_rate}Hz")
    return decode_signal(signal, bitrate, mfsk, sample_rate, interleave_depth, auto,
                         brute_force_recovery=brute_force_recovery,
                         overwrite=overwrite)


def detect_symbols(signal: np.ndarray, sym_duration: float, sample_rate: int, mfsk: int = 2) -> list[int]:
    sps = sample_rate * sym_duration
    symbols = []
    center = sps / 2
    while center + sps / 2 < len(signal):
        sym, _ = symbol_at(signal, center, sps, sample_rate, mfsk)
        symbols.append(sym)
        center += sps
    return symbols


def detect_bits(signal: np.ndarray, bit_duration: float, sample_rate: int) -> list[int]:
    return symbols_to_bits(detect_symbols(signal, bit_duration, sample_rate, mfsk=2), mfsk=2)


def live_preview_bits(signal: np.ndarray, bitrate: int, mfsk: int, sample_rate: int) -> list[int]:
    # The current encoder emits int(sample_rate / bitrate) samples per symbol.
    # Using nominal fractional SPS here drifts and misses live sync/end markers.
    sps = max(8, int(sample_rate / float(bitrate)))
    symbols = demodulate_symbols_fast(signal, sample_rate, mfsk, sps, sps / 2.0)
    return symbols_to_bits(symbols, mfsk)


def marker_in_audio(signal: np.ndarray, marker_bits: list[int], bitrate: int,
                    mfsk: int, sample_rate: int) -> bool:
    if len(signal) < int(sample_rate * 0.20):
        return False
    nominal_sps = sample_rate / float(bitrate)
    sps_candidates = [max(8, int(nominal_sps)), nominal_sps, max(8, round(nominal_sps))]

    seen = set()
    for sps in sps_candidates:
        key = round(float(sps), 3)
        if key in seen:
            continue
        seen.add(key)
        phase_count = 4
        for phase in np.linspace(sps * 0.20, sps * 1.20, phase_count):
            bits = symbols_to_bits(demodulate_symbols_fast(signal, sample_rate, mfsk, sps, phase), mfsk)
            if find_pattern(bits, marker_bits) is not None:
                return True
    return False


def level_dbfs(signal: np.ndarray) -> float:
    if len(signal) == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(signal.astype(np.float64) ** 2)) + 1e-12)
    return max(-120.0, 20.0 * math.log10(rms / 32767.0))


def record_and_decode(bitrate: int | None, mfsk: int | None, interleave_depth: int | None,
                      auto: bool, brute_force_recovery: bool = False,
                      overwrite: bool = False) -> None:
    br = bitrate or DEFAULT_BITRATE
    tones = mfsk or DEFAULT_MFSK
    block = int(SAMPLE_RATE * max(0.25, 32 / br))
    rolling_seconds = 1.0
    rolling_chunk_count = max(1, int(math.ceil((SAMPLE_RATE * rolling_seconds) / block)))
    monitor_every_blocks = max(1, int(math.ceil(SAMPLE_RATE / block)))
    chunks = []
    line_buffer = []
    sync_bits = int_to_bits(SYNC_WORD, 32)
    end_bits = int_to_bits(END_MARKER_WORD, 32)
    recent = []
    sync_seen = False
    blocks_read = 0
    overflows = 0

    try:
        print("[DECODE] Listening... Ctrl+C to stop.")
        print(f"[DECODE] Live preview: {br} symbols/s, {tones}-FSK")
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1) as stream:
            while True:
                block_data, overflowed = stream.read(block)
                if overflowed:
                    overflows += 1
                sig = (block_data[:, 0] * 32767).astype(np.int16)
                chunks.append(sig)
                blocks_read += 1
                preview_bits = live_preview_bits(sig, br, tones, SAMPLE_RATE)
                for bit in preview_bits:
                    line_buffer.append("*" if bit else ".")
                    recent.append(bit)
                    recent = recent[-96:]
                check_markers = blocks_read % monitor_every_blocks == 0
                rolling = None
                if check_markers:
                    rolling = np.concatenate(chunks[-rolling_chunk_count:])[-int(SAMPLE_RATE * rolling_seconds):]
                    if not sync_seen and marker_in_audio(rolling, sync_bits, br, tones, SAMPLE_RATE):
                        sync_seen = True
                        sys.stdout.write("\n[DECODE] Live: sync detected.\n")
                        sys.stdout.flush()
                elapsed = sum(len(chunk) for chunk in chunks) / SAMPLE_RATE
                overflow_msg = f" overflows={overflows}" if overflows else ""
                sys.stdout.write(
                    "\r"
                    + "".join(line_buffer[-80:])
                    + f" | {elapsed:6.1f}s {level_dbfs(sig):6.1f} dBFS{overflow_msg}"
                )
                sys.stdout.flush()
                if check_markers and marker_in_audio(rolling, end_bits * 3, br, tones, SAMPLE_RATE):
                    print("\n[DECODE] End marker detected.")
                    break
    except KeyboardInterrupt:
        print("\n[DECODE] Stopped listening.")

    if chunks:
        full = np.concatenate(chunks)
        print(f"[DECODE] Captured {len(full) / SAMPLE_RATE:.2f}s, peak={np.max(np.abs(full))}, "
              f"rms={level_dbfs(full):.1f} dBFS")
        decode_signal(full, bitrate, mfsk, SAMPLE_RATE, interleave_depth, auto,
                      brute_force_recovery=brute_force_recovery,
                      overwrite=overwrite)


def max_reliable_bitrate(mfsk: int) -> int:
    return int((FREQ_1 - FREQ_0) / (mfsk - 1))


def apply_bitrate_clamp(bitrate: int, mfsk: int, noclamp: bool) -> int:
    max_br = max_reliable_bitrate(mfsk)
    if bitrate > max_br:
        print(f"[MAIN] Warning: {mfsk}-FSK at {bitrate} symbols/s gives "
              f"frequency resolution about {bitrate}Hz > tone spacing {max_br:.1f}Hz")
        if not noclamp:
            print(f"[MAIN]   -> clamping to {max_br} symbols/s")
            return max_br
        print(f"[MAIN]   -> WARNING: CLAMP BYPASSED (forced {bitrate} symbols/s)")
    return bitrate


def read_encode_input(args: argparse.Namespace) -> tuple[bytes, str, str | None]:
    if args.data is not None:
        return args.data.encode(), "inline_data.txt", None
    if args.inputfile:
        with open(args.inputfile, "rb") as fh:
            return fh.read(), os.path.basename(args.inputfile), args.inputfile
    return sys.stdin.buffer.read(), "stdin_input.bin", None


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ass.py",
        description="Audio Serial Storage: FSK/MFSK WAV encoder and decoder with RS ECC",
    )
    parser.add_argument("mode", choices=["encode", "decode"])
    parser.add_argument("file", help="Output WAV for encode; input WAV or '-' for decode")
    parser.add_argument("--data", help="Inline string to encode")
    parser.add_argument("--inputfile", help="Path to input file to encode")
    parser.add_argument("--bitrate", type=int, help=f"Symbol rate (default encode: {DEFAULT_BITRATE}; decode: auto)")
    parser.add_argument("--mfsk", type=int, choices=COMMON_MFSK, help=f"Tone count (default encode: {DEFAULT_MFSK}; decode: auto)")
    parser.add_argument("--noclamp", action="store_true", help="Warn but do not clamp unreliable MFSK symbol rates")
    parser.add_argument("--interleave-depth", type=int, default=1,
                        help="Optional byte interleaver depth. Values >1 are not backward compatible.")
    parser.add_argument("--auto-interleave", action="store_true",
                        help="Try common interleave depths while decoding.")
    parser.add_argument("--resync-interval", type=int, default=0,
                        help="Insert 64-bit resync markers every N encoded bytes. Not backward compatible.")
    parser.add_argument("--brute-force-recovery", action="store_true",
                        help="Try slow RS-prefix recovery when normal header-based recovery fails.")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite an existing decoded output file instead of adding a numeric suffix.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--alwayscompress", "--always-compress", action="store_true", dest="always_compress")
    group.add_argument("--nocompress", "--no-compress", action="store_true", dest="no_compress")
    group.add_argument("--autocompress", "--auto-compress", action="store_true", dest="auto_compress")
    parser.set_defaults(auto_compress=True)

    args = parser.parse_args()
    if args.interleave_depth < 1:
        parser.error("--interleave-depth must be >= 1")
    if args.resync_interval < 0:
        parser.error("--resync-interval must be >= 0")

    if args.mode == "encode":
        bitrate = apply_bitrate_clamp(args.bitrate or DEFAULT_BITRATE, args.mfsk or DEFAULT_MFSK, args.noclamp)
        mfsk = args.mfsk or DEFAULT_MFSK
        raw, name, source_path = read_encode_input(args)
        print(f"[ENCODE] Read data: {len(raw)} bytes from '{name}'")

        if args.always_compress:
            payload = lzma.compress(raw, preset=LZMA_PRESET)
            flags = 1
            print(f"[ENCODE] Compression: forced, result {len(payload)} bytes")
        elif args.no_compress:
            payload = raw
            flags = 0
            print(f"[ENCODE] Compression: disabled, sending {len(payload)} bytes")
        else:
            comp = lzma.compress(raw, preset=LZMA_PRESET)
            if len(comp) < len(raw):
                payload = comp
                flags = 1
                print(f"[ENCODE] Compression: applied, {len(comp)} bytes ({len(raw) - len(comp)} bytes saved)")
            else:
                payload = raw
                flags = 0
                print(f"[ENCODE] Compression: skipped (compressed size {len(comp)} >= raw size {len(raw)})")

        packet = build_packet(name, payload, flags, source_path=source_path)
        print(f"[ENCODE] Packet: {len(packet)} bytes (header+data+crc)")
        encode(packet, args.file, bitrate, mfsk, args.interleave_depth, args.resync_interval)
        return

    auto_modulation = args.bitrate is None and args.mfsk is None
    bitrate = None if auto_modulation else (args.bitrate or DEFAULT_BITRATE)
    mfsk = None if auto_modulation else (args.mfsk or DEFAULT_MFSK)
    if bitrate and mfsk:
        bitrate = apply_bitrate_clamp(bitrate, mfsk, args.noclamp)
    auto = auto_modulation or args.auto_interleave
    depth = None if args.auto_interleave else args.interleave_depth
    print("[MAIN] Entering decode mode" + (" (auto-detect)" if auto else ""))
    if args.file == "-":
        record_and_decode(bitrate, mfsk, depth, auto, args.brute_force_recovery, args.overwrite)
    else:
        decode_wav(args.file, bitrate, mfsk, depth, auto, args.brute_force_recovery, args.overwrite)


if __name__ == "__main__":
    main()
