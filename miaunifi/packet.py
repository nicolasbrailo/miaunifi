"""UniFi inform packet encoding/decoding.

Layout (big endian):
    magic       4s   b"TNBU"
    version     I    packet version, 0
    mac         6s   device MAC
    flags       H    see FLAG_*
    iv          16s
    data_ver    I    payload version, 1
    data_len    I
    payload     data_len bytes

The payload is JSON, compressed, then encrypted. Until adoption the key is
md5("ubnt"); after adoption the controller hands out a per-device authkey.
"""

import hashlib
import os
import struct
import zlib

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"TNBU"
HEADER = struct.Struct(">4sI6sH16sII")

FLAG_ENCRYPTED = 0x01
FLAG_ZLIB = 0x02
FLAG_SNAPPY = 0x04
FLAG_GCM = 0x08

DEFAULT_KEY = hashlib.md5(b"ubnt").digest()


class PacketError(Exception):
    pass


def mac_to_bytes(mac):
    return bytes.fromhex(mac.replace(":", ""))


def bytes_to_mac(raw):
    return ":".join(f"{b:02x}" for b in raw)


def encode(mac, payload, key=DEFAULT_KEY, gcm=False):
    """Build an inform packet from a JSON payload (bytes)."""
    iv = os.urandom(16)
    data = zlib.compress(payload)
    flags = FLAG_ENCRYPTED | FLAG_ZLIB
    if gcm:
        flags |= FLAG_GCM
        # The tag adds 16 bytes; the header (with the final length) is the AAD
        header = HEADER.pack(MAGIC, 0, mac_to_bytes(mac), flags, iv, 1, len(data) + 16)
        data = AESGCM(key).encrypt(iv, data, header)
    else:
        padder = padding.PKCS7(128).padder()
        data = padder.update(data) + padder.finalize()
        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        data = enc.update(data) + enc.finalize()
        header = HEADER.pack(MAGIC, 0, mac_to_bytes(mac), flags, iv, 1, len(data))
    return header + data


def decode(packet, key=DEFAULT_KEY):
    """Parse an inform packet. Returns (mac, flags, payload bytes)."""
    if len(packet) < HEADER.size:
        raise PacketError(f"packet too short ({len(packet)} bytes)")
    magic, _ver, mac, flags, iv, _data_ver, data_len = HEADER.unpack_from(packet)
    if magic != MAGIC:
        raise PacketError(f"bad magic {magic!r}")
    header = packet[:HEADER.size]
    data = packet[HEADER.size:HEADER.size + data_len]
    if len(data) != data_len:
        raise PacketError(f"truncated payload ({len(data)}/{data_len} bytes)")

    if flags & FLAG_ENCRYPTED:
        if flags & FLAG_GCM:
            data = AESGCM(key).decrypt(iv, data, header)
        else:
            dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            data = dec.update(data) + dec.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            data = unpadder.update(data) + unpadder.finalize()

    if flags & FLAG_SNAPPY:
        raise PacketError("snappy compression not supported")
    if flags & FLAG_ZLIB:
        data = zlib.decompress(data)

    return bytes_to_mac(mac), flags, data
