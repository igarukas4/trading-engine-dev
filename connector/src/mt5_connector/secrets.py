"""Secret providers backed by Windows Credential Manager or one safe file handle."""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from urllib.parse import unquote, urlparse


class SecretProviderError(RuntimeError):
    """Stable errors that never contain a secret or filesystem detail."""


def _read_posix_secret(path: Path) -> str:
    """Open and validate a file, then read that same descriptor."""
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)

    fd = None
    parent_fds: list[int] = []
    try:
        # Open each directory component without following a symlink. This also
        # prevents a replacement of a parent directory from redirecting the
        # final open. The final descriptor remains the only object read.
        parts = path.parts
        parent_fd = os.open(os.path.sep, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        parent_fds.append(parent_fd)
        for part in parts[1:-1]:
            if part in ("", "."):
                continue
            if part == "..":
                raise SecretProviderError("SECRET_FILE_UNSAFE")
            parent_fd = os.open(
                part,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            parent_fds.append(parent_fd)
        if not parts or parts[-1] in ("", ".", ".."):
            raise SecretProviderError("SECRET_FILE_UNSAFE")
        fd = os.open(parts[-1], flags, dir_fd=parent_fds[-1])
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise SecretProviderError("SECRET_FILE_UNSAFE")

        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        try:
            value = b"".join(chunks).decode("utf-8").strip()
        except UnicodeError as exc:
            raise SecretProviderError("SECRET_UNAVAILABLE") from exc
        if not value:
            raise SecretProviderError("SECRET_UNAVAILABLE")
        return value
    except SecretProviderError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SecretProviderError("SECRET_FILE_UNSAFE") from exc
        raise SecretProviderError("SECRET_UNAVAILABLE") from exc
    except ValueError as exc:
        raise SecretProviderError("SECRET_UNAVAILABLE") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        for parent in reversed(parent_fds):
            try:
                os.close(parent)
            except OSError:
                pass


def _read_windows_secret(path: str) -> str:
    """Read a regular, ACL-restricted Windows file from one non-reparse handle."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    HANDLE = wintypes.HANDLE
    INVALID_HANDLE_VALUE = HANDLE(-1).value
    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    SE_FILE_OBJECT = 1
    DACL_SECURITY_INFORMATION = 0x00000004
    ERROR_SUCCESS = 0
    ACCESS_ALLOWED_ACE_TYPE = 0
    ACCESS_DENIED_ACE_TYPE = 1
    BROAD_SIDS = {
        "S-1-1-0",       # Everyone
        "S-1-5-7",       # Anonymous Logon
        "S-1-5-11",      # Authenticated Users
        "S-1-5-32-545",  # Builtin Users
        "S-1-5-32-546",  # Builtin Guests
    }

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [("AceType", wintypes.BYTE), ("AceFlags", wintypes.BYTE), ("AceSize", wintypes.WORD)]

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, HANDLE]
    kernel32.CreateFileW.restype = HANDLE
    kernel32.CloseHandle.argtypes = [HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandle.argtypes = [HANDLE, ctypes.POINTER(FileInformation)]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel32.ReadFile.restype = wintypes.BOOL
    advapi32.GetSecurityInfo.argtypes = [HANDLE, wintypes.DWORD, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
                                          ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
                                          ctypes.POINTER(wintypes.LPVOID)]
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorDacl.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.BOOL),
                                                   ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.BOOL)]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    handle = kernel32.CreateFileW(
        path,
        GENERIC_READ,
        0,  # no sharing: a rename/delete cannot race this opened object
        None,
        OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    handle_value = getattr(handle, "value", handle)
    if not handle or handle_value == INVALID_HANDLE_VALUE:
        raise SecretProviderError("SECRET_UNAVAILABLE")
    security_descriptor = wintypes.LPVOID()
    try:
        info = FileInformation()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise SecretProviderError("SECRET_UNAVAILABLE")
        if info.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT):
            raise SecretProviderError("SECRET_FILE_UNSAFE")

        result = advapi32.GetSecurityInfo(
            handle,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
            ctypes.byref(security_descriptor),
        )
        if result != ERROR_SUCCESS or not security_descriptor:
            raise SecretProviderError("SECRET_FILE_ACL_UNAVAILABLE")
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl = wintypes.LPVOID()
        if not advapi32.GetSecurityDescriptorDacl(
            security_descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ) or not dacl_present.value or not dacl:
            raise SecretProviderError("SECRET_FILE_UNSAFE")
        acl_info = AclSizeInformation()
        if not advapi32.GetAclInformation(
            dacl, ctypes.byref(acl_info), ctypes.sizeof(acl_info), 2
        ):
            raise SecretProviderError("SECRET_FILE_ACL_UNAVAILABLE")
        for index in range(acl_info.AceCount):
            ace = wintypes.LPVOID()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)) or not ace:
                raise SecretProviderError("SECRET_FILE_ACL_UNAVAILABLE")
            header = ctypes.cast(ace, ctypes.POINTER(AceHeader)).contents
            if header.AceType not in (ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE):
                continue
            sid = ctypes.c_void_p(ace.value + ctypes.sizeof(AceHeader) + ctypes.sizeof(wintypes.DWORD))
            sid_text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
                raise SecretProviderError("SECRET_FILE_ACL_UNAVAILABLE")
            try:
                if header.AceType == ACCESS_ALLOWED_ACE_TYPE and sid_text.value.upper() in BROAD_SIDS:
                    raise SecretProviderError("SECRET_FILE_UNSAFE")
            finally:
                kernel32.LocalFree(sid_text)

        chunks: list[bytes] = []
        while True:
            buffer = ctypes.create_string_buffer(64 * 1024)
            read = wintypes.DWORD()
            if not kernel32.ReadFile(handle, buffer, len(buffer), ctypes.byref(read), None):
                raise SecretProviderError("SECRET_UNAVAILABLE")
            if not read.value:
                break
            chunks.append(buffer.raw[:read.value])
        try:
            value = b"".join(chunks).decode("utf-8").strip()
        except UnicodeError as exc:
            raise SecretProviderError("SECRET_UNAVAILABLE") from exc
        if not value:
            raise SecretProviderError("SECRET_UNAVAILABLE")
        return value
    finally:
        if security_descriptor:
            kernel32.LocalFree(security_descriptor)
        kernel32.CloseHandle(handle)


def protected_file_secret_provider(path: str):
    """Return a provider that verifies and reads one protected file handle."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise SecretProviderError("SECRET_FILE_MUST_BE_ABSOLUTE")

    def load() -> str:
        if os.name == "nt":
            return _read_windows_secret(str(candidate))
        return _read_posix_secret(candidate)

    load.securely_verified = True
    load.protected_file_provider = True
    return load


def _file_path_from_ref(parsed) -> str:
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        path = "//" + parsed.netloc + path
    if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    elif os.name == "nt" and path.startswith("/"):
        # A file URI without a drive uses the current Windows drive root.
        path = os.path.abspath(path)
    return path


def secret_provider_from_ref(secret_ref: str):
    """Build a securely verified provider for the configured protected store."""
    parsed = urlparse(secret_ref)
    scheme = parsed.scheme.lower()
    if scheme == "file":
        return protected_file_secret_provider(_file_path_from_ref(parsed))
    if scheme == "credential-manager":
        target = parsed.netloc or parsed.path.lstrip("/")
        if not target:
            raise SecretProviderError("SECRET_REFERENCE_INVALID")

        def load() -> str:
            try:
                import keyring
                value = keyring.get_password("mt5-connector", target)
            except Exception as exc:
                raise SecretProviderError("SECRET_UNAVAILABLE") from exc
            if not value:
                raise SecretProviderError("SECRET_UNAVAILABLE")
            return value

        load.securely_verified = True
        load.credential_manager_provider = True
        return load
    raise SecretProviderError("SECRET_REFERENCE_UNSUPPORTED")
