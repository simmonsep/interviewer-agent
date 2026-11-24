"""Windows Credential Manager storage for the user's own model API key.

The desktop app should not keep a provider secret as plaintext in model.json.
Windows ships a per-user credential vault, reachable from the standard library
through advapi32's CredWriteW / CredReadW / CredDeleteW. No new dependency, no
keyring package, no cloud service. On non-Windows hosts, or when the vault
refuses the write, callers fall back to the existing local file and report that
honestly in the UI.

Set INTERVIEW_KEY_VAULT=off to force the fallback (used by isolated tests).
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

TARGET = 'WorkBuddy/InterviewAgent/ModelKey'
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_USER = 2


class _Credential(ctypes.Structure):
    _fields_ = [
        ('Flags', wintypes.DWORD),
        ('Type', wintypes.DWORD),
        ('TargetName', wintypes.LPCWSTR),
        ('Comment', wintypes.LPCWSTR),
        ('LastWritten', wintypes.FILETIME),
        ('CredentialBlobSize', wintypes.DWORD),
        ('CredentialBlob', ctypes.POINTER(ctypes.c_char)),
        ('Persist', wintypes.DWORD),
        ('AttributeCount', wintypes.DWORD),
        ('Attributes', ctypes.c_void_p),
        ('TargetAlias', wintypes.LPCWSTR),
        ('UserName', wintypes.LPCWSTR),
    ]


def available() -> bool:
    return os.name == 'nt' and os.environ.get('INTERVIEW_KEY_VAULT', '').lower() != 'off'


def _api():
    if not available():
        return None
    try:
        api = ctypes.windll.advapi32
    except (AttributeError, OSError):
        return None
    api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(_Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    return api


def store(key: str, *, target: str = TARGET) -> bool:
    """Write a secret into the vault. Returns False on any failure."""
    api = _api()
    if api is None or not key:
        return False
    blob = key.encode('utf-8')
    buffer = ctypes.create_string_buffer(blob)
    value = _Credential()
    value.Type = CRED_TYPE_GENERIC
    value.TargetName = target
    value.CredentialBlobSize = len(blob)
    value.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
    value.Persist = CRED_PERSIST_LOCAL_USER
    value.UserName = 'desktop'
    try:
        return bool(api.CredWriteW(ctypes.byref(value), 0))
    except (OSError, ValueError):
        return False


def load(*, target: str = TARGET) -> str | None:
    api = _api()
    if api is None:
        return None
    pointer = ctypes.POINTER(_Credential)()
    try:
        if not api.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)) or not pointer:
            return None
        size = int(pointer.contents.CredentialBlobSize)
        if not size:
            return None
        return ctypes.string_at(pointer.contents.CredentialBlob, size).decode('utf-8')
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    finally:
        try:
            if pointer:
                api.CredFree(ctypes.cast(pointer, ctypes.c_void_p))
        except (OSError, ValueError):
            pass


def clear(*, target: str = TARGET) -> bool:
    api = _api()
    if api is None:
        return False
    try:
        return bool(api.CredDeleteW(target, CRED_TYPE_GENERIC, 0))
    except (OSError, ValueError):
        return False
