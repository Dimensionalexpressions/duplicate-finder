# ICU forwarder shim

Qt6Core.dll imports 20 *unversioned* ICU converter symbols (`ucnv_open`,
`ucnv_close`, ...). The ICU 78 DLLs we vendor with the MSYS2 OCR tools
export only the *versioned* names (`ucnv_open_78`, ...). On machines
without a plain-named System32 ICU (i.e. most of Windows 10, and Windows
11 only on recent builds) the frozen app therefore fails at the first
PySide6 import with WinError 127 ("The specified procedure could not be
found").

`icuuc.dll` in this directory is a 4 KB forwarder: it re-exports exactly
those 20 plain names and forwards each to `icuuc78.dll` (which sits next
to the OCR tools in the build output). Windows resolves the forwarding at
load time - no code, no dependencies of its own.

Rebuild (only needed if the set of forwarded symbols changes):

    clang --target=x86_64-w64-windows -c shimstub.c -o shimstub.o
    ld.lld -m i386pep -shared shimstub.o icushim.def -o icuuc.dll

`build_windows.py` copies `icuuc.dll` over the flat-copied MSYS2
`icuuc.dll` in the output directory.
