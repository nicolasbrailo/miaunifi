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


if __name__ == "__main__":
    unittest.main()
