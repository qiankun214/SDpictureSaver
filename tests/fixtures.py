"""测试素材构造工具：合成带 EXIF 拍摄时间的 JPG 与 TIFF 系 RAW（ARW / ORF）。

真实的 SD 卡照片不放仓库，测试全部使用这里合成的素材。
"""

from __future__ import annotations

import io
import struct
from datetime import datetime
from pathlib import Path

from PIL import Image

#: 用于 ``make_pair`` 的哨兵：区分"没传参数"与"显式要求 JPG 不带 EXIF 时间"
UNSET = object()


def exif_datetime(dt: datetime) -> str:
    """把 ``datetime`` 转成 EXIF 时间字符串。"""
    return dt.strftime("%Y:%m:%d %H:%M:%S")


def make_jpeg(path: Path, dt: datetime | None, *, size: tuple[int, int] = (24, 16)) -> Path:
    """生成一张带 EXIF 的 JPG；``dt=None`` 时生成完全没有 EXIF 的 JPG。

    做法与真实相机一致：Pillow 生成图像数据，再手工注入 APP1/EXIF 段
    （``DateTimeOriginal`` 位于 Exif 子 IFD，``Image DateTime`` 位于 IFD0）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, (120, 130, 140))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=70)
    data = buffer.getvalue()
    if dt is not None:
        payload = b"Exif\x00\x00" + tiff_bytes(dt, include_ifd0_datetime=True)
        app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
        data = data[:2] + app1 + data[2:]  # 紧跟 SOI 插入 APP1
    path.write_bytes(data)
    return path


def tiff_bytes(
    dt: datetime,
    *,
    big_endian: bool = False,
    orf: bool = False,
    include_ifd0_datetime: bool = True,
) -> bytes:
    """构造一个最小的 TIFF / ORF 结构，IFD0 与 EXIF 子 IFD 都带时间标签。"""
    endian = ">" if big_endian else "<"
    order = b"MM" if big_endian else b"II"
    magic = 0x4F52 if orf else 42  # 0x4F52 == b"RO"，奥林巴斯 IIRO 标记
    text = exif_datetime(dt).encode("ascii") + b"\x00"  # 20 字节

    ifd0_offset = 8
    ifd0_entries = 2 if include_ifd0_datetime else 1
    ifd0_size = 2 + 12 * ifd0_entries + 4
    datetime_offset = ifd0_offset + ifd0_size
    exif_ifd_offset = datetime_offset + len(text)
    exif_ifd_size = 2 + 12 * 1 + 4
    original_offset = exif_ifd_offset + exif_ifd_size

    buffer = bytearray()
    buffer += order + struct.pack(endian + "H", magic) + struct.pack(endian + "I", ifd0_offset)

    def entry(tag: int, ftype: int, count: int, value: int) -> bytes:
        return struct.pack(endian + "HHI", tag, ftype, count) + struct.pack(endian + "I", value)

    # IFD0
    buffer += struct.pack(endian + "H", ifd0_entries)
    if include_ifd0_datetime:
        buffer += entry(0x0132, 2, len(text), datetime_offset)  # Image DateTime
    buffer += entry(0x8769, 4, 1, exif_ifd_offset)  # Exif IFD 指针
    buffer += struct.pack(endian + "I", 0)
    assert len(buffer) == datetime_offset
    if include_ifd0_datetime:
        buffer += text
        assert len(buffer) == exif_ifd_offset
    # EXIF 子 IFD
    buffer += struct.pack(endian + "H", 1)
    buffer += entry(0x9003, 2, len(text), original_offset)  # DateTimeOriginal
    buffer += struct.pack(endian + "I", 0)
    assert len(buffer) == original_offset
    buffer += text
    return bytes(buffer)


def make_raw(path: Path, dt: datetime | None, *, big_endian: bool = False) -> Path:
    """生成一个 TIFF 系 RAW 文件（.arw / .orf）。

    ``dt=None`` 时写入不含任何时间标签的最小 TIFF。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if dt is None:
        endian = "<"
        data = b"II" + struct.pack("<H", 42) + struct.pack("<I", 8) + struct.pack("<H", 0) + struct.pack("<I", 0)
        path.write_bytes(data)
        return path
    orf = path.suffix.lower() == ".orf"
    path.write_bytes(tiff_bytes(dt, big_endian=big_endian, orf=orf))
    return path


def make_pair(
    directory: Path,
    stem: str,
    dt: datetime | None,
    *,
    suffixes: tuple[str, ...] = ("jpg", "arw"),
    jpeg_time=UNSET,
) -> list[Path]:
    """在同一目录下生成同名的 jpg / raw 组合。

    :param dt: 写入 RAW 的拍摄时间（``None`` 表示不写）
    :param jpeg_time: 写入 JPG 的拍摄时间；不传则与 ``dt`` 相同，显式传 ``None`` 表示 JPG 不带 EXIF
    """
    directory.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for suffix in suffixes:
        if suffix in ("jpg", "jpeg"):
            jpeg_dt = dt if jpeg_time is UNSET else jpeg_time
            created.append(make_jpeg(directory / f"{stem}.{suffix}", jpeg_dt))
        else:
            created.append(make_raw(directory / f"{stem}.{suffix}", dt))
    return created
