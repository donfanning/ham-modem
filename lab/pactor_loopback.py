#!/usr/bin/env python3
"""PACTOR-I loopback test: modulate packets at 100 and 200 bd, add noise, demodulate, and verify.

Usage: PYTHONPATH=. .venv/bin/python lab/pactor_loopback.py
"""

import sys
import numpy as np

sys.path.insert(0, ".")
from hammodem.pactor import code, fsk
from hammodem.pactor.arq import MemoryArq

FS = fsk.FS
RUNS = 50


def tx_packet(data: bytes, counter: int, baud: int, header: int = code.HEADER1) -> np.ndarray:
    pkt = code.build_packet(data, counter)
    m = fsk.Modulator()
    hdr_bits = [(header >> i) & 1 for i in range(8)]
    pkt_bits = []
    for byte in pkt:
        for i in range(8):
            pkt_bits.append((byte >> i) & 1)
    return m.bits(hdr_bits + pkt_bits, baud)


def rx_packet(audio: np.ndarray, baud: int) -> dict | None:
    disc = fsk.Discriminator(baud=baud)
    d, e = disc.process(audio.astype(float))
    start = fsk.detect_burst(e, threshold=0.0005, min_samples=50)
    if start is None:
        return None
    pkt_bytes = 23 if baud == 200 else 11
    total_bits = 8 + pkt_bytes * 8
    phase, _ = fsk.best_phase(d, start, total_bits, baud)
    bits_out, _ = fsk.slice_bits(d, start + phase, total_bits, baud)
    if len(bits_out) < total_bits:
        return None
    hdr = sum(bits_out[i] << i for i in range(8))
    raw = bytearray()
    for byte_i in range(pkt_bytes):
        val = 0
        for bit_i in range(8):
            val |= bits_out[8 + byte_i * 8 + bit_i] << bit_i
        raw.append(val)
    parsed = code.parse_packet(bytes(raw))
    if parsed:
        parsed["header"] = hdr
    return parsed


def test_clean(baud: int) -> int:
    data_len = 20 if baud == 200 else 8
    ok = 0
    for i in range(RUNS):
        data = bytes([(i * 7 + j) & 0x7F for j in range(data_len)])
        audio = tx_packet(data, i & 3, baud)
        audio = np.concatenate((np.zeros(300, np.int16), audio, np.zeros(300, np.int16)))
        parsed = rx_packet(audio, baud)
        if parsed and parsed["data"] == data:
            ok += 1
    return ok


def test_noisy(baud: int, snr_db: float) -> int:
    rng = np.random.RandomState(42)
    data_len = 20 if baud == 200 else 8
    ok = 0
    for i in range(RUNS):
        data = bytes([(i * 7 + j) & 0x7F for j in range(data_len)])
        audio = tx_packet(data, i & 3, baud).astype(float)
        noise_power = 10 ** (-snr_db / 20) * np.std(audio)
        noisy = (audio + rng.normal(0, noise_power, len(audio))).astype(np.int16)
        noisy = np.concatenate((np.zeros(300, np.int16), noisy, np.zeros(300, np.int16)))
        parsed = rx_packet(noisy, baud)
        if parsed and parsed["data"] == data:
            ok += 1
    return ok


def test_memory_arq(baud: int, snr_db: float, n_tries: int = 3) -> int:
    rng = np.random.RandomState(99)
    data_len = 20 if baud == 200 else 8
    pkt_bytes = 23 if baud == 200 else 11
    ok = 0
    for i in range(RUNS):
        data = bytes([(i * 7 + j) & 0x7F for j in range(data_len)])
        audio_clean = tx_packet(data, i & 3, baud)
        total_bits = 8 + pkt_bytes * 8
        marq = MemoryArq(pkt_bytes * 8)
        recovered = False
        for t in range(n_tries):
            audio = audio_clean.astype(float)
            noise_power = 10 ** (-snr_db / 20) * np.std(audio)
            noisy = (audio + rng.normal(0, noise_power, len(audio))).astype(np.int16)
            noisy = np.concatenate((np.zeros(300, np.int16), noisy, np.zeros(300, np.int16)))
            disc = fsk.Discriminator(baud=baud)
            d, e = disc.process(noisy.astype(float))
            start = fsk.detect_burst(e, threshold=0.0003, min_samples=50)
            if start is None:
                continue
            phase, _ = fsk.best_phase(d, start, total_bits, baud)
            bits_out, soft = fsk.slice_bits(d, start + phase, total_bits, baud)
            if len(soft) >= 8 + pkt_bytes * 8:
                marq.add(soft[8:])  # skip header soft values
            if marq.count >= 2:
                marq_bits = marq.decide()
                raw = bytearray()
                for byte_i in range(pkt_bytes):
                    val = 0
                    for bit_i in range(8):
                        idx = byte_i * 8 + bit_i
                        val |= (marq_bits[idx] if idx < len(marq_bits) else 0) << bit_i
                    raw.append(val)
                parsed = code.parse_packet(bytes(raw))
                if parsed and parsed["data"] == data:
                    recovered = True
                    break
        if recovered:
            ok += 1
    return ok


def main():
    print("PACTOR-I loopback test")
    print("=" * 50)
    for baud in (100, 200):
        ok = test_clean(baud)
        print(f"  {baud} bd clean:    {ok}/{RUNS}")
    for baud in (100, 200):
        for snr in (20, 15, 12, 10):
            ok = test_noisy(baud, snr)
            print(f"  {baud} bd {snr:2d} dB:    {ok}/{RUNS}")
    print("\nMemory ARQ (3 tries at 8 dB):")
    for baud in (100, 200):
        ok = test_memory_arq(baud, 8.0, n_tries=3)
        print(f"  {baud} bd MARQ:     {ok}/{RUNS}")

    # Huffman compression test
    print("\nHuffman compression:")
    for text in [b"the quick brown fox", b"HELLO WORLD 12345", b"CQ CQ CQ DE W1AW W1AW K"]:
        packed, nbits = code.huffman_encode(text)
        decoded = code.huffman_decode(packed, nbits)
        ratio = len(packed) / len(text)
        print(f"  {text.decode()!r:30s} {len(text):2d}B -> {len(packed):2d}B ({ratio:.0%}) ok={decoded == text}")

    # CS detection test
    print("\nControl signal audio roundtrip:")
    for name, cs in [("CS1", code.CS1), ("CS2", code.CS2), ("CS3", code.CS3), ("CS4", code.CS4)]:
        m = fsk.Modulator()
        audio = m.bits(code.cs_bits(cs), baud=100)
        audio = np.concatenate((np.zeros(100, np.int16), audio, np.zeros(100, np.int16)))
        disc = fsk.Discriminator(baud=100)
        d, _ = disc.process(audio.astype(float))
        phase, _ = fsk.best_phase(d, 100, 12, baud=100)
        bits_out, _ = fsk.slice_bits(d, 100 + phase, 12, baud=100)
        recovered = code.cs_from_bits(bits_out)
        print(f"  {name}: {'OK' if recovered == cs else 'FAIL'}")


if __name__ == "__main__":
    main()
