"""The translation stream's frame format and input conditioning. No I/O.

Every audio message, in both directions, is one frame of exactly 976 bytes:

    bytes 0-7    sequence number, uint64 little-endian
    bytes 8-15   sample offset, uint64 little-endian (sequence x 480)
    bytes 16-975 480 samples of 16-bit signed little-endian PCM, mono, 24 kHz: 20 ms
"""

from __future__ import annotations

import struct
from typing import Optional

from av import AudioFrame, AudioResampler

STREAM_SAMPLE_RATE = 24_000
SAMPLES_PER_FRAME = 480
PCM_BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2
SILENCE = bytes(PCM_BYTES_PER_FRAME)

_HEADER = struct.Struct("<QQ")
FRAME_BYTES = _HEADER.size + PCM_BYTES_PER_FRAME


def encode_frame(sequence: int, pcm: bytes) -> bytes:
    if len(pcm) != PCM_BYTES_PER_FRAME:
        raise ValueError(f"A frame carries {PCM_BYTES_PER_FRAME} bytes of PCM, not {len(pcm)}")
    return _HEADER.pack(sequence, sequence * SAMPLES_PER_FRAME) + pcm


def decode_frame(message: bytes) -> tuple[int, int, bytes]:
    """Return ``(sequence, sample_offset, pcm)``."""
    if len(message) != FRAME_BYTES:
        raise ValueError(f"An audio frame is {FRAME_BYTES} bytes, not {len(message)}")
    sequence, sample_offset = _HEADER.unpack_from(message)
    return sequence, sample_offset, bytes(message[_HEADER.size :])


class PcmFramer:
    """Turns chunks of 16-bit mono PCM of any size, at ``sample_rate``, into the
    960-byte payloads of 24 kHz frames. Holds whatever doesn't fill a frame yet."""

    def __init__(self, sample_rate: int) -> None:
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive, not {sample_rate}")
        self._sample_rate = sample_rate
        self._resampler: Optional[AudioResampler] = None
        if sample_rate != STREAM_SAMPLE_RATE:
            self._resampler = AudioResampler(format="s16", layout="mono", rate=STREAM_SAMPLE_RATE)
        self._odd_byte = b""
        self._buffer = bytearray()

    def push(self, pcm: bytes) -> list[bytes]:
        data = self._odd_byte + bytes(pcm)
        whole_samples = len(data) & ~1
        self._odd_byte = data[whole_samples:]
        if whole_samples:
            self._buffer += self._resample(data[:whole_samples])
        return self._take_frames()

    def flush(self) -> list[bytes]:
        """Return what's left, with the last frame padded with silence. Call it once,
        at the end of the audio."""
        if self._resampler is not None:
            self._buffer += self._pcm_of(self._resampler.resample(None))
        frames = self._take_frames()
        if self._buffer:
            frames.append(bytes(self._buffer) + bytes(PCM_BYTES_PER_FRAME - len(self._buffer)))
        return frames

    def _resample(self, pcm: bytes) -> bytes:
        if self._resampler is None:
            return pcm
        frame = AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
        frame.sample_rate = self._sample_rate
        frame.planes[0].update(pcm)
        return self._pcm_of(self._resampler.resample(frame))

    @staticmethod
    def _pcm_of(frames: list[AudioFrame]) -> bytes:
        return b"".join(bytes(frame.planes[0])[: frame.samples * 2] for frame in frames)

    def _take_frames(self) -> list[bytes]:
        count = len(self._buffer) // PCM_BYTES_PER_FRAME
        frames = [
            bytes(self._buffer[index * PCM_BYTES_PER_FRAME : (index + 1) * PCM_BYTES_PER_FRAME])
            for index in range(count)
        ]
        del self._buffer[: count * PCM_BYTES_PER_FRAME]
        return frames
