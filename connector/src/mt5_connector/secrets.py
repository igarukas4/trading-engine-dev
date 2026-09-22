"""Secret providers backed by Windows Credential Manager or one safe file handle."""
from __future__ import annotations

import errno
import ntpath
import os
import stat
from pathlib import Path
from urllib.parse import unquote, urlparse


class SecretProviderError(RuntimeError):
    """Stable errors that never contain a secret or filesystem detail."""


_PROVIDER_TOKEN = object()


class _ProtectedSecretProvider:
    """Callable created only by a protected-store factory."""

    __slots__ = ("_loader", "_kind")

    def __init__(self, token, loader, kind: str):
        if token is not _PROVIDER_TOKEN:
            raise TypeError("protected provider must come from its factory")
        object.__setattr__(self, "_loader", loader)
        object.__setattr__(self, "_kind", kind)

    def __setattr__(self, name, value):
        raise AttributeError("protected provider is immutable")

    def __call__(self) -> str:
        try:
            value = self._loader()
        except SecretProviderError:
            raise
        except Exception as exc:
            raise SecretProviderError("SECRET_UNAVAILABLE") from exc
        if not isinstance(value, str) or not value.strip():
            raise SecretProviderError("SECRET_UNAVAILABLE")
        return value

    @property
    def securely_verified(self) -> bool:
        return True

    @property
    def protected_file_provider(self) -> bool:
        return self._kind == "file"

    @property
    def credential_manager_provider(self) -> bool:
        return self._kind == "credential-manager"


def is_protected_secret_provider(value) -> bool:
    """Return whether a provider came from a protected secret-store factory."""
    return isinstance(value, _ProtectedSecretProvider)


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


def _windows_path_prefixes(path: str) -> tuple[list[str], str]:
    """Split an absolute Windows path into prefixes and its final component."""
    normalized = ntpath.normpath(path)
    drive, tail = ntpath.splitdrive(normalized)
    if not drive or not tail.startswith("\\"):
        raise SecretProviderError("SECRET_FILE_MUST_BE_ABSOLUTE")
    parts = [part for part in tail.split("\\") if part]
    if not parts:
        raise SecretProviderError("SECRET_FILE_UNSAFE")
    if drive.startswith("\\\\"):
        server_share = drive.rstrip("\\")
        root = server_share + "\\"
    else:
        root = drive.rstrip("\\") + "\\"
    prefixes = [root]
    for part in parts[:-1]:
        if part in {".", ".."}:
            raise SecretProviderError("SECRET_FILE_UNSAFE")
        prefixes.append(ntpath.join(prefixes[-1], part))
    final = ntpath.join(prefixes[-1], parts[-1])
    return prefixes, final


def _read_windows_secret_unchecked(path: str, *, windows_api=None) -> str:
    """Read an ACL-restricted file after checking every parent component."""
    import ctypes
    from ctypes import wintypes

    if windows_api is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    else:
        kernel32, advapi32 = windows_api
    HANDLE = wintypes.HANDLE
    INVALID_HANDLE_VALUE = HANDLE(-1).value
    GENERIC_READ = 0x80000000
    FILE_READ_ATTRIBUTES = 0x00000080
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    FILE_TYPE_DISK = 0x0001
    OPEN_EXISTING = 3
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
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
    kernel32.GetFileType.argtypes = [HANDLE]
    kernel32.GetFileType.restype = wintypes.DWORD
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

    try:
        parents, final_path = _windows_path_prefixes(path)
    except SecretProviderError:
        raise

    # OPEN_REPARSE_POINT applies only to the final component of one call.
    # Validate every parent with a separate handle before opening the file.
    # A junction in any existing component therefore fails closed instead of
    # redirecting the final CreateFileW call outside the configured tree.
    parent_handles = []
    handle = None
    try:
        for parent in parents:
            handle = kernel32.CreateFileW(
                parent,
                FILE_READ_ATTRIBUTES,
                # Keep the directory identity pinned through final open/read.
                # Read/write sharing remains allowed for normal consumers.
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
            handle_value = getattr(handle, "value", handle)
            if not handle or handle_value in (INVALID_HANDLE_VALUE, -1):
                raise SecretProviderError("SECRET_UNAVAILABLE")
            parent_handles.append(handle)
            info = FileInformation()
            if (not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)) or
                    not info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY or
                    info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT):
                raise SecretProviderError("SECRET_FILE_UNSAFE")

        handle = kernel32.CreateFileW(
            final_path,
            GENERIC_READ,
            0,  # no sharing: a rename/delete cannot race this opened object
            None,
            OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
    except BaseException:
        for parent_handle in reversed(parent_handles):
            try:
                kernel32.CloseHandle(parent_handle)
            except Exception:
                pass
        raise
    handle_value = getattr(handle, "value", handle)
    security_descriptor = wintypes.LPVOID()
    try:
        if not handle or handle_value in (INVALID_HANDLE_VALUE, -1):
            raise SecretProviderError("SECRET_UNAVAILABLE")
        if kernel32.GetFileType(handle) != FILE_TYPE_DISK:
            raise SecretProviderError("SECRET_FILE_UNSAFE")
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
            # The parser intentionally understands only basic allow/deny ACEs.
            # Object, callback, compound, and other variants can carry access
            # semantics this small parser cannot prove safe.
            if header.AceType not in (ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE):
                raise SecretProviderError("SECRET_FILE_UNSAFE")
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
        if handle and handle_value not in (INVALID_HANDLE_VALUE, -1):
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
        for parent_handle in reversed(parent_handles):
            try:
                kernel32.CloseHandle(parent_handle)
            except Exception:
                pass


def _read_windows_secret(path: str) -> str:
    try:
        return _read_windows_secret_unchecked(path)
    except SecretProviderError:
        raise
    except (OSError, ValueError, ImportError, AttributeError, TypeError) as exc:
        raise SecretProviderError("SECRET_UNAVAILABLE") from exc
    except Exception as exc:
        # ctypes can surface platform-specific exceptions that do not share a
        # stable public type. Never expose those details to the runtime.
        raise SecretProviderError("SECRET_UNAVAILABLE") from exc


def protected_file_secret_provider(path: str):
    """Return a provider that verifies and reads one protected file handle."""
    try:
        candidate = Path(path).expanduser()
    except (TypeError, ValueError) as exc:
        raise SecretProviderError("SECRET_REFERENCE_INVALID") from exc
    if not candidate.is_absolute():
        raise SecretProviderError("SECRET_FILE_MUST_BE_ABSOLUTE")

    def load() -> str:
        if os.name == "nt":
            return _read_windows_secret(str(candidate))
        return _read_posix_secret(candidate)

    return _ProtectedSecretProvider(_PROVIDER_TOKEN, load, "file")


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


def _read_windows_credential(target: str) -> str:
    """Read one generic credential through the native Windows API."""
    if os.name != "nt":
        raise SecretProviderError("SECRET_UNAVAILABLE")
    import ctypes
    from ctypes import wintypes

    class Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                   wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None
    credential = ctypes.POINTER(Credential)()
    CRED_TYPE_GENERIC = 1
    if not advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(credential)):
        raise SecretProviderError("SECRET_UNAVAILABLE")
    try:
        item = credential.contents
        if not item.CredentialBlob or not item.CredentialBlobSize:
            raise SecretProviderError("SECRET_UNAVAILABLE")
        raw = ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize)
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeError as exc:
            raise SecretProviderError("SECRET_UNAVAILABLE") from exc
        if not value:
            raise SecretProviderError("SECRET_UNAVAILABLE")
        return value
    finally:
        try:
            advapi32.CredFree(credential)
        except Exception:
            pass


def secret_provider_from_ref(secret_ref: str):
    """Build a securely verified provider for the configured protected store."""
    try:
        parsed = urlparse(secret_ref)
    except (TypeError, ValueError) as exc:
        raise SecretProviderError("SECRET_REFERENCE_INVALID") from exc
    scheme = parsed.scheme.lower()
    if scheme == "file":
        return protected_file_secret_provider(_file_path_from_ref(parsed))
    if scheme == "credential-manager":
        target = unquote(parsed.netloc or parsed.path.lstrip("/"))
        if not target:
            raise SecretProviderError("SECRET_REFERENCE_INVALID")

        return _ProtectedSecretProvider(
            _PROVIDER_TOKEN,
            lambda: _read_windows_credential(target),
            "credential-manager",
        )
    raise SecretProviderError("SECRET_REFERENCE_UNSUPPORTED")
