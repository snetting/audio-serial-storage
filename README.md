ASS: Audio Serial Storage
=========================

**Save data to cassette** via FSK modulation, Reed--Solomon ECC, and LZMA compression.

A simple tape‑style encoder/decoder originally inspired by 8‑bit microcomputers and the now‑deprecated ctape project. Beyond cassette storage, ASS supports low‑bandwidth data links (e.g., amateur radio).

* * * * *

Features
--------

-   **FSK Modulation/Demodulation** at 1200 Hz / 2400 Hz tones

-   **LZMA Compression** (xz‑9e) with three modes: `--alwayscompress`, `--nocompress`, or `--autocompress` (default)

-   **Reed--Solomon (255, 223)** error correction (up to 16 byte‑errors per block)

-   **CRC32** payload integrity check

-   **Live bit‑stream display** in console

-   **Read/write from WAV file or live audio input**

-   **Preserve and restore file metadata:** timestamps, ownership, and permissions

-   **Experimental** MFSK 2,4,8,16 support

-   **Automatic decode detection** for common symbol rates and FSK/MFSK modes when `--bitrate` or `--mfsk` is omitted

-   **Improved sync and timing recovery** using preamble correlation, tone-speed estimation, adaptive PLL candidate demodulation, and RS/CRC validation

-   **Optional burst-error mitigation** via byte interleaving (`--interleave-depth`) and periodic resync markers (`--resync-interval`)
* * * * *

Serial Data Structure
---------------------

Each packet on the wire is arranged as follows (all multi‑byte fields big‑endian):

| Offset | Length | Field                                 |
|:------:|:------:|:--------------------------------------|
| 0      | 4      | Magic header: `"ASS1"`                |
| 4      | 1      | Filename length (N)                   |
| 5      | N      | Filename (UTF-8)                      |
| 5+N    | 4      | Backup timestamp (UNIX epoch seconds) |
| 9+N    | 4      | Original ctime                        |
| 13+N   | 4      | Original mtime                        |
| 17+N   | 4      | UID                                   |
| 21+N   | 4      | GID                                   |
| 25+N   | 2      | File mode (permissions)               |
| 27+N   | 1      | Flags (bit 0 = compression used)      |
| 28+N   | 4      | Payload byte-length                   |
| 32+N   | 4      | CRC32 of payload                      |
| 36+N   | M      | Payload (compressed or raw data)      |

After framing, each byte goes through Reed--Solomon -> optional byte interleaving -> optional resync marker insertion -> FSK/MFSK -> WAV.

The packet header intentionally remains `ASS1` for compatibility. It records payload compression but does **not** record the audio modulation mode. That is useful only after demodulation, so the decoder now detects mode from the audio layer before parsing the packet.

Compatibility Notes
-------------------

- Default encoding remains compatible with older decoders: no interleaving and no resync markers are inserted unless explicitly requested.

- `--interleave-depth` values greater than `1` are not backward compatible. New decoders must be told the depth, or decode with `--auto-interleave`.

- `--resync-interval N` is not backward compatible. New decoders strip the 64-bit resync marker automatically, but old decoders will treat those marker bits as payload.

- Decode-side auto-detection does not change the wire format. It tries common symbol rates and MFSK tone counts, then accepts only candidates that pass Reed--Solomon and CRC validation.

* * * * *

Prerequisites
-------------

-   **Python** 3.7+

### Dependencies

Install via `pip`:

```
pip install numpy sounddevice reedsolo
```

> `*lzma*`* is part of Python's standard library.*

* * * * *

Installation
------------

```
git clone https://your.repo.url/ass.git
cd ass
chmod +x ass.py
```

* * * * *

Usage
-----

```
usage: ass.py [-h] {encode,decode} [--data DATA] [--inputfile INPUTFILE]
             [--bitrate BITRATE] [--mfsk {2,4,8,16}]
             [--alwayscompress | --nocompress | --autocompress] 
             [--noclamp] [--interleave-depth N] [--auto-interleave]
             [--resync-interval N] [--brute-force-recovery]
             [--overwrite] file
```

-   **Positional arguments**:

    -   `encode, decode` : Mode of operation

    -   `file` : Output WAV when encoding; input WAV (or `-` for live audio) when decoding

-   **Optional arguments**:

    -   `-h, --help` : Show this help message and exit

    -   `--data DATA` : Inline string to encode

    -   `--inputfile FILE` : Path to input file to encode

    -   `--bitrate BITRATE` : Symbol rate. Encoding defaults to 1200. Decoding auto-detects only when both `--bitrate` and `--mfsk` are omitted.
    
    -   `--mfsk {2,4,8,16}` : Number of tones (2 = standard FSK; 4/8/16 = experimental MFSK). If only `--bitrate` is specified while decoding, `--mfsk` defaults to 2.

    -   `--noclamp` : bypass bitrate clamping (use with care!)

    -   `--interleave-depth N` : optional byte interleaver depth. `1` is compatible/default; values greater than `1` require a new decoder.

    -   `--auto-interleave` : on decode, try common interleave depths.

    -   `--resync-interval N` : insert a 64-bit resync marker every N Reed--Solomon encoded bytes. Disabled by default; requires a new decoder.

    -   `--brute-force-recovery` : enable slow RS-prefix recovery when header-based recovery fails. This is intended for damaged captures and can be CPU-intensive on long recordings.

    -   `--overwrite` : when decoding, overwrite an existing output file. By default the decoder adds a numeric suffix to avoid clobbering files.

    -   **Compression modes** (mutually exclusive):

        -   `--alwayscompress` : Always apply LZMA

        -   `--nocompress` : Disable LZMA

        -   `--autocompress` : Apply only if compressed < raw (default)

### Examples

```
# Encode with auto‑compress (default)
./ass.py encode output.wav --inputfile example.bin --bitrate 2400

# Encode forcing compression
./ass.py encode output.wav --inputfile example.bin --bitrate 2400 --alwayscompress

# Encode without compression
./ass.py encode output.wav --inputfile example.bin --bitrate 2400 --nocompress

# Decode from file with explicit settings
./ass.py decode input.wav --bitrate 1200 --mfsk 2

# Decode from file with explicit bitrate and default 2-FSK
./ass.py decode input.wav --bitrate 1200

# Decode from file with auto-detection
./ass.py decode input.wav

# Decode live from mic
./ass.py decode - --bitrate 1200 --mfsk 2

# Experimental 8-FSK at 100 bps bit-rate (effectively 300 bps)
./ass.py encode out-mfsk.wav --inputfile data.bin --bitrate 100 --mfsk 8

# Decode a 16-FSK transmission
./ass.py decode in-mfsk16.wav --bitrate 80 --mfsk 16

# Optional interleaving for burst-error resilience (new decoder required)
./ass.py encode interleaved.wav --inputfile data.bin --interleave-depth 8
./ass.py decode interleaved.wav --auto-interleave

```

# Bitrate Clamping

To ensure reliable tone discrimination, ASS by default limits your requested `--bitrate` so that the FFT’s frequency resolution (≈ bitrate Hz) does not exceed the spacing between adjacent M-FSK tones:

```
tone_spacing = (FREQ_1 - FREQ_0) / (M - 1)
max_bitrate  = floor(tone_spacing)
```

- **2-FSK (M=2)**: tone_spacing = 1200 Hz → max_bitrate = 1200 bps  
- **16-FSK (M=16)**: tone_spacing ≈ 1200 Hz / 15 ≈ 80 Hz → max_bitrate = 80 bps → effective bit-rate = 80 × 4 = 320 bps  

If you request a higher bitrate, ASS prints:

```
[MAIN] Warning: 8-FSK at 300 bps gives Δf_res≈300 Hz > tone spacing 171.4 Hz;
    → clamping to 171 bps
```

Add `--noclamp` to bypass the clamp (you’ll still see the warning, but ASS will respect your requested bitrate).

Decoded files are written with their original names, metadata, ownership, and permissions.

Restore Location
----------------

The packet stores the original filename only as a basename, not a full source path. Decode therefore restores files into the current working directory.

By default, decode will not overwrite an existing file. If `README.md` already exists, a decoded `README.md` is written as `README.1.md`, then `README.2.md`, and so on. Use `--overwrite` only when replacement is intentional.

Live Decode vs WAV Decode
-------------------------

WAV decode and live decode use the same final packet recovery path: sync/timing search, Reed--Solomon correction, header parsing, decompression, and CRC validation.

The difference is capture control:

- WAV decode already has the complete recording, so it can run recovery over the whole file immediately.

- Live decode must decide when enough audio has been captured. It therefore runs a lightweight rolling monitor that looks for sync and the repeated end marker while buffering the raw audio. Once capture stops, the buffered audio is passed to the same decoder used for WAV files.

- If the live monitor misses the end marker, press `Ctrl+C` after playback finishes. The buffered audio is still decoded using the full decoder.

- Live mode prints elapsed capture time, input level in dBFS, and stream overflow count. Very low level, clipping, or overflows usually indicate an audio-device/loopback problem rather than a packet-format problem.

For cassette work, recording the cassette to WAV first and then running `./ass.py decode recording.wav ...` is often the more repeatable workflow. It avoids live auto-stop issues and lets you inspect, normalize, archive, or retry the same capture with different decode options. Live decode is useful when you want direct tape-to-file restore without creating an intermediate WAV.

Sync And Timing Recovery
------------------------

Older versions demodulated using fixed symbol windows derived directly from the requested bitrate. That works for clean WAV files but fails quickly when tape or record/replay hardware adds leading silence, speed error, or timing drift.

The decoder now:

- trims leading/trailing silence using an RMS envelope
- locks to the preamble and sync word by testing symbol phase and samples-per-symbol candidates
- estimates constant playback speed error from the 1200/2400 Hz tone locations
- tries fixed and adaptive PLL-style demodulation candidates
- validates the final candidate with Reed--Solomon and payload CRC32
- can recover when the end marker is damaged by searching valid Reed--Solomon encoded prefixes

Normal decoding first uses header-based RS sizing, which keeps long recordings fast. `--brute-force-recovery` enables a slower RS-prefix search for severe damage; the decoder prints candidate progress so this mode does not appear hung.

This is a practical improvement, not a guarantee against severe tape wow/flutter. Very unstable playback can still exceed the correction range, but modest synthetic speed error and wow/flutter are covered by automated tests.

* * * * *

Inspiration
-----------

-   My love of classic **8‑bit microcomputers** and cassette storage

-   The deprecated **ctape** project: https://github.com/windytan/ctape

-   Low‑bandwidth digital modes in **amateur radio**

* * * * *

Disclaimer
----------

This code was partially generated and reviewed with the assistance of a large language model (LLM). It is provided as a work in progress; use at your own risk. Contributions and pull requests are welcome.

* * * * *

To Do
-----

-   Continue improving real-world tape tests with aged media and consumer recorders

-   Tune PLL loop constants against captured bad tapes rather than only synthetic tests

-   (Optional) GUI

* * * * *

*Have fun sending bits the old‑school way!*\
*73 de OH3SPN / M0SPN / TCM^SLP*
