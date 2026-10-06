import importlib

import rawpy

raw = rawpy.RawPy
names = [n for n in dir(raw) if not n.startswith("_")]
print("RawPy attrs:", ", ".join(names))
print("has metadata:", hasattr(raw, "metadata"))
print("has timestamp:", hasattr(raw, "timestamp"))
