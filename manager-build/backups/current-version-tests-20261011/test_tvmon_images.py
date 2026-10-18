import base64
import os
from pathlib import Path
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch

import tvmon_images
from tvmon_images import thumbnail_ppm


# Fixed 8x4 RGB (220, 30, 50) images; no imaging library is needed by the tests.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAECAIAAAA8r+mnAAAAFElEQVR4nGO8I2fEgA0wYRUlSwIA"
    "dXYBNP+C1lwAAAAASUVORK5CYII=")
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAEAAgDASIA"
    "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
    "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
    "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
    "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDmKKKK"
    "8s/Uz//Z")
TRANSPARENT_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAYAAACp8Z5+AAAAFElEQVR4nGO8I2fEgAyYGNAAYQEA"
    "T1YBNCyBDwAAAAAASUVORK5CYII=")
HALF_ALPHA_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAECAYAAACzzX7wAAAAFUlEQVR4nGO8I2fUwIAHMOGTpI4C"
    "ANuWAbTpOba5AAAAAElFTkSuQmCC")
BACKGROUND = bytes((226, 232, 240))
WEBP = base64.b64decode(
    "UklGRjoAAABXRUJQVlA4IC4AAACQAQCdASoIAAQAAUAmJaACdLoAA5gA/vBTH+6L7UVQf/79pf+"
    "BO/+BO/6GAAAA")
# The same color with 50% alpha in lossless VP8L and extended VP8X containers.
LOSSLESS_WEBP = base64.b64decode("UklGRh4AAABXRUJQVlA4TBEAAAAvB8AAEAdQj3JXpoCBiOh/AAA=")
EXTENDED_WEBP = base64.b64decode(
    "UklGRl4AAABXRUJQVlA4WAoAAAAQAAAABwAAAwAAQUxQSAoAAAABB1DAiAhERP8DVlA4IC4AAACQ"
    "AQCdASoIAAQAAUAmJaACdLoAA5gA/vBTH+6L7UVQf/79pf+BO/+BO/6GAAAA")


class ImageValidationTests(unittest.TestCase):
    def test_invalid_data_and_output_dimensions(self):
        for data in (None, "image", bytearray(PNG), b"", b"not an image", PNG[:20], JPEG[:20]):
            with self.subTest(data=type(data).__name__), self.assertRaises(ValueError):
                thumbnail_ppm(data, 86, 112)
        for width, height in ((0, 10), (10, -1), (2049, 10), (10, 2049), (True, 5),
                              (5, False), (1.5, 10), (10, "20")):
            with self.subTest(size=(width, height)), self.assertRaises(ValueError):
                thumbnail_ppm(PNG, width, height)

    def test_resource_limits_precede_native_decoding(self):
        oversized_png = PNG[:16] + struct.pack(">II", 10000, 10000) + PNG[24:]
        oversized_jpeg = bytearray(JPEG)
        frame = oversized_jpeg.index(b"\xff\xc0")
        oversized_jpeg[frame + 5:frame + 9] = struct.pack(">HH", 10000, 10000)
        oversized_webp = (EXTENDED_WEBP[:24] + (9999).to_bytes(3, "little")
                          + (9999).to_bytes(3, "little") + EXTENDED_WEBP[30:])
        with patch("tvmon_images._gdiplus", side_effect=AssertionError("native decode called")):
            for data in (oversized_png, bytes(oversized_jpeg), oversized_webp,
                         b"x" * (16 * 1024 * 1024 + 1)):
                with self.assertRaises(ValueError):
                    thumbnail_ppm(data, 86, 112)

    def test_truncated_and_invalid_webp_containers(self):
        for data in (WEBP[:28], b"RIFF\x04\0\0\0WEBP", WEBP[:20] + bytes(len(WEBP) - 20)):
            with self.assertRaises(ValueError):
                thumbnail_ppm(data, 86, 112)


@unittest.skipUnless(os.name == "nt", "Windows GDI+ is required")
class NativeThumbnailTests(unittest.TestCase):
    def test_png_jpeg_webp_fit_letterbox_and_rgb_order(self):
        for data in (PNG, JPEG, WEBP):
            for width, height in ((86, 112), (128, 72), (32, 8), (1, 1), (4, 2)):
                with self.subTest(format=data[:2], size=(width, height)):
                    ppm = thumbnail_ppm(data, width, height)
                    header = f"P6\n{width} {height}\n255\n".encode("ascii")
                    self.assertTrue(ppm.startswith(header))
                    pixels = ppm[len(header):]
                    self.assertEqual(len(pixels), width * height * 3)
                    center = (height // 2 * width + width // 2) * 3
                    for actual, expected in zip(pixels[center:center + 3], (220, 30, 50)):
                        self.assertLessEqual(abs(actual - expected), 2)
                    if (width, height) in ((86, 112), (128, 72), (32, 8)):
                        self.assertEqual(pixels[:3], BACKGROUND)
                        self.assertEqual(pixels[-3:], BACKGROUND)

    def test_transparent_png_is_composited_onto_background(self):
        ppm = thumbnail_ppm(TRANSPARENT_PNG, 13, 9)
        self.assertEqual(ppm.split(b"\n", 3)[3], BACKGROUND * (13 * 9))
        pixels = thumbnail_ppm(HALF_ALPHA_PNG, 8, 4).split(b"\n", 3)[3]
        for actual, expected in zip(pixels[36:39], (223, 131, 145)):
            self.assertLessEqual(abs(actual - expected), 1)

    def test_webp_lossless_and_extended_alpha(self):
        for data in (LOSSLESS_WEBP, EXTENDED_WEBP):
            self.assertEqual(tvmon_images._source_size(data), (8, 4))
            pixels = thumbnail_ppm(data, 8, 4).split(b"\n", 3)[3]
            for actual, expected in zip(pixels[36:39], (223, 131, 145)):
                self.assertLessEqual(abs(actual - expected), 2)

    def test_webp_decode_failure_releases_com_objects(self):
        original = tvmon_images._com_method
        released = []

        def fail_copy_pixels(pointer, index, *arguments):
            if index == 7:
                return lambda *args: -2147467259  # E_FAIL from CopyPixels.
            if index == 2:
                released.append(pointer)
            return original(pointer, index, *arguments)

        with patch("tvmon_images._com_method", side_effect=fail_copy_pixels):
            with self.assertRaises(OSError):
                thumbnail_ppm(WEBP, 86, 112)
        self.assertEqual(len(released), 5)  # Converter, frame, decoder, factory, stream.
        self.assertTrue(thumbnail_ppm(WEBP, 86, 112).startswith(b"P6\n"))

    def test_real_downloaded_webp_poster(self):
        # Optional live fixture fetched through the existing downloader.fetch:
        # https://api3.apisvc10.com/data/peakl/thumb/39343afb04e0598f642641a6d4719924e87e66e6.webp
        fixture = Path(__file__).parent / "output" / "catalog" / "live-poster.webp"
        if not fixture.exists():
            self.skipTest("The optional live poster fixture has not been downloaded")
        data = fixture.read_bytes()
        self.assertEqual(tvmon_images._source_size(data), (240, 360))
        for width, height in ((86, 112), (128, 72)):
            pixels = thumbnail_ppm(data, width, height).split(b"\n", 3)[3]
            self.assertEqual(len(pixels), width * height * 3)
            self.assertGreater(len(set(pixels)), 100)

    def test_native_decode_failure_does_not_break_next_request(self):
        for _ in range(10):
            with self.assertRaises(OSError):
                thumbnail_ppm(PNG[:33], 86, 112)
        self.assertTrue(thumbnail_ppm(PNG, 86, 112).startswith(b"P6\n86 112\n"))

    def test_native_render_failure_releases_created_objects(self):
        thumbnail_ppm(PNG, 8, 4)
        library = tvmon_images._native[0]
        with patch.object(library, "GdipDrawImageRectRectI", return_value=2), \
                patch.object(library, "GdipDisposeImage", wraps=library.GdipDisposeImage) as images, \
                patch.object(library, "GdipDeleteGraphics", wraps=library.GdipDeleteGraphics) as graphics, \
                patch.object(library, "GdipDisposeImageAttributes",
                             wraps=library.GdipDisposeImageAttributes) as attributes:
            with self.assertRaises(OSError):
                thumbnail_ppm(PNG, 86, 112)
            self.assertEqual(images.call_count, 2)
            graphics.assert_called_once()
            attributes.assert_called_once()
        self.assertTrue(thumbnail_ppm(PNG, 86, 112).startswith(b"P6\n"))

    def test_simultaneous_first_use_in_fresh_process(self):
        code = """
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import ctypes
from test_tvmon_images import WEBP
from tvmon_images import thumbnail_ppm
barrier = Barrier(8)
def decode(index):
    ole = ctypes.WinDLL('ole32.dll')
    # Exercise both existing STA apartments and fresh worker threads.
    if index % 2:
        assert ole.CoInitializeEx(None, 2) >= 0
    barrier.wait()
    try:
        return thumbnail_ppm(WEBP, 86, 112)
    finally:
        if index % 2:
            ole.CoUninitialize()
with ThreadPoolExecutor(max_workers=8) as executor:
    results = list(executor.map(decode, range(8)))
assert len(set(results)) == 1
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, cwd=os.path.dirname(__file__), timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tk_photoimage_accepts_binary_ppm(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            photo = tk.PhotoImage(master=root, data=thumbnail_ppm(PNG, 86, 112), format="PPM")
            self.assertEqual((photo.width(), photo.height()), (86, 112))
            self.assertEqual(photo.get(43, 56), (220, 30, 50))
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
