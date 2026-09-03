"""
tokens.py — HMAC-based one-time request tokens.

Each session gets a random 16-byte hex secret (32 chars).
For counter N, token = first 10 hex of HMAC-SHA256(secret, str(N)) + hex checksum.

Validation checks a sliding window (current ±10) to tolerate drift/burst.
"""

import hmac
import hashlib
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Session expiry — sessions live 2 days, then must be refreshed (re-validate)
# ---------------------------------------------------------------------------

SESSION_TTL_DAYS = 2


def is_session_expired(sess: dict | None) -> bool:
    """True if a bngc_sessions row is older than SESSION_TTL_DAYS.

    Missing created_at (legacy rows) counts as NOT expired — only explicit
    timestamps expire, so old rows without dates keep working until re-linked.
    """

    if not sess:
        return False
    created = sess.get("created_at")
    if not created:
        return False
    try:
        ts = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400
        return age_days > SESSION_TTL_DAYS
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Legacy JS HMAC fallback (for old userscripts that used pure-JS SHA256)
# ---------------------------------------------------------------------------

_K = [
    1116352408, 1899447441, 3049323471, 3921009573, 961987163, 1508970993,
    2453635748, 2870763221, 3624381080, 310598401, 607225278, 1426881987,
    1925078388, 2162078206, 2614888103, 3248222580, 3835390401, 4022224774,
    264347078, 604807628, 770255983, 1249150122, 1555081692, 1996064986,
    2554220882, 2821834349, 2952996808, 3210313671, 3336571891, 3584528711,
    113926993, 338241895, 666307205, 773529912, 1294757372, 1396182291,
    1695183700, 1986661051, 2177026350, 2456956037, 2730485921, 2820302411,
    3259730800, 3345764771, 3516065817, 3600352804, 4094571909, 275423344,
    430227734, 506948616, 659060556, 883997877, 958139571, 1322822218,
    1537002063, 1747873779, 1955562222, 2024104815, 2227730452, 2361852424,
    2428436474, 2756734187, 3204031479, 3329325298,
]


def _rotr(value: int, shift: int) -> int:
    return ((value >> shift) | (value << (32 - shift))) & 0xFFFFFFFF


def _sha256_pure_js(message: str) -> str:
    """
    Pure-Python SHA256 matching the old JS implementation.
    Only used for legacy compat tokens.
    """

    # This is a direct port of the JS sha256Impl — kept for compat.
    h = [
        1779033703, 3144134277, 1013904242, 2773480762,
        1359893119, 2600822924, 528734635, 1541459225,
    ]

    # Pre-processing (padding)
    msg = message + "\x80"
    while len(msg) % 64 != 56:
        msg += "\x00"

    # Length in bits
    bit_len = len(message) * 8

    # Convert to words
    words = []
    for i in range(0, len(msg), 4):
        words.append(
            (ord(msg[i]) << 24)
            | (ord(msg[i + 1]) << 16)
            | (ord(msg[i + 2]) << 8)
            | ord(msg[i + 3])
        )

    words.append(bit_len >> 32)
    words.append(bit_len & 0xFFFFFFFF)

    # Process chunks
    for chunk_start in range(0, len(words), 16):
        w = words[chunk_start:chunk_start + 16]

        for j in range(16, 64):
            s0 = _rotr(w[j - 15], 7) ^ _rotr(w[j - 15], 18) ^ (w[j - 15] >> 3)
            s1 = _rotr(w[j - 2], 17) ^ _rotr(w[j - 2], 19) ^ (w[j - 2] >> 10)
            w.append((w[j - 16] + s0 + w[j - 7] + s1) & 0xFFFFFFFF)

        a, b, c, d, e, f, g, h2 = h

        for j in range(64):
            S1 = _rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25)
            ch = (e & f) ^ ((~e) & g)
            temp1 = (h2 + S1 + ch + _K[j] + w[j]) & 0xFFFFFFFF
            S0 = _rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)
            maj = (a & b) ^ (a & c) ^ (b & c)
            temp2 = (S0 + maj) & 0xFFFFFFFF

            h2 = g
            g = f
            f = e
            e = (d + temp1) & 0xFFFFFFFF
            d = c
            c = b
            b = a
            a = (temp1 + temp2) & 0xFFFFFFFF

        h = [(h[i] + [a, b, c, d, e, f, g, h2][i]) & 0xFFFFFFFF for i in range(8)]

    # Convert to hex
    result = ""
    for val in h:
        for shift in (24, 16, 8, 0):
            result += f"{(val >> shift) & 0xFF:02x}"

    return result


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def generate_request_token(secret: str, counter: int) -> str:
    """
    Generate a one-time token for a given secret and counter.

    Uses HMAC-SHA256 with the secret as key (hex-decoded) and str(counter)
    as message. Takes first 10 hex chars + hex checksum (Luhn-like).
    """

    # Secret is stored as 32-char hex (16 bytes)
    key = bytes.fromhex(secret)

    # HMAC-SHA256
    digest = hmac.new(key, str(counter).encode(), hashlib.sha256).hexdigest()

    # First 10 hex chars
    part = digest[:10]

    # Hex checksum (Luhn variant for hex)
    total = 0
    alt = False

    for ch in reversed(part):
        value = int(ch, 16)

        if alt:
            value *= 2
            if value > 15:
                value -= 15

        total += value
        alt = not alt

    checksum = (16 - (total % 16)) % 16

    return part + format(checksum, "x")


def generate_compat_token(secret: str, counter: int) -> str | None:
    """
    Legacy compat: generate token using the old pure-JS HMAC.
    Returns None on failure.
    """

    try:
        # Clean hex
        clean = "".join(c for c in secret if c in "0123456789abcdefABCDEF")

        # Build HMAC with pure-JS SHA256
        block_size = 64
        key_bytes = [int(clean[i:i+2], 16) for i in range(0, len(clean), 2)]

        if len(key_bytes) > block_size:
            hashed = _sha256_pure_js("".join(chr(b) for b in key_bytes))
            key_bytes = [int(hashed[i:i+2], 16) for i in range(0, len(hashed), 2)]

        while len(key_bytes) < block_size:
            key_bytes.append(0)

        o_key = [b ^ 0x5C for b in key_bytes]
        i_key = [b ^ 0x36 for b in key_bytes]

        inner = _sha256_pure_js("".join(chr(b) for b in i_key) + str(counter))
        inner_bytes = [int(inner[i:i+2], 16) for i in range(0, len(inner), 2)]

        outer = _sha256_pure_js("".join(chr(b) for b in o_key) + "".join(chr(b) for b in inner_bytes))

        part = outer[:10]

        total = 0
        alt = False

        for ch in reversed(part):
            value = int(ch, 16)
            if alt:
                value *= 2
                if value > 15:
                    value -= 15
            total += value
            alt = not alt

        checksum = (16 - (total % 16)) % 16

        return part + format(checksum, "x")

    except Exception:
        return None


def validate_request_token(secret: str, token: str, current_counter: int) -> dict:
    """
    Validate a token against a sliding window around current_counter.

    Checks current ±20 to tolerate drift from bursts.
    Returns {"valid": True, "matched_counter": c} or {"valid": False}.
    """

    # Check all candidates in drift window
    candidates = list(range(current_counter - 20, current_counter + 11))

    for counter in candidates:
        if counter < 0:
            continue

        # Primary (Python HMAC)
        if generate_request_token(secret, counter) == token:
            return {"valid": True, "matched_counter": counter}

        # Legacy compat
        compat = generate_compat_token(secret, counter)
        if compat and compat == token:
            return {"valid": True, "matched_counter": counter, "compat": True}

    return {"valid": False, "reason": "Token mismatch"}
