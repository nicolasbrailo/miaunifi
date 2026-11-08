import unittest

from miaunifi import packet

MAC = "02:11:22:33:44:55"


class TestPacket(unittest.TestCase):
    def test_roundtrip_cbc(self):
        raw = packet.encode(MAC, b'{"hello": "world"}')
        self.assertEqual(raw[:4], b"TNBU")
        mac, flags, data = packet.decode(raw)
        self.assertEqual(mac, MAC)
        self.assertEqual(flags, packet.FLAG_ENCRYPTED | packet.FLAG_ZLIB)
        self.assertEqual(data, b'{"hello": "world"}')

    def test_roundtrip_gcm(self):
        raw = packet.encode(MAC, b'{"a": 1}', gcm=True)
        _, flags, data = packet.decode(raw)
        self.assertTrue(flags & packet.FLAG_GCM)
        self.assertEqual(data, b'{"a": 1}')

    def test_default_key(self):
        self.assertEqual(packet.DEFAULT_KEY.hex(), "ba86f2bbe107c7c57eb5f2690775c712")

    def test_bad_magic(self):
        raw = b"XXXX" + packet.encode(MAC, b"{}")[4:]
        with self.assertRaises(packet.PacketError):
            packet.decode(raw)

    def test_wrong_key(self):
        for gcm in (False, True):
            raw = packet.encode(MAC, b'{"a": 1}', key=b"k" * 16, gcm=gcm)
            with self.assertRaises(packet.PacketError):
                packet.decode(raw)

    def test_bad_zlib(self):
        raw = bytearray(packet.encode(MAC, b"{}"))
        # Clear FLAG_ENCRYPTED (flags are at offset 14): the ciphertext goes straight to zlib
        raw[14:16] = packet.FLAG_ZLIB.to_bytes(2, "big")
        with self.assertRaises(packet.PacketError):
            packet.decode(bytes(raw))


if __name__ == "__main__":
    unittest.main()
