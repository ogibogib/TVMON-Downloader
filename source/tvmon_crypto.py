"""TVMon's ordinary HLS key decoding and Windows AES-128-CBC decryption."""

import base64
import binascii
import ctypes
from functools import lru_cache
import json
import os


def _base64(value):
    if not isinstance(value, str):
        raise ValueError("키 응답의 Base64 값이 올바르지 않습니다.")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4),
                                altchars=b"-_", validate=True)
    except (binascii.Error, UnicodeError, ValueError) as error:
        raise ValueError("키 응답의 Base64 값이 올바르지 않습니다.") from error


def _permutation(value, size):
    if (not isinstance(value, list) or len(value) != size
            or any(type(item) is not int for item in value)
            or sorted(value) != list(range(size))):
        raise ValueError("키 응답의 순열 값이 올바르지 않습니다.")
    return value


def decode_key(payload: bytes) -> bytes:
    """Accept the raw 16-byte key or the player's Level7 JSON response."""
    if not isinstance(payload, bytes):
        raise ValueError("키 응답은 바이트 데이터여야 합니다.")
    if len(payload) == 16:
        return payload
    try:
        response = json.loads(payload.decode("utf-8"))
        layers = {layer["name"]: layer for layer in response["layers"]}
        data = _base64(response["encrypted_key"])
        mask = _base64(layers["final_encrypt"]["xor_mask"])
        lengths = layers["decoy_shuffle"]["segment_lengths"]
        positions = layers["decoy_shuffle"]["real_positions"]
        previous = _base64(layers["xor_chain"]["init_key"])
        noise_perm = _permutation(layers["segment_noise"]["perm"], 16)
        rotations = layers["bit_rotate"]["rotations"]
        inverse = _base64(layers["sbox"]["inverse_sbox"])
        bit_perm = _permutation(layers["bit_interleave"]["perm"], 128)
        if (not mask or not previous or not isinstance(lengths, list)
                or not lengths or any(type(n) is not int or n <= 0 for n in lengths)
                or sum(lengths) > len(data)):
            raise ValueError("키 응답의 세그먼트 길이가 올바르지 않습니다.")
        if (not isinstance(positions, list) or len(positions) != 16
                or any(type(p) is not int or p < 0 or p >= len(lengths) for p in positions)
                or len(set(positions)) != 16):
            raise ValueError("키 응답의 세그먼트 위치가 올바르지 않습니다.")
        if (not isinstance(rotations, list) or not rotations
                or any(type(r) is not int or r < 0 for r in rotations)
                or len(inverse) != 256 or len(set(inverse)) != 256):
            raise ValueError("키 응답의 회전 또는 S-Box 값이 올바르지 않습니다.")

        combined = bytes(data[i] ^ mask[i % len(mask)] for i in range(sum(lengths)))
        segments = []
        offset = 0
        for length in lengths:
            segments.append(combined[offset:offset + length])
            offset += length
        layer3 = bytearray(16)
        for index, position in enumerate(positions):
            segment = segments[position]
            layer3[noise_perm[index]] = segment[0] ^ previous[0]
            previous = segment[:4]
        layer1 = bytearray(16)
        for index, value in enumerate(layer3):
            rotation = rotations[index % len(rotations)] % 8
            rotated = ((value >> rotation) | (value << (8 - rotation))) & 255
            layer1[index] = inverse[rotated]
        bits = [0] * 128
        for index, position in enumerate(bit_perm):
            bits[position] = (layer1[index >> 3] >> (7 - (index & 7))) & 1
        return bytes(sum(bits[index * 8 + bit] << (7 - bit) for bit in range(8))
                     for index in range(16))
    except (KeyError, TypeError, IndexError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("영상 키 응답이 올바른 Level7 형식이 아닙니다.") from error


@lru_cache(maxsize=1)
def _bcrypt():
    if os.name != "nt":
        raise RuntimeError("AES 영상 다운로드는 Windows에서 지원됩니다.")
    library = ctypes.WinDLL("bcrypt.dll")
    handle = ctypes.c_void_p
    ulong = ctypes.c_ulong
    pointer_ulong = ctypes.POINTER(ulong)
    pointer_handle = ctypes.POINTER(handle)
    library.BCryptOpenAlgorithmProvider.argtypes = [pointer_handle, ctypes.c_wchar_p,
                                                    ctypes.c_wchar_p, ulong]
    library.BCryptSetProperty.argtypes = [handle, ctypes.c_wchar_p, handle, ulong, ulong]
    library.BCryptGetProperty.argtypes = [handle, ctypes.c_wchar_p, handle,
                                        ulong, pointer_ulong, ulong]
    library.BCryptGenerateSymmetricKey.argtypes = [handle, pointer_handle, handle,
                                                 ulong, handle, ulong, ulong]
    library.BCryptDecrypt.argtypes = [handle, handle, ulong, handle, handle,
                                     ulong, handle, ulong, pointer_ulong, ulong]
    library.BCryptDestroyKey.argtypes = [handle]
    library.BCryptCloseAlgorithmProvider.argtypes = [handle, ulong]
    for name in ("BCryptOpenAlgorithmProvider", "BCryptSetProperty", "BCryptGetProperty",
                 "BCryptGenerateSymmetricKey", "BCryptDecrypt", "BCryptDestroyKey",
                 "BCryptCloseAlgorithmProvider"):
        getattr(library, name).restype = ctypes.c_long
    return library


def _check(status, operation):
    if status < 0:
        raise OSError(f"{operation} 실패 (Windows 상태 0x{status & 0xffffffff:08X})")


def decrypt_aes128(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt one PKCS#7-padded HLS segment using Windows BCrypt."""
    if not isinstance(key, bytes) or len(key) != 16:
        raise ValueError("AES-128 키는 정확히 16바이트여야 합니다.")
    if not isinstance(iv, bytes) or len(iv) != 16:
        raise ValueError("AES IV는 정확히 16바이트여야 합니다.")
    if (not isinstance(ciphertext, bytes) or not ciphertext
            or len(ciphertext) % 16 or len(ciphertext) > 0xffffffff):
        raise ValueError("암호화된 영상 조각 길이가 올바르지 않습니다.")
    library = _bcrypt()
    algorithm = ctypes.c_void_p()
    key_handle = ctypes.c_void_p()
    try:
        _check(library.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), "AES", None, 0),
               "AES 초기화")
        mode = ctypes.create_unicode_buffer("ChainingModeCBC")
        _check(library.BCryptSetProperty(algorithm, "ChainingMode", mode,
                                         ctypes.sizeof(mode), 0), "CBC 설정")
        object_length = ctypes.c_ulong()
        result_length = ctypes.c_ulong()
        _check(library.BCryptGetProperty(algorithm, "ObjectLength", ctypes.byref(object_length),
                                         ctypes.sizeof(object_length),
                                         ctypes.byref(result_length), 0), "AES 키 크기 확인")
        key_object = ctypes.create_string_buffer(object_length.value)
        key_buffer = ctypes.create_string_buffer(key, len(key))
        _check(library.BCryptGenerateSymmetricKey(algorithm, ctypes.byref(key_handle), key_object,
                                                  len(key_object), key_buffer, len(key), 0),
               "AES 키 생성")
        input_buffer = ctypes.create_string_buffer(ciphertext, len(ciphertext))
        iv_buffer = ctypes.create_string_buffer(iv, len(iv))
        output_buffer = ctypes.create_string_buffer(len(ciphertext))
        _check(library.BCryptDecrypt(key_handle, input_buffer, len(ciphertext), None,
                                     iv_buffer, len(iv), output_buffer, len(output_buffer),
                                     ctypes.byref(result_length), 0), "영상 복호화")
        plaintext = output_buffer.raw[:result_length.value]
        padding = plaintext[-1]
        if not 1 <= padding <= 16 or plaintext[-padding:] != bytes([padding]) * padding:
            raise ValueError("영상 조각의 AES 패딩이 올바르지 않습니다.")
        return plaintext[:-padding]
    finally:
        if key_handle.value:
            library.BCryptDestroyKey(key_handle)
        if algorithm.value:
            library.BCryptCloseAlgorithmProvider(algorithm, 0)
