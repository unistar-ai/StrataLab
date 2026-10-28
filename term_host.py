"""Pseudo-console host for Strata Lab.

ConPTY has to live in a small dedicated process: created inside the HTTP server it did not
reliably attach the child, and its stdio pipes misbehaved (writes from the reader thread
failed with EINVAL). So this process talks to app.py through files instead — boring, but
nothing can go wrong with handle inheritance:

    <base>.out   host writes raw pty bytes here, app.py reads them by offset
    <base>.in    app.py appends keystrokes here, host feeds them to the pty
    <base>.ctl   lines: "<cols>x<rows>" to resize, "stop" to shut down
    <base>.alive app.py touches this while it is running; gone -> the host exits

CLI: term_host.py <kind> <cols> <rows> <cwd> <base>

Only ctypes: CreatePseudoConsole + PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE. No pywinpty.
"""
import ctypes
import ctypes.wintypes as wt
import msvcrt
import os
import sys
import threading
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD), ("dwXCountChars", wt.DWORD),
        ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wt.BOOL)]


k32.CreatePseudoConsole.argtypes = [COORD, wt.HANDLE, wt.HANDLE, wt.DWORD,
                                    ctypes.POINTER(wt.HANDLE)]
k32.CreatePseudoConsole.restype = ctypes.c_long
k32.ResizePseudoConsole.argtypes = [wt.HANDLE, COORD]
k32.CreatePipe.argtypes = [ctypes.POINTER(wt.HANDLE), ctypes.POINTER(wt.HANDLE),
                           ctypes.POINTER(SECURITY_ATTRIBUTES), wt.DWORD]
k32.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                               wt.BOOL, wt.DWORD, ctypes.c_void_p, wt.LPCWSTR,
                               ctypes.c_void_p, ctypes.POINTER(PROCESS_INFORMATION)]

PSEUDOCONSOLE = 0x00020016          # x/sys/windows; the macro-expanded 0x160002 is wrong
EXTENDED = 0x00080000

BASE = ""
LOGF = None


def say(msg):
    try:
        if LOGF:
            LOGF.write("[host] %s\n" % msg)
            LOGF.flush()
    except Exception:
        pass


def pipe():
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(sa)
    sa.bInheritHandle = True
    r, w = wt.HANDLE(), wt.HANDLE()
    if not k32.CreatePipe(ctypes.byref(r), ctypes.byref(w), ctypes.byref(sa), 0):
        raise OSError(ctypes.get_last_error(), "CreatePipe")
    return r, w


def spawn(cmdline, cwd, cols, rows):
    in_r, in_w = pipe()
    out_r, out_w = pipe()
    k32.SetHandleInformation(in_w, 1, 0)
    k32.SetHandleInformation(out_r, 1, 0)

    hpc = wt.HANDLE()
    hr = k32.CreatePseudoConsole(COORD(cols, rows), in_r, out_w, 0, ctypes.byref(hpc))
    if hr != 0:
        raise OSError("CreatePseudoConsole hr=%#x" % (hr & 0xFFFFFFFF))

    size = ctypes.c_size_t(0)
    k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attrs = ctypes.create_string_buffer(size.value)
    if not k32.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)):
        raise OSError(ctypes.get_last_error(), "InitializeProcThreadAttributeList")
    if not k32.UpdateProcThreadAttribute(attrs, 0, PSEUDOCONSOLE,
                                         ctypes.c_void_p(hpc.value),
                                         ctypes.sizeof(ctypes.c_void_p), None, None):
        raise OSError(ctypes.get_last_error(), "UpdateProcThreadAttribute")

    si = STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    # STARTF_USESTDHANDLES with explicit NULLs: without it the shell inherits this host's
    # std handles. When the host was given NUL handles (a service-ish spawn), cmd writes its
    # banner and prompt there and the pseudo console screen stays empty forever. With NULL
    # handles the shell falls back to CONOUT$, which is the pty.
    si.StartupInfo.dwFlags = 0x00000100
    si.StartupInfo.hStdInput = None
    si.StartupInfo.hStdOutput = None
    si.StartupInfo.hStdError = None
    si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
    pi = PROCESS_INFORMATION()
    buf = ctypes.create_unicode_buffer(cmdline)
    # Inherit the environment. A hand-built Unicode environment block made the pty attach
    # fail silently (the shell kept the parent's std handles and wrote nothing into the
    # console); the extra PATH entries are set once in app.py instead and inherited here.
    if not k32.CreateProcessW(None, buf, None, None, False, EXTENDED, None, cwd,
                              ctypes.byref(si), ctypes.byref(pi)):
        raise OSError(ctypes.get_last_error(), "CreateProcessW")
    k32.CloseHandle(in_r)
    k32.CloseHandle(out_w)
    return hpc, pi, in_w, out_r


class Follower:
    """Reads a file that keeps growing, remembering how far it got."""

    def __init__(self, path):
        self.fh = open(path, "rb", buffering=0)
        self.offset = 0

    def new_data(self):
        try:
            self.fh.seek(self.offset)
            data = self.fh.read()
        except OSError:
            return b""
        if data:
            self.offset += len(data)
        return data


def main():
    global BASE, LOGF
    kind = sys.argv[1] if len(sys.argv) > 1 else "cmd"
    cols = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    rows = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    cwd = sys.argv[4] if len(sys.argv) > 4 else os.getcwd()
    BASE = sys.argv[5] if len(sys.argv) > 5 else os.path.join(cwd, "term")

    LOGF = open(BASE + ".log", "a", encoding="utf-8")
    cmdlines = {
        "cmd": "cmd.exe /K",
        "pi": "cmd.exe /K pi",
        "pwsh": "powershell.exe -NoLogo",
        "python": '"%s" -i' % sys.executable.replace("pythonw.exe", "python.exe"),
    }
    cmdline = cmdlines.get(kind, cmdlines["cmd"])
    say("start kind=%s cmd=%r cwd=%r base=%r" % (kind, cmdline, cwd, BASE))

    try:
        hpc, pi, in_w, out_r = spawn(cmdline, cwd, cols, rows)
    except OSError as exc:
        say("spawn failed: %s" % exc)
        return 1
    say("child pid=%d" % pi.dwProcessId)

    rfd = msvcrt.open_osfhandle(out_r.value, os.O_RDONLY | os.O_BINARY)
    wfd = msvcrt.open_osfhandle(in_w.value, os.O_WRONLY | os.O_BINARY)
    out = open(BASE + ".out", "ab", buffering=0)
    inputs = Follower(BASE + ".in")
    ctl = Follower(BASE + ".ctl")
    say("ready rfd=%d wfd=%d" % (rfd, wfd))

    # A blocking os.read on the pty would freeze the whole loop (and with it keystrokes), so
    # the reading happens on its own thread and the main loop only drains what arrived.
    pending = []
    plock = threading.Lock()

    def reader():
        while True:
            try:
                data = os.read(rfd, 8192)
            except OSError:
                break
            if not data:
                break
            with plock:
                pending.append(data)
        with plock:
            pending.append(None)

    threading.Thread(target=reader, daemon=True).start()

    stop = False
    last = time.time()
    while not stop:
        with plock:
            arrived, pending[:] = list(pending), []
        for data in arrived:
            if data is None:
                say("pty eof")
                stop = True
                break
            out.write(data)
        chunk = inputs.new_data()
        if chunk:
            try:
                os.write(wfd, chunk)
            except OSError as exc:
                say("input write failed: %s" % exc)
        for line in ctl.new_data().splitlines():
            line = line.strip()
            if line == b"stop":
                stop = True
            elif b"x" in line:
                try:
                    c, r = line.split(b"x")
                    k32.ResizePseudoConsole(hpc, COORD(int(c), int(r)))
                    say("resize %sx%s" % (c, r))
                except Exception as exc:
                    say("resize failed: %s" % exc)
        if not arrived:
            time.sleep(0.015)
        if time.time() - last > 2:          # the app is gone: no one is listening
            last = time.time()
            if not os.path.exists(BASE + ".alive"):
                say("app disconnected, stopping")
                stop = True

    say("shutting down")
    try:
        k32.TerminateProcess(pi.hProcess, 0)
    except Exception:
        pass
    for handle in (pi.hThread, pi.hProcess):
        try:
            k32.CloseHandle(handle)
        except Exception:
            pass
    try:
        k32.ClosePseudoConsole(hpc)
    except Exception:
        pass
    out.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        try:
            with open((sys.argv[5] if len(sys.argv) > 5 else "term") + ".log", "a",
                      encoding="utf-8") as fh:
                fh.write(traceback.format_exc())
        except Exception:
            pass
        raise
