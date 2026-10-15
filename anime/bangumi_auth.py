"""Per-Windows-user DPAPI vault. Tokens never enter SQLite, responses or logs."""
import ctypes
import json
import os
from pathlib import Path


def protect(data, decrypt=False):
    if os.name != 'nt':
        raise ValueError('Bangumi 凭据保存需要 Windows 用户加密环境')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt.CryptUnprotectData
        description = None
    else:
        fn = crypt.CryptProtectData
        description = 'AnimeService Bangumi'
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p if decrypt else wintypes.LPCWSTR,
                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), description, None, None, None, 1, ctypes.byref(result)):
        raise ValueError('Bangumi 凭据加密或读取失败，请在本机重新连接账号')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class TokenVault:
    def __init__(self, directory):
        self.path = Path(directory) / 'bangumi-auth.bin'

    def read(self):
        if not self.path.is_file():
            return None
        try:
            return json.loads(protect(self.path.read_bytes(), decrypt=True))
        except Exception:
            raise ValueError('Bangumi 凭据读取失败，请在本机重新连接账号') from None

    def save(self, credentials):
        encrypted = protect(json.dumps(credentials).encode('utf-8'))
        temporary = self.path.with_suffix('.tmp')
        temporary.write_bytes(encrypted)
        temporary.replace(self.path)

    def clear(self):
        self.path.unlink(missing_ok=True)
