"""Strict, bounded FLV/AVC/AAC inspection; no video or audio decoding."""
from dataclasses import dataclass
import struct
import time

FLV_HEADER = b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00"
MAX_TAG_BYTES = 2 * 1024 * 1024


class MediaError(ValueError):
    pass


def exact(stream, size, allow_eof=False):
    chunks = bytearray()
    while len(chunks) < size:
        part = stream.read(size - len(chunks))
        if not part:
            if allow_eof and not chunks:
                return None
            raise MediaError("Entrada FLV truncada.")
        chunks.extend(part)
    return bytes(chunks)


@dataclass(frozen=True, slots=True)
class Tag:
    kind: int
    timestamp: int
    payload: bytes
    arrival: float

    @property
    def size(self):
        return len(self.payload) + 15

    def with_timestamp(self, timestamp):
        return Tag(self.kind, int(timestamp), self.payload, self.arrival)

    def encode(self):
        t = self.timestamp
        if not 0 <= t <= 0xFFFFFFFF:
            raise MediaError("Timestamp fuera del rango FLV; reinicia la sesión.")
        n = len(self.payload)
        return (bytes([self.kind]) + n.to_bytes(3, "big")
                + (t & 0xFFFFFF).to_bytes(3, "big") + bytes([t >> 24])
                + b"\x00\x00\x00" + self.payload + struct.pack(">I", n + 11))


def read_tags(stream):
    header = exact(stream, 9)
    if header[:4] != b"FLV\x01" or header[4] != 5:
        raise MediaError("Se requiere FLV con audio y vídeo.")
    offset = int.from_bytes(header[5:9], "big")
    if not 9 <= offset <= 4096:
        raise MediaError("Cabecera FLV inválida.")
    exact(stream, offset - 9)
    if exact(stream, 4) != b"\x00" * 4:
        raise MediaError("PreviousTagSize inicial inválido.")
    while (header := exact(stream, 11, allow_eof=True)) is not None:
        size = int.from_bytes(header[1:4], "big")
        if size > MAX_TAG_BYTES:
            raise MediaError("Paquete superior al límite de 2 MiB.")
        if header[0] not in (8, 9, 18) or header[8:11] != b"\x00" * 3:
            raise MediaError("Tipo de paquete FLV no soportado.")
        data = exact(stream, size)
        if int.from_bytes(exact(stream, 4), "big") != size + 11:
            raise MediaError("Tamaño FLV inconsistente.")
        ts = int.from_bytes(header[4:7], "big") | (header[7] << 24)
        yield Tag(header[0], ts, data, time.perf_counter())


class Bits:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def read(self, n):
        if self.pos + n > len(self.data) * 8:
            raise MediaError("Cabecera de codec truncada.")
        out = 0
        for _ in range(n):
            out = out * 2 + ((self.data[self.pos // 8] >> (7 - self.pos % 8)) & 1)
            self.pos += 1
        return out

    def ue(self):
        zeros = 0
        while not self.read(1):
            zeros += 1
            if zeros > 31:
                raise MediaError("Código Exp-Golomb inválido.")
        return (1 << zeros) - 1 + self.read(zeros)


def slice_type(nal):
    # Only the beginning of the slice header is necessary. Remove escape bytes
    # before reading first_mb_in_slice and slice_type. Never decode pixels.
    rbsp = bytearray()
    zeros = 0
    for value in nal[1:65]:
        if zeros >= 2 and value == 3:
            zeros = 0
            continue
        rbsp.append(value)
        zeros = zeros + 1 if value == 0 else 0
    bits = Bits(rbsp)
    bits.ue()
    result = bits.ue()
    if result > 9:
        raise MediaError("slice_type H.264 inválido.")
    return result % 5


def tag_has_idr(tag, nal_length):
    """Return True iff an AVC NALU video tag contains an IDR slice (NAL type 5)."""
    if tag.kind != 9 or nal_length is None:
        return False
    p = tag.payload
    if len(p) < 5 or p[1] != 1:
        return False
    pos = 5
    while pos + nal_length <= len(p):
        size = int.from_bytes(p[pos:pos + nal_length], "big")
        pos += nal_length
        if not size or pos + size > len(p):
            return False
        if (p[pos] & 31) == 5:
            return True
        pos += size
    return False


class Validator:
    def __init__(self, *, allow_bframes=False):
        self.allow_bframes = bool(allow_bframes)
        self.headers = {}
        self.nal_length = None
        self.parameter_sets = set()
        self.seen_idr = False
        self.last = {}
        self.last_global = None
        self.sample_rate = None
        self.channels = None
        self.has_bframes = False
        self.last_is_idr = False
        self.first_idr_tag = None
        self.video_dts_samples = []

    @property
    def ready(self):
        return len(self.headers) == 2 and self.seen_idr

    def _header(self, tag):
        old = self.headers.get(tag.kind)
        if old is not None and old.payload != tag.payload:
            raise MediaError("Configuración del encoder cambiada. Reinicia la sesión.")
        self.headers[tag.kind] = Tag(tag.kind, 0, tag.payload, tag.arrival)

    def _avcc(self, raw):
        if len(raw) < 7 or raw[0] != 1:
            raise MediaError("AVCDecoderConfigurationRecord inválido.")
        self.nal_length = (raw[4] & 3) + 1
        if self.nal_length not in (1, 2, 4):
            raise MediaError("Tamaño de longitud NAL no admitido.")
        pos = 6
        sets = set()
        counts = [raw[5] & 31]
        for group in range(2):
            if group:
                if pos >= len(raw):
                    raise MediaError("Faltan PPS.")
                counts.append(raw[pos]); pos += 1
            if counts[group] == 0:
                raise MediaError("Se requieren SPS y PPS.")
            for _ in range(counts[group]):
                if pos + 2 > len(raw):
                    raise MediaError("SPS/PPS truncado.")
                size = int.from_bytes(raw[pos:pos+2], "big"); pos += 2
                nal = raw[pos:pos+size]; pos += size
                if not size or len(nal) != size or (nal[0] & 31) != (7 if group == 0 else 8):
                    raise MediaError("SPS/PPS inválido.")
                sets.add(nal)
        self.parameter_sets = sets

    def _aac(self, raw):
        bits = Bits(raw)
        obj, frequency = bits.read(5), bits.read(4)
        rates = {3: 48000, 4: 44100}
        channels = bits.read(4)
        if obj != 2 or frequency not in rates or channels not in (1, 2):
            raise MediaError("Se requiere AAC-LC, 44,1/48 kHz y mono/estéreo.")
        # GASpecificConfig: only 1024-sample frames without extensions/core coder.
        if bits.read(1) or bits.read(1) or bits.read(1):
            raise MediaError("Configuración AAC extendida no soportada.")
        self.sample_rate, self.channels = rates[frequency], channels

    def accept(self, tag):
        """Return whether this is a validated media packet to queue."""
        p = tag.payload
        self.last_is_idr = False
        if tag.kind == 18:
            return False  # No metadata retention, including arbitrary encoder strings.
        if tag.kind == 9:
            if len(p) < 5 or p[0] & 0x80 or p[0] & 15 != 7:
                raise MediaError("Solo se admite vídeo H.264 en FLV clásico.")
            if p[1] == 0:
                self._header(tag); self._avcc(p[5:]); return False
            if p[1] == 2:
                return False
            if p[1] != 1 or self.nal_length is None:
                raise MediaError("Vídeo sin cabecera AVC válida.")
            if p[2:5] != b"\x00" * 3:
                if not self.allow_bframes:
                    raise MediaError("PTS distinto de DTS: configura B-frames = 0.")
                self.has_bframes = True
            pos, has_slice, idr = 5, False, False
            while pos < len(p):
                if pos + self.nal_length > len(p):
                    raise MediaError("Longitud NAL truncada.")
                size = int.from_bytes(p[pos:pos+self.nal_length], "big")
                pos += self.nal_length
                nal = p[pos:pos+size]; pos += size
                if not size or len(nal) != size or nal[0] & 0x80:
                    raise MediaError("NAL inválido.")
                kind = nal[0] & 31
                if kind in (1, 5):
                    st = slice_type(nal)
                    if st == 1:
                        if not self.allow_bframes:
                            raise MediaError("B-frames detectados en H.264. Configura bf=0.")
                        self.has_bframes = True
                    elif st not in (0, 2):
                        raise MediaError("Solo se admiten slices I/P." if not self.allow_bframes
                                         else "Solo se admiten slices I/P/B.")
                    has_slice = True
                    idr |= kind == 5
                elif kind in (7, 8):
                    if nal not in self.parameter_sets:
                        raise MediaError("SPS/PPS cambiado dentro del flujo.")
                elif kind not in (6, 9, 10, 11, 12):
                    raise MediaError("Extensión H.264 no soportada.")
            if not has_slice:
                return False
            if not self.seen_idr:
                if not idr:
                    raise MediaError("El primer vídeo debe empezar en un IDR.")
                self.seen_idr = True
            self.last_is_idr = idr
            if self.first_idr_tag is None and idr:
                self.first_idr_tag = tag
            if len(self.video_dts_samples) < 16:
                self.video_dts_samples.append(tag.timestamp)
        else:
            if len(p) < 3 or p[0] >> 4 != 10:
                raise MediaError("Solo se admite audio AAC.")
            if p[1] == 0:
                self._header(tag); self._aac(p[2:]); return False
            if p[1] != 1 or 8 not in self.headers:
                raise MediaError("Audio sin cabecera AAC válida.")
        previous = self.last.get(tag.kind)
        if previous is not None and tag.timestamp <= previous:
            raise MediaError("Los timestamps de cada pista deben crecer.")
        if self.last_global is not None and tag.timestamp < self.last_global:
            raise MediaError("Entrada FLV fuera de orden temporal.")
        self.last[tag.kind] = tag.timestamp
        self.last_global = tag.timestamp
        return True
