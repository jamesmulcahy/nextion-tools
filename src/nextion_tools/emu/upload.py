"""TFT uploads (`whmi-wri` / `whmi-wris`), as the display handles them.

From the upload instruction on, the UART carries raw TFT bytes. The display writes them into its
flash at its own cursor and acknowledges every 4096-byte chunk with a bare 0x05. Under
`whmi-wris` (protocol v1.2) its reply to the first chunk is instead 0x08 and a u32le offset to
continue from, which lets it skip what it already holds; 0 means carry on.

The display can be told to misbehave (slow, lost or duplicated acks, jumps, stray bytes) to test
how an uploader copes.
"""

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .protocol import UPDATE_ACK, UPDATE_CHUNK, UPDATE_JUMP

log = logging.getLogger("nextion_tools.emu.upload")

UPLOAD = re.compile(r"^whmi-(wris?) (\d+),(\d+),(\d+)$")


@dataclass
class Upload:
    """One TFT upload as the display saw it."""

    size: int
    baud: int
    protocol: str  # "v1.1" (whmi-wri) or "v1.2" (whmi-wris)
    image: bytearray  # the display's flash after this upload; starts as the previous image
    cursor: int = 0  # where the next received byte lands
    chunk_fill: int = 0  # bytes received since the last ack
    chunks: List[Tuple[int, int, float]] = field(default_factory=list)  # (offset, length, time) per chunk
    acks: List[Tuple[float, bytes]] = field(default_factory=list)  # (time, bytes) sent to the MCU
    overrun: int = 0  # bytes received after the image was complete
    complete: bool = False
    abandoned: bool = False  # power-cycled before complete, or refused
    started: float = field(default_factory=time.monotonic)

    @property
    def md5(self) -> str:
        return hashlib.md5(bytes(self.image)).hexdigest()

    @property
    def received(self) -> int:
        return sum(length for _, length, _ in self.chunks) + self.chunk_fill


class UploadMixin:
    """The upload half of Display. Expects `_cond` and `_reply(data, delay, record)` from it."""

    def _init_upload(self, flash_size: int) -> None:
        self.flash_size = flash_size
        self.upload: Optional[Upload] = None  # in progress
        self.uploads: List[Upload] = []  # all, including the one in progress
        # The TFT the display holds now (what a v1.2 jump leaves in place). display_state is "ok"
        # if it runs, or "data_error" when the display would show SYSTEM DATA ERROR: an upload
        # was cut short, or produced an image that is none of valid_md5s (empty: any image is
        # valid).
        self.flash: bytes = b""
        self.valid_md5s: Set[str] = set()
        self.display_state = "ok"

        # Misbehaviour.
        self.ack_delay = 0.0  # seconds before each ack; ~0.38 at 115200 baud on hardware
        self.jump_offset = 0  # v1.2 reply to the first chunk
        # Send 0x08 and its offset as two writes, which is how they arrive from a real display;
        # False sends all five bytes at once.
        self.split_jump = True
        self.reject_upload = False  # never acknowledge whmi-wri(s)
        # Chunk numbers count from 1 within each upload. These fire once and are then forgotten,
        # so a retried upload goes through; the always_ ones fire on every upload.
        self.drop_ack_at: Set[int] = set()  # chunks not acknowledged
        self.always_drop_ack_at: Set[int] = set()
        self.dup_ack_at: Set[int] = set()  # chunks acknowledged twice
        self.inject_at: Dict[int, bytes] = {}  # chunk -> raw bytes sent right after its ack

    @property
    def last_upload(self) -> Optional[Upload]:
        with self._cond:
            return self.uploads[-1] if self.uploads else None

    def wait_for_upload_start(self, timeout: float = 120, count: int = 1) -> Upload:
        self.wait_for(lambda n: len(n.uploads) >= count, timeout, "upload #%d to start" % count)
        return self.uploads[count - 1]

    def wait_for_upload_progress(self, offset: int, timeout: float = 300) -> None:
        self.wait_for(lambda n: n.upload is not None and n.upload.cursor >= offset, timeout,
                      "upload to reach offset %d" % offset)

    # Called with _cond held.

    def _start_upload(self, m: "re.Match") -> None:
        size, baud = int(m.group(2)), int(m.group(3))
        protocol = "v1.2" if m.group(1) == "wris" else "v1.1"
        if size > self.flash_size:
            # Recorded, so tests see it, but never acknowledged: the TFT cannot fit.
            log.warning("upload of %d bytes exceeds the display's %d-byte flash; not acknowledged", size, self.flash_size)
            self.uploads.append(Upload(size=size, baud=baud, protocol=protocol, image=bytearray(), abandoned=True))
            return
        image = bytearray(self.flash[:size].ljust(size, b"\0"))
        self.upload = Upload(size=size, baud=baud, protocol=protocol, image=image)
        self.uploads.append(self.upload)
        log.info("upload started: %d bytes at %d baud, %s", size, baud, protocol)
        if not self.reject_upload:
            self._upload_reply(bytes([UPDATE_ACK]))

    def _abandon_upload(self) -> None:
        if self.upload is not None:
            log.warning("upload abandoned at %d/%d bytes", self.upload.cursor, self.upload.size)
            self.upload.abandoned = True
            self.upload = None
            self.display_state = "data_error"

    def _receive_upload(self, buf: bytes) -> bytes:
        """Take TFT bytes from buf up to the end of the current chunk; return the rest."""
        u = self.upload
        take = min(len(buf), UPDATE_CHUNK - u.chunk_fill, u.size - u.cursor)
        if take <= 0:
            # Sent past the end of the image, or past a chunk that was not acknowledged.
            u.overrun += len(buf)
            return b""
        u.image[u.cursor:u.cursor + take] = buf[:take]
        u.cursor += take
        u.chunk_fill += take
        if u.chunk_fill == UPDATE_CHUNK or u.cursor >= u.size:
            self._end_chunk(u)
        return buf[take:]

    def _end_chunk(self, u: Upload) -> None:
        start = u.cursor - u.chunk_fill
        u.chunks.append((start, u.chunk_fill, time.monotonic()))
        u.chunk_fill = 0
        n = len(u.chunks)
        if u.cursor >= u.size:
            u.complete = True
            self.upload = None
            self.flash = bytes(u.image)
            ok = not self.valid_md5s or u.md5 in self.valid_md5s
            self.display_state = "ok" if ok else "data_error"
            log.info("upload complete after %d chunks, md5 %s (%s)", n, u.md5, self.display_state)
            self._upload_finished()
            return
        if n in self.drop_ack_at or n in self.always_drop_ack_at:
            log.info("dropping ack for chunk %d", n)
            self.drop_ack_at.discard(n)
            return
        if u.protocol == "v1.2" and n == 1:
            jump = self.jump_offset
            if jump:
                u.cursor = jump
            offset = jump.to_bytes(4, "little")
            if self.split_jump:
                self._upload_reply(bytes([UPDATE_JUMP]))
                self._upload_reply(offset, delay=0.05)
            else:
                self._upload_reply(bytes([UPDATE_JUMP]) + offset)
        else:
            self._upload_reply(bytes([UPDATE_ACK]))
        if n in self.dup_ack_at:
            self.dup_ack_at.discard(n)
            self._upload_reply(bytes([UPDATE_ACK]), delay=0.05)
        if n in self.inject_at:
            self._upload_reply(self.inject_at.pop(n), delay=0.05)

    def _upload_reply(self, data: bytes, delay: Optional[float] = None) -> None:
        u = self.uploads[-1] if self.uploads else None
        self._reply(data, self.ack_delay if delay is None else delay, upload=u)

    def _upload_finished(self) -> None:
        """Hook for Display: the display restarts on the new TFT."""
