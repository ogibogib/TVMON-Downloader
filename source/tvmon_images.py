"""Small JPEG/PNG/WebP thumbnails using Windows codecs and the standard library."""

import atexit
import ctypes
import os
import struct
import threading
import uuid


_MAX_INPUT_BYTES = 16 * 1024 * 1024
_MAX_SOURCE_PIXELS = 16 * 1024 * 1024
_MAX_SOURCE_SIDE = 16384
_MAX_OUTPUT_SIDE = 2048
_BACKGROUND = 0xFFE2E8F0
_PIXEL_FORMAT = 0x26200A  # PixelFormat32bppARGB (BGRA byte order on Windows).
_native_lock = threading.Lock()
_native = None
_closed = False


class _StartupInput(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint), ("callback", ctypes.c_void_p),
                ("suppress_thread", ctypes.c_int), ("suppress_codecs", ctypes.c_int)]


def _check(status, operation):
    if status:
        raise OSError(f"{operation} 실패 (GDI+ 상태 {status})")


def _check_source_size(width, height):
    if (not 0 < width <= _MAX_SOURCE_SIDE or not 0 < height <= _MAX_SOURCE_SIDE
            or width * height > _MAX_SOURCE_PIXELS):
        raise ValueError("이미지 크기가 너무 크거나 올바르지 않습니다.")


def _source_size(data):
    """Reject oversized images before asking the native decoder to allocate them."""
    if (data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 33
            and data[8:16] == b"\x00\x00\x00\rIHDR"):
        width, height = struct.unpack_from(">II", data, 16)
        _check_source_size(width, height)
        return width, height
    if data.startswith(b"\xff\xd8"):
        offset = 2
        while offset < len(data):
            if data[offset] != 0xff:
                break
            while offset < len(data) and data[offset] == 0xff:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in (0xda, 0xd9, 0x00):  # Scan, end, or invalid stuffed byte.
                break
            if marker == 0x01 or 0xd0 <= marker <= 0xd8:
                continue
            if offset + 2 > len(data):
                break
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 2 or offset + length > len(data):
                break
            if 0xc0 <= marker <= 0xcf and marker not in (0xc4, 0xc8, 0xcc):
                if length < 8:
                    break
                height, width = struct.unpack_from(">HH", data, offset + 3)
                _check_source_size(width, height)
                return width, height
            offset += length
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        file_end = int.from_bytes(data[4:8], "little") + 8
        offset = 12
        while offset + 8 <= file_end <= len(data):
            chunk = data[offset:offset + 4]
            length = int.from_bytes(data[offset + 4:offset + 8], "little")
            offset += 8
            if offset + length > file_end:
                break
            payload = data[offset:offset + min(length, 10)]
            if chunk == b"VP8X" and length >= 10:
                width = int.from_bytes(payload[4:7], "little") + 1
                height = int.from_bytes(payload[7:10], "little") + 1
            elif chunk == b"VP8L" and length >= 5 and payload[0] == 0x2f:
                bits = int.from_bytes(payload[1:5], "little")
                width, height = (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
            elif chunk == b"VP8 " and length >= 10 and payload[3:6] == b"\x9d\x01\x2a":
                width = int.from_bytes(payload[6:8], "little") & 0x3fff
                height = int.from_bytes(payload[8:10], "little") & 0x3fff
            else:
                offset += length + (length & 1)
                continue
            _check_source_size(width, height)
            return width, height
    raise ValueError("올바른 JPEG, PNG 또는 WebP 이미지가 아닙니다.")


def _com_method(pointer, index, *arguments):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *arguments)(vtable[0][index])


def _webp_pixels(stream, expected_size):
    """Decode WebP through the installed Windows WIC codec into straight BGRA."""
    handle = ctypes.c_void_p
    pointer_handle = ctypes.POINTER(handle)
    uint = ctypes.c_uint
    ole = ctypes.WinDLL("ole32.dll")
    ole.CoInitializeEx.argtypes = [handle, uint]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoCreateInstance.argtypes = [handle, handle, uint, handle, pointer_handle]
    ole.CoCreateInstance.restype = ctypes.c_long

    def check(status):
        if status < 0:
            raise OSError(f"Windows WebP 이미지 읽기 실패 (0x{status & 0xffffffff:08X})")

    initialized = ole.CoInitializeEx(None, 0)
    # RPC_E_CHANGED_MODE means this thread already has a usable COM apartment.
    if initialized != -2147417850:
        check(initialized)
    factory, decoder, frame, converter = (handle() for _ in range(4))
    try:
        factory_clsid = ctypes.create_string_buffer(
            uuid.UUID("cacaf262-9370-4615-a13b-9f5539da4c0a").bytes_le)
        factory_iid = ctypes.create_string_buffer(
            uuid.UUID("ec5ec8a9-c395-4314-9c77-54d7a935ff70").bytes_le)
        bgra = ctypes.create_string_buffer(
            uuid.UUID("6fddc324-4e03-4bfe-b185-3d77768dc90f").bytes_le)
        check(ole.CoCreateInstance(factory_clsid, None, 1, factory_iid, ctypes.byref(factory)))
        # IWICImagingFactory::CreateDecoderFromStream / IWICBitmapDecoder::GetFrame.
        check(_com_method(factory, 4, handle, handle, uint, pointer_handle)(
            factory, stream, None, 0, ctypes.byref(decoder)))
        check(_com_method(decoder, 13, uint, pointer_handle)(decoder, 0, ctypes.byref(frame)))
        width, height = uint(), uint()
        check(_com_method(frame, 3, ctypes.POINTER(uint), ctypes.POINTER(uint))(
            frame, ctypes.byref(width), ctypes.byref(height)))
        _check_source_size(width.value, height.value)
        if (width.value, height.value) != expected_size:
            raise ValueError("WebP 이미지 헤더 크기가 일치하지 않습니다.")
        # IWICFormatConverter::Initialize, then IWICBitmapSource::CopyPixels.
        check(_com_method(factory, 10, pointer_handle)(factory, ctypes.byref(converter)))
        check(_com_method(converter, 8, handle, handle, uint, handle, ctypes.c_double, uint)(
            converter, frame, bgra, 0, None, 0.0, 0))
        pixels = ctypes.create_string_buffer(width.value * height.value * 4)
        check(_com_method(converter, 7, handle, uint, uint, handle)(
            converter, None, width.value * 4, len(pixels), pixels))
        return pixels
    finally:
        for pointer in (converter, frame, decoder, factory):
            if pointer.value:
                _com_method(pointer, 2)(pointer)
        if initialized >= 0:
            ole.CoUninitialize()


def _gdiplus():
    """Called under _native_lock, including during process shutdown."""
    global _native
    if _closed:
        raise RuntimeError("이미지 처리가 종료되었습니다.")
    if _native is not None:
        return _native
    if os.name != "nt":
        raise RuntimeError("이미지 미리보기는 Windows에서 지원됩니다.")
    library = ctypes.WinDLL("gdiplus.dll")
    shell = ctypes.WinDLL("shlwapi.dll")
    handle = ctypes.c_void_p
    pointer_handle = ctypes.POINTER(handle)
    uint = ctypes.c_uint
    integer = ctypes.c_int
    library.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_size_t),
                                     ctypes.POINTER(_StartupInput), handle]
    library.GdiplusStartup.restype = integer
    library.GdiplusShutdown.argtypes = [ctypes.c_size_t]
    library.GdiplusShutdown.restype = None
    signatures = {
        "GdipCreateBitmapFromStream": [handle, pointer_handle],
        "GdipGetImageWidth": [handle, ctypes.POINTER(uint)],
        "GdipGetImageHeight": [handle, ctypes.POINTER(uint)],
        "GdipCreateBitmapFromScan0": [integer, integer, integer, integer,
                                       handle, pointer_handle],
        "GdipGetImageGraphicsContext": [handle, pointer_handle],
        "GdipGraphicsClear": [handle, uint],
        "GdipSetInterpolationMode": [handle, integer],
        "GdipCreateImageAttributes": [pointer_handle],
        "GdipSetImageAttributesWrapMode": [handle, integer, uint, integer],
        "GdipDrawImageRectRectI": [handle, handle] + [integer] * 9 + [handle] * 3,
        "GdipFlush": [handle, integer],
        "GdipDeleteGraphics": [handle],
        "GdipDisposeImage": [handle],
        "GdipDisposeImageAttributes": [handle],
    }
    for name, arguments in signatures.items():
        function = getattr(library, name)
        function.argtypes = arguments
        function.restype = integer
    shell.SHCreateMemStream.argtypes = [handle, uint]
    shell.SHCreateMemStream.restype = handle
    token = ctypes.c_size_t()
    startup = _StartupInput(1, None, 0, 1)
    _check(library.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup), None),
           "이미지 처리 초기화")
    _native = library, shell, token
    atexit.register(_shutdown)
    return _native


def _shutdown():
    global _native, _closed
    with _native_lock:
        _closed = True
        if _native is not None:
            library, _, token = _native
            library.GdiplusShutdown(token)
            _native = None


def thumbnail_ppm(data: bytes, width: int, height: int) -> bytes:
    """Fit a JPEG/PNG/WebP into exact dimensions on #e2e8f0; return Tk PPM bytes.

    Safe to call on background threads. Create Tk's PhotoImage from the returned
    bytes on the UI thread. Inputs are limited to 16 MiB and 16 megapixels;
    requested output sides must be between 1 and 2048 pixels. WebP uses the
    Windows WebP codec installed on the machine.
    """
    if not isinstance(data, bytes) or not 0 < len(data) <= _MAX_INPUT_BYTES:
        raise ValueError("이미지 데이터는 16 MiB 이하의 바이트여야 합니다.")
    if (type(width) is not int or type(height) is not int
            or not 0 < width <= _MAX_OUTPUT_SIDE or not 0 < height <= _MAX_OUTPUT_SIDE):
        raise ValueError("미리보기 크기는 1~2048 픽셀의 정수여야 합니다.")
    expected_size = _source_size(data)
    # Serializing these short native calls also keeps shutdown from racing a worker.
    with _native_lock:
        library, shell, _ = _gdiplus()
        input_buffer = ctypes.create_string_buffer(data, len(data))
        stream = shell.SHCreateMemStream(input_buffer, len(data))
        if not stream:
            raise MemoryError("이미지 메모리 스트림을 만들 수 없습니다.")
        source = ctypes.c_void_p()
        output = ctypes.c_void_p()
        graphics = ctypes.c_void_p()
        attributes = ctypes.c_void_p()
        try:
            if data.startswith(b"RIFF"):
                source_pixels = _webp_pixels(stream, expected_size)
                _check(library.GdipCreateBitmapFromScan0(
                    *expected_size, expected_size[0] * 4, _PIXEL_FORMAT, source_pixels,
                    ctypes.byref(source)), "WebP 이미지 생성")
            else:
                _check(library.GdipCreateBitmapFromStream(stream, ctypes.byref(source)),
                       "이미지 읽기")
            source_width = ctypes.c_uint()
            source_height = ctypes.c_uint()
            _check(library.GdipGetImageWidth(source, ctypes.byref(source_width)),
                   "이미지 너비 읽기")
            _check(library.GdipGetImageHeight(source, ctypes.byref(source_height)),
                   "이미지 높이 읽기")
            actual_size = source_width.value, source_height.value
            _check_source_size(*actual_size)
            if actual_size != expected_size:
                raise ValueError("이미지 헤더 크기가 일치하지 않습니다.")
            scale = min(width / actual_size[0], height / actual_size[1])
            fitted_width = max(1, min(width, round(actual_size[0] * scale)))
            fitted_height = max(1, min(height, round(actual_size[1] * scale)))
            pixels = ctypes.create_string_buffer(width * height * 4)
            _check(library.GdipCreateBitmapFromScan0(width, height, width * 4,
                                                    _PIXEL_FORMAT, pixels,
                                                    ctypes.byref(output)), "미리보기 생성")
            _check(library.GdipGetImageGraphicsContext(output, ctypes.byref(graphics)),
                   "미리보기 그리기 준비")
            _check(library.GdipGraphicsClear(graphics, _BACKGROUND), "배경 그리기")
            _check(library.GdipSetInterpolationMode(graphics, 7), "이미지 축소 설정")
            _check(library.GdipCreateImageAttributes(ctypes.byref(attributes)),
                   "이미지 가장자리 설정 준비")
            # TileFlipXY prevents bicubic filtering from blending beyond image edges.
            _check(library.GdipSetImageAttributesWrapMode(attributes, 3, 0, 0),
                   "이미지 가장자리 설정")
            _check(library.GdipDrawImageRectRectI(
                graphics, source, (width - fitted_width) // 2, (height - fitted_height) // 2,
                fitted_width, fitted_height, 0, 0, *actual_size, 2, attributes, None, None),
                "미리보기 그리기")
            _check(library.GdipFlush(graphics, 1), "미리보기 완료")
            raw = pixels.raw
            rgb = bytearray(width * height * 3)
            rgb[0::3], rgb[1::3], rgb[2::3] = raw[2::4], raw[1::4], raw[0::4]
            return f"P6\n{width} {height}\n255\n".encode("ascii") + rgb
        finally:
            if graphics.value:
                library.GdipDeleteGraphics(graphics)
            if output.value:
                library.GdipDisposeImage(output)
            if attributes.value:
                library.GdipDisposeImageAttributes(attributes)
            if source.value:
                library.GdipDisposeImage(source)
            # IUnknown::Release is slot 2; the stream must outlive the source bitmap.
            _com_method(stream, 2)(stream)
