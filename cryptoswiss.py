#!/usr/bin/env python3
"""
CryptoSwiss - the all-in-one encode/decode/identify Swiss-army knife.

Auto-detects and decodes chained encodings (Base64, Base32, Base58, Base85,
Hex, Binary, URL-encoding, Morse) and classical ciphers (Caesar/ROT-N via
brute-force + chi-squared scoring, Atbash), and identifies common hash types
by length/format (hashes are one-way, so identification only - no cracking).
Can also encode plaintext into any supported format on demand.

Usage:
    python3 cryptoswiss.py "SGVsbG8sIFdvcmxkIQ=="        # auto-detect & decode
    python3 cryptoswiss.py -f cipher.txt                    # read input from file
    python3 cryptoswiss.py -e base64 "Hello, World!"         # encode to a format
    python3 cryptoswiss.py -e caesar -k 7 "Hello"             # encode w/ a cipher key
    python3 cryptoswiss.py --identify 5f4dcc3b5aa765d61d8327deb882cf99
"""

import argparse
import base64
import binascii
import codecs
import re
import string
import urllib.parse

# ============================== Helpers ===============================

ENGLISH_FREQ = {
    'a': 8.17, 'b': 1.49, 'c': 2.78, 'd': 4.25, 'e': 12.70, 'f': 2.23,
    'g': 2.02, 'h': 6.09, 'i': 6.97, 'j': 0.15, 'k': 0.77, 'l': 4.03,
    'm': 2.41, 'n': 6.75, 'o': 7.51, 'p': 1.93, 'q': 0.10, 'r': 5.99,
    's': 6.33, 't': 9.06, 'u': 2.76, 'v': 0.98, 'w': 2.36, 'x': 0.15,
    'y': 1.97, 'z': 0.07,
}

MORSE_MAP = {
    'A': '.-', 'B': '-...', 'C': '-.-.', 'D': '-..', 'E': '.', 'F': '..-.',
    'G': '--.', 'H': '....', 'I': '..', 'J': '.---', 'K': '-.-', 'L': '.-..',
    'M': '--', 'N': '-.', 'O': '---', 'P': '.--.', 'Q': '--.-', 'R': '.-.',
    'S': '...', 'T': '-', 'U': '..-', 'V': '...-', 'W': '.--', 'X': '-..-',
    'Y': '-.--', 'Z': '--..', '0': '-----', '1': '.----', '2': '..---',
    '3': '...--', '4': '....-', '5': '.....', '6': '-....', '7': '--...',
    '8': '---..', '9': '----.',
}
MORSE_MAP_INV = {v: k for k, v in MORSE_MAP.items()}
B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def printable_ratio(s):
    if not s:
        return 0.0
    ok = sum(1 for c in s if c in string.printable)
    return ok / len(s)


def chi_squared(text):
    text = text.lower()
    letters = [c for c in text if c in string.ascii_lowercase]
    n = len(letters)
    if n == 0:
        return float('inf')
    counts = {c: 0 for c in string.ascii_lowercase}
    for c in letters:
        counts[c] += 1
    score = 0.0
    for c in string.ascii_lowercase:
        expected = ENGLISH_FREQ[c] / 100.0 * n
        if expected > 0:
            score += (counts[c] - expected) ** 2 / expected
    return score


def mostly_printable(s):
    """Loose gate used while chaining decodes: is this even text-like (not raw binary junk)?"""
    if not s:
        return False
    return printable_ratio(s) > 0.85


COMMON_WORDS = {
    "the", "and", "hello", "world", "is", "you", "to", "a", "of", "in", "that",
    "it", "for", "on", "with", "as", "this", "be", "at", "by", "an", "are",
    "or", "not", "have", "from", "but", "what", "all", "were", "when", "we",
    "there", "can", "will", "each", "which", "do", "how", "their", "if", "up",
    "other", "about", "out", "many", "then", "them", "so", "her", "would",
    "make", "like", "him", "into", "time", "has", "look", "two", "more",
    "write", "go", "see", "number", "no", "way", "could", "people", "my",
    "than", "first", "been", "who", "its", "now", "find", "long", "down",
    "day", "did", "get", "made", "may", "part", "help", "sos", "flag", "key",
    "secret", "password", "message", "test", "hack", "code", "ctf",
}


def word_score(text):
    """Count how many whitespace-separated tokens are recognizable common English words."""
    words = re.findall(r"[a-zA-Z']+", text.lower())
    return sum(1 for w in words if w in COMMON_WORDS)


def looks_like_plaintext(s):
    """Stricter check used for the FINAL result: mostly printable and has real letter content."""
    if printable_ratio(s) < 0.95:
        return False
    letters = [c for c in s.lower() if c in string.ascii_lowercase]
    if len(letters) < max(3, len(s) * 0.3):
        return False
    return True


# ============================== Codecs ==================================
# Each decoder returns decoded str, or raises on failure.

def hex_decode(s):
    cleaned = re.sub(r'[\s:]', '', s)
    return binascii.unhexlify(cleaned).decode('utf-8')


def hex_encode(s):
    return s.encode('utf-8').hex()


def b64_decode(s):
    cleaned = s.strip()
    cleaned += '=' * (-len(cleaned) % 4)
    return base64.b64decode(cleaned).decode('utf-8')


def b64_encode(s):
    return base64.b64encode(s.encode('utf-8')).decode('ascii')


def b32_decode(s):
    cleaned = s.strip().upper()
    cleaned += '=' * (-len(cleaned) % 8)
    return base64.b32decode(cleaned).decode('utf-8')


def b32_encode(s):
    return base64.b32encode(s.encode('utf-8')).decode('ascii')


def b85_decode(s):
    return base64.b85decode(s.strip()).decode('utf-8')


def b85_encode(s):
    return base64.b85encode(s.encode('utf-8')).decode('ascii')


def b58_decode(s):
    s = s.strip()
    num = 0
    for c in s:
        num = num * 58 + B58_ALPHABET.index(c)
    raw = num.to_bytes((num.bit_length() + 7) // 8, 'big')
    pad = len(s) - len(s.lstrip('1'))
    return (b'\x00' * pad + raw).decode('utf-8')


def b58_encode(s):
    data = s.encode('utf-8')
    num = int.from_bytes(data, 'big')
    out = ''
    while num > 0:
        num, rem = divmod(num, 58)
        out = B58_ALPHABET[rem] + out
    pad = len(data) - len(data.lstrip(b'\x00'))
    return B58_ALPHABET[0] * pad + out


def binary_decode(s):
    bits = re.sub(r'[\s]', '', s)
    if len(bits) % 8 != 0:
        raise ValueError("not byte-aligned")
    chars = [chr(int(bits[i:i + 8], 2)) for i in range(0, len(bits), 8)]
    return ''.join(chars)


def binary_encode(s):
    return ' '.join(format(ord(c), '08b') for c in s)


def url_decode(s):
    decoded = urllib.parse.unquote(s)
    if decoded == s:
        raise ValueError("no percent-escapes found")
    return decoded


def url_encode(s):
    return urllib.parse.quote(s)


def rot13(s):
    return codecs.encode(s, 'rot13')


def atbash(s):
    def flip(c):
        if c.isupper():
            return chr(ord('Z') - (ord(c) - ord('A')))
        if c.islower():
            return chr(ord('z') - (ord(c) - ord('a')))
        return c
    return ''.join(flip(c) for c in s)


def morse_decode(s):
    words = s.strip().split('/')
    out_words = []
    for w in words:
        letters = w.strip().split()
        out_words.append(''.join(MORSE_MAP_INV.get(l, '?') for l in letters))
    return ' '.join(out_words)


def morse_encode(s):
    words = s.upper().split(' ')
    return ' / '.join(' '.join(MORSE_MAP.get(c, '') for c in w if c in MORSE_MAP) for w in words)


def reverse_text(s):
    return s[::-1]


def caesar_shift(s, key):
    def shift_char(c):
        if c.isupper():
            return chr((ord(c) - ord('A') - key) % 26 + ord('A'))
        if c.islower():
            return chr((ord(c) - ord('a') - key) % 26 + ord('a'))
        return c
    return ''.join(shift_char(c) for c in s)


def caesar_crack(s):
    """
    Brute force all 26 shifts. Score = chi-squared letter-frequency distance,
    minus a heavy bonus for recognizable English words - the word bonus
    dominates on short strings where frequency stats alone are unreliable.
    Returns (best_key, best_text, best_combined_score).
    """
    best = None
    for key in range(26):
        candidate = caesar_shift(s, key)
        combined = chi_squared(candidate) - word_score(candidate) * 100
        if best is None or combined < best[2]:
            best = (key, candidate, combined)
    return best


# ============================ Detection ==================================

def detect_and_decode_one_step(s):
    """
    Try decoders roughly from most-specific pattern to least-specific.
    Returns (method_name, decoded_str) for the first plausible hit, or None.
    """
    stripped = s.strip()

    # Morse: only dots, dashes, slashes, whitespace
    if re.fullmatch(r'[.\-/\s]+', stripped) and '.' in stripped or '-' in stripped:
        if re.fullmatch(r'[.\-/\s]+', stripped):
            try:
                decoded = morse_decode(stripped)
                if decoded and mostly_printable(decoded):
                    return ("Morse code", decoded)
            except Exception:
                pass

    # Binary: only 0/1 and whitespace, byte-aligned
    if re.fullmatch(r'[01\s]+', stripped):
        try:
            decoded = binary_decode(stripped)
            if mostly_printable(decoded):
                return ("Binary", decoded)
        except Exception:
            pass

    # Hex: even-length hex digits
    if re.fullmatch(r'[0-9a-fA-F\s:]+', stripped) and len(re.sub(r'[\s:]', '', stripped)) % 2 == 0:
        try:
            decoded = hex_decode(stripped)
            if mostly_printable(decoded):
                return ("Hex", decoded)
        except Exception:
            pass

    # URL-encoding
    if '%' in stripped:
        try:
            decoded = url_decode(stripped)
            if mostly_printable(decoded):
                return ("URL-encoding", decoded)
        except Exception:
            pass

    # Base64
    if re.fullmatch(r'[A-Za-z0-9+/]+=*', stripped) and len(stripped) >= 4:
        try:
            decoded = b64_decode(stripped)
            if mostly_printable(decoded):
                return ("Base64", decoded)
        except Exception:
            pass

    # Base32
    if re.fullmatch(r'[A-Z2-7=]+', stripped.upper()) and len(stripped) >= 8:
        try:
            decoded = b32_decode(stripped)
            if mostly_printable(decoded):
                return ("Base32", decoded)
        except Exception:
            pass

    # Base85 (Z85/b85 alphabet is broad; try last since it overlaps others)
    if re.fullmatch(r'[0-9A-Za-z!#$%&()*+\-;<=>?@^_`{|}~]+', stripped) and len(stripped) >= 4:
        try:
            decoded = b85_decode(stripped)
            if mostly_printable(decoded):
                return ("Base85", decoded)
        except Exception:
            pass

    # Base58 (bitcoin alphabet - excludes 0, O, I, l)
    if re.fullmatch(r'[' + re.escape(B58_ALPHABET) + r']+', stripped) and len(stripped) >= 4:
        try:
            decoded = b58_decode(stripped)
            if mostly_printable(decoded):
                return ("Base58", decoded)
        except Exception:
            pass

    return None


def try_classical_cipher(s):
    """Try Atbash and Caesar/ROT-N as a last resort on alphabetic-heavy text."""
    letters = [c for c in s.lower() if c in string.ascii_lowercase]
    if len(letters) < 6:
        return None

    def combined(text):
        return chi_squared(text) - word_score(text) * 100

    baseline_score = combined(s)

    atbash_text = atbash(s)
    atbash_score = combined(atbash_text)

    key, caesar_text, caesar_score = caesar_crack(s)

    best_method, best_text, best_score = "none", s, baseline_score
    if atbash_score < best_score:
        best_method, best_text, best_score = "Atbash", atbash_text, atbash_score
    if caesar_score < best_score and key != 0:
        best_method, best_text, best_score = f"Caesar (key={key})", caesar_text, caesar_score

    # Only report if meaningfully better than leaving the text alone
    if best_method != "none" and (word_score(best_text) > word_score(s) or best_score < baseline_score - 5):
        return (best_method, best_text)
    return None


HASH_PATTERNS = [
    (32, "MD5 / NTLM / MD4"),
    (40, "SHA-1"),
    (56, "SHA-224 / SHA3-224"),
    (64, "SHA-256 / SHA3-256"),
    (96, "SHA-384 / SHA3-384"),
    (128, "SHA-512 / SHA3-512"),
]


def identify_hash(s):
    s = s.strip()
    if re.fullmatch(r'[a-fA-F0-9]+', s):
        for length, name in HASH_PATTERNS:
            if len(s) == length:
                return name
    if s.startswith(("$2a$", "$2b$", "$2y$")):
        return "bcrypt"
    if s.startswith("$1$"):
        return "MD5 crypt (Unix)"
    if s.startswith("$5$"):
        return "SHA-256 crypt (Unix)"
    if s.startswith("$6$"):
        return "SHA-512 crypt (Unix)"
    if re.fullmatch(r'[A-Za-z0-9+/]{20}==', s) or re.fullmatch(r'[A-Za-z0-9+/]{27}=', s):
        return "possibly a base64-encoded digest (SHA-1/MD5)"
    return None


def auto_decode(s, max_depth=6):
    """Chain-decode as far as possible, then attempt classical cipher cracking."""
    trace = []
    current = s.strip()

    hash_guess = identify_hash(current)
    if hash_guess:
        return trace, hash_guess, current

    for _ in range(max_depth):
        result = detect_and_decode_one_step(current)
        if result is None:
            break
        method, decoded = result
        if decoded.strip() == current.strip():
            break
        trace.append((method, decoded))
        current = decoded

    cipher_result = try_classical_cipher(current)
    if cipher_result:
        method, decoded = cipher_result
        trace.append((method, decoded))
        current = decoded

    return trace, None, current


# ============================== Encoders =================================

ENCODERS = {
    "hex": hex_encode,
    "base64": b64_encode,
    "base32": b32_encode,
    "base85": b85_encode,
    "base58": b58_encode,
    "binary": binary_encode,
    "url": url_encode,
    "rot13": rot13,
    "atbash": atbash,
    "morse": morse_encode,
    "reverse": reverse_text,
}


def encode_with_key(fmt, text, key):
    if fmt == "caesar":
        if key is None:
            raise ValueError("caesar encoding requires -k/--key")
        return caesar_shift(text, -key)  # encrypt = shift forward
    if fmt not in ENCODERS:
        raise ValueError(f"unknown format '{fmt}'. Options: {', '.join(list(ENCODERS) + ['caesar'])}")
    return ENCODERS[fmt](text)


# ================================= CLI ====================================

def main():
    parser = argparse.ArgumentParser(description="All-in-one encode/decode/identify tool.")
    parser.add_argument("text", nargs="?", help="Input text (or omit to be prompted / use -f)")
    parser.add_argument("-f", "--file", help="Read input from a file")
    parser.add_argument("-e", "--encode", metavar="FORMAT",
                         help="Encode instead of decode. Formats: " + ", ".join(list(ENCODERS) + ["caesar"]))
    parser.add_argument("-k", "--key", type=int, help="Key for caesar encoding, or force a specific Caesar key when decoding")
    parser.add_argument("--identify", action="store_true", help="Only identify (don't attempt to decode)")
    args = parser.parse_args()

    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            data = fh.read().strip()
    elif args.text:
        data = args.text
    else:
        data = input("Enter input: ").strip()

    if args.encode:
        print(encode_with_key(args.encode, data, args.key))
        return

    if args.identify:
        hash_guess = identify_hash(data)
        if hash_guess:
            print(f"Looks like a hash: {hash_guess} (hashes are one-way - identification only, not reversible)")
        else:
            print("No confident hash/encoding pattern match.")
        return

    if args.key is not None:
        print(caesar_shift(data, args.key))
        return

    trace, hash_guess, final = auto_decode(data)

    if hash_guess:
        print(f"Looks like a hash: {hash_guess}")
        print("Hashes are one-way functions - identification only, no reversing/cracking performed.")
        return

    if not trace:
        print("Could not confidently identify any encoding or cipher.")
        print(f"Input as-is: {data}")
        return

    print("Decode chain:")
    current_input = data
    for step, (method, decoded) in enumerate(trace, 1):
        print(f"  {step}. [{method}]  {current_input!r} -> {decoded!r}")
        current_input = decoded

    print(f"\nFinal result: {final}")


if __name__ == "__main__":
    main()
