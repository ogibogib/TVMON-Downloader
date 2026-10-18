import base64
import json
import os
import unittest

from tvmon_crypto import decode_key, decrypt_aes128


def level7_fixture(key):
    """Apply the player's seven forward layers, including decoys and padding."""
    def encode(data):
        return base64.urlsafe_b64encode(bytes(data)).decode("ascii").rstrip("=")

    bit_perm = [(index * 37 + 11) % 128 for index in range(128)]
    key_bits = [(byte >> (7 - bit)) & 1 for byte in key for bit in range(8)]
    layer1 = [sum(key_bits[bit_perm[index * 8 + bit]] << (7 - bit) for bit in range(8))
              for index in range(16)]
    sbox = [(index * 197 + 43) % 256 for index in range(256)]
    inverse = [0] * 256
    for index, value in enumerate(sbox):
        inverse[value] = index
    rotations = [0, 1, 2, 3, 4, 5, 6, 7]
    layer3 = []
    for index, value in enumerate(layer1):
        value = sbox[value]
        rotation = rotations[index % len(rotations)]
        layer3.append(((value << rotation) | (value >> (8 - rotation))) & 255)
    noise_perm = [(index * 5 + 3) % 16 for index in range(16)]
    initial = b"\xf3\x27\x99\x14"
    previous = initial
    chained = []
    for index, position in enumerate(noise_perm):
        noisy = bytes([layer3[position]]) + bytes(range(1, 2 + index % 5))
        segment = bytes(value ^ previous[offset % len(previous)]
                        for offset, value in enumerate(noisy))
        chained.append(segment)
        previous = segment[:4]
    segments = [b"decoy"]
    positions = []
    for segment in reversed(chained):
        segments.append(segment)
        positions.insert(0, len(segments) - 1)
        segments.append(b"noise")
    mask = b"\x83\x21\xf9"
    combined = b"".join(segments)
    encrypted = bytes(value ^ mask[index % len(mask)] for index, value in enumerate(combined))
    return {
        "encrypted_key": encode(encrypted + b"ignored padding"),
        "layers": [
            {"name": "bit_interleave", "perm": bit_perm},
            {"name": "sbox", "inverse_sbox": encode(inverse)},
            {"name": "bit_rotate", "rotations": rotations},
            {"name": "segment_noise", "perm": noise_perm},
            {"name": "xor_chain", "init_key": encode(initial)},
            {"name": "decoy_shuffle", "segment_lengths": [len(s) for s in segments],
             "real_positions": positions},
            {"name": "final_encrypt", "xor_mask": encode(mask)},
        ],
    }


class KeyDecodeTests(unittest.TestCase):
    def test_raw_key(self):
        key = bytes(range(16))
        self.assertEqual(decode_key(key), key)

    def test_seven_layers(self):
        for key in (bytes(range(16)), bytes(16), bytes([255]) * 16):
            with self.subTest(key=key.hex()):
                payload = json.dumps(level7_fixture(key)).encode("utf-8")
                self.assertEqual(decode_key(payload), key)

    def test_malformed_json(self):
        for payload in (b"", b"{}", b"[]", b"not JSON", b"\xff"):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    decode_key(payload)

    def test_malformed_layers(self):
        for name, field, invalid in (
                ("bit_interleave", "perm", [0] * 128),
                ("segment_noise", "perm", list(range(15))),
                ("final_encrypt", "xor_mask", ""),
                ("xor_chain", "init_key", "!!!"),
                ("decoy_shuffle", "segment_lengths", [999999]),
                ("decoy_shuffle", "real_positions", [0] * 16),
                ("bit_rotate", "rotations", []),
                ("sbox", "inverse_sbox", "AA")):
            with self.subTest(name=name, field=field):
                fixture = level7_fixture(bytes(range(16)))
                for layer in fixture["layers"]:
                    if layer["name"] == name:
                        layer[field] = invalid
                with self.assertRaises(ValueError):
                    decode_key(json.dumps(fixture).encode("utf-8"))


class AesDecryptTests(unittest.TestCase):
    KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    IV = bytes(range(16))
    # NIST SP 800-38A first CBC plaintext block, followed by a full PKCS#7 block.
    CIPHERTEXT = bytes.fromhex("7649abac8119b246cee98e9b12e9197d8964e0b149c10b7b682e6e39aaeb731c")
    PLAINTEXT = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")

    @unittest.skipUnless(os.name == "nt", "Windows BCrypt is required")
    def test_fixed_cbc_vector_and_padding(self):
        self.assertEqual(decrypt_aes128(self.CIPHERTEXT, self.KEY, self.IV), self.PLAINTEXT)
        self.assertEqual(decrypt_aes128(self.CIPHERTEXT, self.KEY, self.IV), self.PLAINTEXT)

    @unittest.skipUnless(os.name == "nt", "Windows BCrypt is required")
    def test_invalid_padding(self):
        with self.assertRaises(ValueError):
            decrypt_aes128(self.CIPHERTEXT[:16], self.KEY, self.IV)

    def test_invalid_key_iv_and_ciphertext(self):
        for ciphertext, key, iv in ((self.CIPHERTEXT, b"short", self.IV),
                                     (self.CIPHERTEXT, self.KEY, bytes(15)),
                                     (b"", self.KEY, self.IV),
                                     (bytes(17), self.KEY, self.IV)):
            with self.subTest(lengths=(len(ciphertext), len(key), len(iv))):
                with self.assertRaises(ValueError):
                    decrypt_aes128(ciphertext, key, iv)


if __name__ == "__main__":
    unittest.main()
