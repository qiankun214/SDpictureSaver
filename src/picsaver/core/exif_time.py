"""读取照片拍摄时间。

读取顺序（逐个尝试，第一个成功即返回）：

1. ``ExifRead``：JPG 与 TIFF 系 RAW（ARW / ORF）通用，主方案；
2. ``Pillow``：JPG 兜底；
3. 内置极简 TIFF/EXIF 读取器：ExifRead 失败时的最后兜底（纯标准库）。

> 实测结论：本机 ``rawpy 0.24.0`` **没有**拍摄时间接口（``RawPy`` 无 ``metadata`` /
> ``timestamp`` 属性），因此不用它取时间。详见 Agent.md 第 8 节。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# EXIF 时间标签：优先 DateTimeOriginal，其次 DateTimeDigitized，最后 DateTime
_EXIF_DATETIME_KEYS: tuple[str, ...] = (
    "EXIF DateTimeOriginal",
    "EXIF DateTimeDigitized",
    "Image DateTime",
)

# TIFF 标签编号
_TAG_DATETIME = 0x0132
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_DATETIME_DIGITIZED = 0x9004
_TAG_EXIF_IFD = 0x8769

_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}

_DATETIME_FORMATS = (
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y:%m:%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
)


@dataclass(frozen=True)
class CaptureTime:
    """一次成功的拍摄时间读取结果。"""

    value: datetime
    tool: str  # exifread / pillow / tiff
    tag: str  # 命中的标签名
    suffix: str  # 命中的文件后缀

    def describe(self) -> str:
        """例：``exifread:EXIF DateTimeOriginal（JPG）``"""
        return f"{self.tool}:{self.tag}（{self.suffix.upper()}）"


def parse_exif_datetime(text: str) -> datetime | None:
    """解析 EXIF 时间字符串；无法解析或为占位零值（``0000:00:00``）时返回 ``None``。"""
    raw = (text or "").strip().strip("\x00").strip()
    if not raw:
        return None
    if raw.startswith("0000:00:00") or raw.startswith("0000-00-00"):
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# 读取器 1：ExifRead
# --------------------------------------------------------------------------
def _from_exifread(path: Path, suffix: str) -> CaptureTime | None:
    import exifread

    with open(path, "rb") as handle:
        tags = exifread.process_file(
            handle, details=False, strict=False, stop_tag="EXIF DateTimeOriginal"
        )
    for key in _EXIF_DATETIME_KEYS:
        tag = tags.get(key)
        if tag is None:
            continue
        value = parse_exif_datetime(str(tag))
        if value is not None:
            return CaptureTime(value=value, tool="exifread", tag=key, suffix=suffix)
    return None


# --------------------------------------------------------------------------
# 读取器 2：Pillow（JPG 兜底）
# --------------------------------------------------------------------------
def _from_pillow(path: Path, suffix: str) -> CaptureTime | None:
    from PIL import Image

    with Image.open(path) as image:
        exif = image.getexif()
    for tag_id, name in ((36867, "EXIF DateTimeOriginal"), (36868, "EXIF DateTimeDigitized"), (306, "Image DateTime")):
        raw = exif.get(tag_id)
        if raw is None:
            continue
        value = parse_exif_datetime(str(raw))
        if value is not None:
            return CaptureTime(value=value, tool="pillow", tag=name, suffix=suffix)
    return None


# --------------------------------------------------------------------------
# 读取器 3：内置极简 TIFF/EXIF 读取器（纯标准库）
# --------------------------------------------------------------------------
def _read_ifd(handle, offset: int, endian: str, depth: int = 0) -> dict[int, bytes]:
    """读取一个 IFD，返回 ``{tag: 原始字节}``；只用于取 ASCII 时间标签。"""
    if depth > 3 or offset <= 0:
        return {}
    result: dict[int, bytes] = {}
    handle.seek(offset)
    header = handle.read(2)
    if len(header) < 2:
        return {}
    (count,) = struct.unpack(endian + "H", header)
    if count <= 0 or count > 512:
        return {}
    entries = handle.read(12 * count)
    if len(entries) < 12 * count:
        return {}
    for index in range(count):
        chunk = entries[index * 12 : index * 12 + 12]
        tag, ftype, num = struct.unpack(endian + "HHI", chunk[:8])
        size = _TYPE_SIZES.get(ftype, 0) * num
        if size <= 0:
            continue
        if size <= 4:
            payload = chunk[8 : 8 + size]
        else:
            (pointer,) = struct.unpack(endian + "I", chunk[8:12])
            if pointer <= 0:
                continue
            handle.seek(pointer)
            payload = handle.read(size)
        if ftype == 2:  # ASCII
            result[tag] = payload
        elif tag == _TAG_EXIF_IFD and ftype in (3, 4):
            (sub_offset,) = struct.unpack(endian + ("H" if ftype == 3 else "I"), chunk[8:12])
            result.update(_read_ifd(handle, sub_offset, endian, depth + 1))
    return result


def _from_tiff(path: Path, suffix: str) -> CaptureTime | None:
    """解析 TIFF 系文件（含 ARW / ORF）的 IFD0 与 EXIF 子 IFD。"""
    with open(path, "rb") as handle:
        header = handle.read(8)
        if len(header) < 8:
            return None
        order = header[:2]
        if order == b"II":
            endian = "<"
        elif order == b"MM":
            endian = ">"
        else:
            return None
        (magic,) = struct.unpack(endian + "H", header[2:4])
        # 42 = 标准 TIFF；0x4F52 = 'RO'（奥林巴斯 IIRO）；0x5352 = 'RS'（IIRS）；0x0055 = 部分 ORF
        if magic not in (42, 0x4F52, 0x5352, 0x0055):
            return None
        (first_ifd,) = struct.unpack(endian + "I", header[4:8])
        tags = _read_ifd(handle, first_ifd, endian)
    for tag_id, name in (
        (_TAG_DATETIME_ORIGINAL, "EXIF DateTimeOriginal"),
        (_TAG_DATETIME_DIGITIZED, "EXIF DateTimeDigitized"),
        (_TAG_DATETIME, "Image DateTime"),
    ):
        payload = tags.get(tag_id)
        if not payload:
            continue
        value = parse_exif_datetime(payload.decode("ascii", errors="ignore"))
        if value is not None:
            return CaptureTime(value=value, tool="tiff", tag=name, suffix=suffix)
    return None


#: 读取器顺序（与 Agent.md 第 8 节一致）
READERS = (
    ("exifread", _from_exifread),
    ("pillow", _from_pillow),
    ("tiff", _from_tiff),
)

#: 读取单个文件时允许尝试的读取器数量
READER_NAMES = tuple(name for name, _ in READERS)


def _active_readers():
    """按名称解析当前生效的读取器。

    每次调用都从模块全局取值，便于测试替换单个读取器。
    """
    return (
        ("exifread", _from_exifread),
        ("pillow", _from_pillow),
        ("tiff", _from_tiff),
    )


def read_capture_time(path: str | Path, suffix: str | None = None) -> CaptureTime | None:
    """读取一个照片文件的拍摄时间。

    :param path: 文件路径
    :param suffix: 小写后缀；省略时从文件名推导
    :return: :class:`CaptureTime`，全部读取器都失败时返回 ``None``
    """
    file_path = Path(path)
    ext = (suffix or file_path.suffix.lstrip(".")).lower()
    for _name, reader in _active_readers():
        try:
            found = reader(file_path, ext)
        except Exception:  # noqa: BLE001 - 单个读取器失败要继续尝试下一个
            found = None
        if found is not None:
            return found
    return None
