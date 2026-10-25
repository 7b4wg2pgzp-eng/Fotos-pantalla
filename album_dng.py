"""Read the embedded JPEG of a DNG without decoding its large RAW sensor data."""
import struct

MAX_PREVIEW_BYTES = 32 * 1024 * 1024


def extract_preview(read, size):
    def bounded(offset, length):
        if offset < 0 or length < 0 or offset + length > size:
            raise ValueError('Invalid DNG offset')
        data = read(offset, length)
        if len(data) != length:
            raise ValueError('Incomplete DNG range')
        return data

    header = bounded(0, 8)
    if header[:2] not in (b'II', b'MM'):
        raise ValueError('Unsupported DNG header')
    endian = '<' if header[:2] == b'II' else '>'
    if struct.unpack(endian + 'H', header[2:4])[0] != 42:
        raise ValueError('Unsupported DNG TIFF format')
    pending = [struct.unpack(endian + 'I', header[4:])[0]]
    seen, candidates = set(), []
    while pending:
        offset = pending.pop(0)
        if not offset or offset in seen:
            continue
        if len(seen) >= 32:
            raise ValueError('Too many DNG image directories')
        seen.add(offset)
        count = struct.unpack(endian + 'H', bounded(offset, 2))[0]
        if count > 512:
            raise ValueError('Too many DNG tags')
        entries = bounded(offset + 2, count * 12 + 4)
        tags = {}
        for index in range(count):
            entry = entries[index * 12:(index + 1) * 12]
            tag, kind, length = struct.unpack(endian + 'HHI', entry[:8])
            if tag not in (254, 256, 257, 259, 273, 279, 330, 513, 514) or kind not in (3, 4):
                continue
            if length > 1024:
                raise ValueError('Oversized DNG tag')
            width, code = (2, 'H') if kind == 3 else (4, 'I')
            value = entry[8:8 + length * width] if length * width <= 4 else bounded(
                struct.unpack(endian + 'I', entry[8:12])[0], length * width)
            tags[tag] = struct.unpack(endian + str(length) + code, value)
        pending.extend(tags.get(330, ()))
        pending.append(struct.unpack(endian + 'I', entries[count * 12:])[0])
        # Reduced-resolution DNG images can still be full-sized JPEG previews.
        # Pillow's JPEG draft decoder downsamples them before allocating pixels.
        if not (tags.get(254, (0,))[0] & 1):
            continue
        offsets, lengths = tags.get(513), tags.get(514)
        if not offsets and tags.get(259, (0,))[0] in (6, 7):
            offsets, lengths = tags.get(273), tags.get(279)
        if offsets and lengths and len(offsets) == len(lengths) == 1:
            if 0 < lengths[0] <= MAX_PREVIEW_BYTES and offsets[0] + lengths[0] <= size:
                candidates.append((tags.get(256, (0,))[0] * tags.get(257, (0,))[0], offsets[0], lengths[0]))
    for _, offset, length in sorted(candidates, reverse=True):
        data = bounded(offset, length)
        if data.startswith(b'\xff\xd8'):
            return data
    raise ValueError('DNG has no supported embedded JPEG preview')
