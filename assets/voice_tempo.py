"""Streaming, pitch-preserving tempo for little-endian signed PCM16.

One instance belongs to one utterance, including every decoder PCM chunk.
The 1.0 path needs no NumPy/AudioTSM and is byte-identical. Faster playback
uses AudioTSM 0.1.2 (MIT), whose bounded overlap-add buffers are retained
until finish(). No audio device, model, or GPU is involved.
"""
from __future__ import annotations

import math

# WSOLA adaptation below: Copyright (c) 2017 Muges (AudioTSM).
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.


def _make_wsola(np, channels: int, speed: float, frame_length: int):
    """Adapt AudioTSM WSOLA for linked stereo and fractional analysis hops.

    Algorithm adapted from audiotsm/wsola.py, Copyright (c) 2017 Muges,
    MIT license (distributed with the audiotsm dependency). The underlying
    AnalysisSynthesisTSM owns windowing, normalization and circular buffers.
    """
    from audiotsm.base import AnalysisSynthesisTSM
    from audiotsm.wsola import WSOLAConverter
    from audiotsm.utils.windows import hanning

    hop = frame_length // 2
    tolerance = hop

    class LinkedConverter(WSOLAConverter):
        def convert_frame(self, analysis_frame):
            if self._first:
                # AudioTSM's input includes the search margin before the
                # actual frame. Start at the frame, avoiding a silent prefix.
                delta = self._tolerance
            else:
                correlation = sum(
                    np.correlate(analysis_frame[k, :-self._synthesis_hop],
                                 self._natural_progression[k])
                    for k in range(self._channels)
                )
                # A single alignment preserves the inter-channel relation.
                delta = int(np.argmax(correlation))
            np.copyto(self._synthesis_frame,
                      analysis_frame[:, delta:delta + self._frame_length])
            delta += self._synthesis_hop
            np.copyto(self._natural_progression,
                      analysis_frame[:, delta:delta + self._frame_length])
            self._first = False
            return self._synthesis_frame

    class FractionalHopTSM(AnalysisSynthesisTSM):
        # Pinned AudioTSM 0.1.2 frame hook: rounding only a fixed hop would
        # accumulate timing drift in long streams at rates such as 1.08.
        def clear(self):
            self._frame_index = 0
            super().clear()

        def _process_frame(self):
            index = self._frame_index
            self._analysis_hop = (round((index + 1) * speed * hop)
                                  - round(index * speed * hop))
            self._converter.set_analysis_hop(self._analysis_hop)
            super()._process_frame()
            self._frame_index += 1

    converter = LinkedConverter(channels, frame_length, hop, tolerance)
    return FractionalHopTSM(converter, channels, frame_length,
                            round(hop * speed), hop, None,
                            hanning(frame_length), tolerance, tolerance + hop)


class TempoStream:
    """Transform arbitrary PCM byte chunks without changing the sample rate.

    ``process`` always returns complete interleaved PCM16 frames; a partial
    input frame is carried into the next call. ``finish`` pads the WSOLA
    lookahead internally and trims to round(input_frames / speed), so no
    incomplete analysis window or zero-padding is lost/leaked. An incomplete
    PCM frame at EOF raises ValueError. Repeated finish() is harmless.

    At 48 kHz the WSOLA window is 1024 samples (21.3 ms), with roughly
    43 ms of lookahead before the first output. Retained state is bounded
    by the analysis window, independent of utterance duration.
    """

    def __init__(self, rate: int = 48000, channels: int = 2,
                 speed: float = 1.08):
        if (not isinstance(rate, int) or isinstance(rate, bool) or rate <= 0
                or not isinstance(channels, int) or isinstance(channels, bool)
                or channels <= 0):
            raise ValueError('rate and channels must be positive integers')
        speed = float(speed)
        if not math.isfinite(speed) or not 1.0 <= speed <= 1.2:
            raise ValueError('tempo speed must be between 1.0 and 1.2')
        self.rate = rate
        self.channels = channels
        self.speed = speed
        self._frame_bytes = 2 * channels
        self._carry = b''
        self._input_frames = 0
        self._output_frames = 0
        self._finished = False
        self._tsm = None
        if speed != 1.0:
            import numpy as np
            from audiotsm.io.array import ArrayReader, ArrayWriter

            self._np = np
            self._reader = ArrayReader
            self._writer = ArrayWriter
            # Keep the same ~21 ms window at other PCM sample rates.
            self._window = max(32, 2 * round(512 * rate / 48000))
            self._tsm = _make_wsola(np, channels, speed, self._window)

    def _transform(self, pcm: bytes) -> bytes:
        np = self._np
        samples = np.frombuffer(pcm, dtype='<i2').reshape(-1, self.channels)
        pieces = []
        # Float working memory remains fixed even if a decoder supplies a
        # large PCM chunk. Returned bytes naturally scale with that chunk.
        for offset in range(0, len(samples), 4096):
            source = samples[offset:offset + 4096].T.astype(np.float32)
            writer = self._writer(self.channels)
            self._tsm.run(self._reader(source), writer, flush=False)
            result = np.rint(np.clip(writer.data.T, -32768, 32767))
            pieces.append(result.astype('<i2').tobytes())
        return b''.join(pieces)

    def process(self, pcm: bytes) -> bytes:
        if self._finished:
            raise RuntimeError('tempo stream is already finished')
        data = self._carry + bytes(pcm)
        aligned = len(data) - len(data) % self._frame_bytes
        self._carry = data[aligned:]
        data = data[:aligned]
        self._input_frames += aligned // self._frame_bytes
        result = data if self._tsm is None else self._transform(data)
        self._output_frames += len(result) // self._frame_bytes
        return result

    def finish(self) -> bytes:
        if self._finished:
            return b''
        if self._carry:
            raise ValueError('incomplete interleaved PCM16 frame at EOF')
        if self._tsm is None or self._input_frames == 0:
            self._finished = True
            return b''
        remaining = round(self._input_frames / self.speed) - self._output_frames
        # AudioTSM flush_to() does not process a partial analysis window.
        # Feed only a bounded lookahead of zeros, then retain the actual
        # requested duration rather than its padded output.
        padded = self._transform(bytes(self._window * 4 * self._frame_bytes))
        needed = remaining * self._frame_bytes
        if remaining < 0 or len(padded) < needed:
            raise RuntimeError('tempo output length exceeded its frame bound')
        self._output_frames += remaining
        self._finished = True
        self._tsm = None
        return padded[:needed]
