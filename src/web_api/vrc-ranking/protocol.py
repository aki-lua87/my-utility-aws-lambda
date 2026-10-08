"""Versioned lightweight Udon-compatible XTEA-CTR + SipHash-2-4 envelope.

Deters casual editing; shared client keys do not prove a genuine VRChat run.
Keys: 32 bytes, encryption key first, authentication key second. All integers LE.
"""

import hmac
import json
import re
import struct

MASK32 = (1 << 32) - 1
MASK64 = (1 << 64) - 1
WORLD = re.compile(r"wrld_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")


def siphash(data: bytes, key: bytes) -> bytes:
    k0, k1 = struct.unpack("<QQ", key)
    v = [0x736F6D6570736575 ^ k0, 0x646F72616E646F6D ^ k1, 0x6C7967656E657261 ^ k0, 0x7465646279746573 ^ k1]

    def rotate(x: int, n: int) -> int:
        return ((x << n) | (x >> (64 - n))) & MASK64

    def rounds(count: int) -> None:
        for _ in range(count):
            v[0] = (v[0] + v[1]) & MASK64
            v[1] = rotate(v[1], 13) ^ v[0]
            v[0] = rotate(v[0], 32)
            v[2] = (v[2] + v[3]) & MASK64
            v[3] = rotate(v[3], 16) ^ v[2]
            v[0] = (v[0] + v[3]) & MASK64
            v[3] = rotate(v[3], 21) ^ v[0]
            v[2] = (v[2] + v[1]) & MASK64
            v[1] = rotate(v[1], 17) ^ v[2]
            v[2] = rotate(v[2], 32)

    end = len(data) // 8 * 8
    for offset in range(0, end, 8):
        m = int.from_bytes(data[offset : offset + 8], "little")
        v[3] ^= m
        rounds(2)
        v[0] ^= m
    last = ((len(data) & 255) << 56) | int.from_bytes(data[end:], "little")
    v[3] ^= last
    rounds(2)
    v[0] ^= last
    v[2] ^= 255
    rounds(4)
    return struct.pack("<Q", v[0] ^ v[1] ^ v[2] ^ v[3])


def crypt(data: bytes, key: bytes, nonce: bytes) -> bytes:
    words = struct.unpack("<IIII", key)
    counter = int.from_bytes(nonce, "little")
    result = bytearray(data)
    for offset in range(0, len(data), 8):
        v0, v1 = struct.unpack("<II", struct.pack("<Q", counter))
        total = 0
        for _ in range(32):
            v0 = (v0 + ((((v1 << 4) ^ (v1 >> 5)) + v1) ^ (total + words[total & 3]))) & MASK32
            total = (total + 0x9E3779B9) & MASK32
            v1 = (v1 + ((((v0 << 4) ^ (v0 >> 5)) + v0) ^ (total + words[(total >> 11) & 3]))) & MASK32
        stream = struct.pack("<II", v0, v1)
        for i in range(min(8, len(data) - offset)):
            result[offset + i] ^= stream[i]
        counter = (counter + 1) & MASK64
    return bytes(result)


def encode(record: dict, world_id: str, key_id: str, key_hex: str, nonce: bytes) -> str:
    key = bytes.fromhex(key_hex)
    cipher = crypt(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode(), key[:16], nonce)
    message = f"1.{world_id}.{key_id}.{nonce.hex()}.{cipher.hex()}"
    return message + "." + siphash(message.encode(), key[16:]).hex()


def decode(payload: str, worlds: dict) -> tuple[str, dict, dict]:
    if not isinstance(payload, str) or not 1 <= len(payload) <= 4096:
        raise ValueError("invalid_payload")
    parts = payload.split(".")
    if len(parts) != 6 or parts[0] != "1" or not WORLD.fullmatch(parts[1]) or not IDENTIFIER.fullmatch(parts[2]):
        raise ValueError("invalid_payload")
    _, world_id, key_id, nonce_hex, cipher_hex, tag_hex = parts
    config = worlds.get(world_id)
    if config is None:
        raise ValueError("unknown_world")
    key_hex = config.get("keys", {}).get(key_id)
    if key_hex is None:
        raise ValueError("invalid_payload")
    key, nonce, cipher, tag = map(bytes.fromhex, (key_hex, nonce_hex, cipher_hex, tag_hex))
    if len(key) != 32 or len(nonce) != 8 or len(tag) != 8 or not cipher:
        raise ValueError("invalid_payload")
    if not hmac.compare_digest(tag, siphash(".".join(parts[:-1]).encode(), key[16:])):
        raise ValueError("invalid_payload")
    record = json.loads(crypt(cipher, key[:16], nonce).decode("utf-8"))
    if not isinstance(record, dict):
        raise ValueError("invalid_record")
    return world_id, record, config
