"""A small QR code encoder: byte mode, error correction level M, versions 1 to 10 (up to 213 bytes).
Enough for the wireless-debugging pairing code, and no packages to install.

encode(text) returns a square list of rows, True for a dark module (no quiet zone)."""

# Level M: (error-correction codewords per block, [(number of blocks, data codewords per block), ...])
_M = {1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
      5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
      9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)])}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
          7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
_EC_LEVEL_M = 0b00

# ------------------------------------------------------------------ Reed-Solomon over GF(256)

_EXP, _LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i], _LOG[_x] = _x, _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _generator(n):
    g = [1]
    for i in range(n):                       # g(x) *= (x - a^i), highest power first
        nxt = g + [0]
        for j, c in enumerate(g):
            nxt[j + 1] ^= _mul(c, _EXP[i])
        g = nxt
    return g


def _ec_codewords(data, n):
    g = _generator(n)
    rem = list(data) + [0] * n
    for i in range(len(data)):
        c = rem[i]
        if c:
            for j in range(len(g)):
                rem[i + j] ^= _mul(g[j], c)
    return rem[len(data):]


# ------------------------------------------------------------------ data

def _capacity(version):
    return sum(b * d for b, d in _M[version][1])


def _pick_version(n_bytes):
    for v in range(1, 11):
        if 4 + (8 if v < 10 else 16) + 8 * n_bytes <= 8 * _capacity(v):
            return v
    raise ValueError("text too long for this QR encoder (%d bytes)" % n_bytes)


def _codewords(data, version):
    ec_n, groups = _M[version]
    cap = _capacity(version)
    bits = []

    def put(value, n):
        bits.extend((value >> (n - 1 - i)) & 1 for i in range(n))
    put(0b0100, 4)                                       # byte mode
    put(len(data), 8 if version < 10 else 16)
    for byte in data:
        put(byte, 8)
    put(0, min(4, 8 * cap - len(bits)))                  # terminator
    bits.extend([0] * (-len(bits) % 8))
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < cap:
        words.append(pad)
        pad ^= 0xEC ^ 0x11

    blocks, k = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(words[k:k + size])
            k += size
    ecs = [_ec_codewords(b, ec_n) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):         # interleave data, then error correction
        out.extend(b[i] for b in blocks if i < len(b))
    for i in range(ec_n):
        out.extend(e[i] for e in ecs)
    return out


# ------------------------------------------------------------------ the matrix

def _format_bits(mask):
    data = (_EC_LEVEL_M << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _version_bits(version):
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | rem


def _draw_format(m, mask):
    size, bits = len(m), _format_bits(mask)
    bit = [(bits >> i) & 1 == 1 for i in range(15)]
    for i in range(6):
        m[i][8] = bit[i]
    m[7][8], m[8][8], m[8][7] = bit[6], bit[7], bit[8]
    for i in range(9, 15):
        m[8][14 - i] = bit[i]
    for i in range(8):
        m[8][size - 1 - i] = bit[i]
    for i in range(8, 15):
        m[size - 15 + i][8] = bit[i]
    m[size - 8][8] = True                                # the dark module


def _function_patterns(version):
    size = 17 + 4 * version
    m = [[None] * size for _ in range(size)]

    def finder(r0, c0):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if 0 <= r < size and 0 <= c < size:
                    inside = 0 <= dr <= 6 and 0 <= dc <= 6
                    m[r][c] = inside and (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4))
    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)
    for i in range(8, size - 8):
        m[6][i] = m[i][6] = i % 2 == 0
    pos = _ALIGN[version]
    for r in pos:
        for c in pos:
            if (r, c) in ((pos[0], pos[0]), (pos[0], pos[-1]), (pos[-1], pos[0])):
                continue                                 # these would sit on a finder pattern
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    m[r + dr][c + dc] = max(abs(dr), abs(dc)) != 1
    _draw_format(m, 0)                                   # reserves the format areas
    if version >= 7:
        vb = _version_bits(version)
        for i in range(18):
            bit = (vb >> i) & 1 == 1
            a, b = size - 11 + i % 3, i // 3
            m[b][a] = m[a][b] = bit
    return m


_MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _place_data(base, words, mask):
    size = len(base)
    m = [row[:] for row in base]
    bits = [(w >> (7 - i)) & 1 == 1 for w in words for i in range(8)]
    k, right = 0, size - 1
    while right >= 1:
        if right == 6:
            right = 5                                    # skip the vertical timing column
        upward = ((right + 1) & 2) == 0
        for vert in range(size):
            r = size - 1 - vert if upward else vert
            for c in (right, right - 1):
                if base[r][c] is None:
                    bit = bits[k] if k < len(bits) else False
                    k += 1
                    m[r][c] = bit != _MASKS[mask](r, c)
        right -= 2
    _draw_format(m, mask)
    return m


def _penalty(m):
    size, score = len(m), 0
    lines = m + [list(col) for col in zip(*m)]
    for line in lines:                                   # runs of five or more
        run = 1
        for i in range(1, size + 1):
            if i < size and line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run = 1
        s = "".join("1" if v else "0" for v in line)     # finder-like patterns
        score += 40 * (s.count("10111010000") + s.count("00001011101"))
    for r in range(size - 1):                            # 2x2 blocks of one colour
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(v for row in m for v in row) * 100 // (size * size)
    return score + 10 * (abs(dark - 50) // 5)


def encode(text, mask=None):
    """The QR code for `text`. `mask` forces one of the 8 masks (for tests); by default the best is chosen."""
    data = text.encode("utf-8")
    version = _pick_version(len(data))
    base = _function_patterns(version)
    words = _codewords(data, version)
    if mask is not None:
        return _place_data(base, words, mask)
    return min((_place_data(base, words, k) for k in range(8)), key=_penalty)
