"""Windows worker containment, imported only on Windows. Standard library only.

Create the child suspended, assign it to a kill-on-close job, then resume its
initial thread. Assignment after an ordinary Popen would let descendants escape.
See https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects .
"""
import ctypes as ct
from ctypes import wintypes as wt
import subprocess
import time


CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
THREAD_SUSPEND_RESUME = 0x0002
TH32CS_SNAPTHREAD = 0x00000004


class BasicLimits(ct.Structure):
    _fields_ = [("process_time", ct.c_longlong), ("job_time", ct.c_longlong), ("flags", wt.DWORD),
                ("min_working_set", ct.c_size_t), ("max_working_set", ct.c_size_t),
                ("active_limit", wt.DWORD), ("affinity", ct.c_size_t),
                ("priority", wt.DWORD), ("scheduling", wt.DWORD)]


class ExtendedLimits(ct.Structure):
    _fields_ = [("basic", BasicLimits), ("io_counters", ct.c_ulonglong * 6),
                ("process_memory", ct.c_size_t), ("job_memory", ct.c_size_t),
                ("peak_process_memory", ct.c_size_t), ("peak_job_memory", ct.c_size_t)]


class Accounting(ct.Structure):
    _fields_ = [("times", ct.c_longlong * 4), ("page_faults", wt.DWORD),
                ("total", wt.DWORD), ("active", wt.DWORD), ("terminated", wt.DWORD)]


class ThreadEntry(ct.Structure):
    _fields_ = [("size", wt.DWORD), ("usage", wt.DWORD), ("tid", wt.DWORD),
                ("pid", wt.DWORD), ("priority", wt.LONG), ("delta", wt.LONG), ("flags", wt.DWORD)]


class WindowsJob:
    creationflags = subprocess.CREATE_NO_WINDOW | CREATE_SUSPENDED

    def __init__(self):
        self.assigned = False
        self.api = ct.WinDLL("kernel32", use_last_error=True)
        for name, result, arguments in [
            ("CreateJobObjectW", wt.HANDLE, [ct.c_void_p, wt.LPCWSTR]),
            ("CloseHandle", wt.BOOL, [wt.HANDLE]),
            ("SetInformationJobObject", wt.BOOL, [wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD]),
            ("QueryInformationJobObject", wt.BOOL, [wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD, ct.c_void_p]),
            ("AssignProcessToJobObject", wt.BOOL, [wt.HANDLE, wt.HANDLE]),
            ("TerminateJobObject", wt.BOOL, [wt.HANDLE, wt.UINT]),
            ("OpenProcess", wt.HANDLE, [wt.DWORD, wt.BOOL, wt.DWORD]),
            ("OpenThread", wt.HANDLE, [wt.DWORD, wt.BOOL, wt.DWORD]),
            ("CreateToolhelp32Snapshot", wt.HANDLE, [wt.DWORD, wt.DWORD]),
            ("Thread32First", wt.BOOL, [wt.HANDLE, ct.POINTER(ThreadEntry)]),
            ("Thread32Next", wt.BOOL, [wt.HANDLE, ct.POINTER(ThreadEntry)]),
            ("ResumeThread", wt.DWORD, [wt.HANDLE]),
        ]:
            function = getattr(self.api, name)
            function.restype, function.argtypes = result, arguments
        # Anonymous and non-inheritable: supervisor death closes the last handle.
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ct.WinError(ct.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE  # No breakaway permission.
        if not self.api.SetInformationJobObject(self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                               ct.byref(limits), ct.sizeof(limits)):
            error = ct.WinError(ct.get_last_error())
            self.close()
            raise error

    def contain(self, child):
        """Assign a still-suspended Popen child, then resume. Caller owns failure cleanup."""
        process = self.api.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, child.pid)
        if not process:
            raise ct.WinError(ct.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, process):
                raise ct.WinError(ct.get_last_error())
            self.assigned = True
        finally:
            self._close_handle(process)
        self._resume(child.pid)

    def _resume(self, pid):
        # Popen closes CreateProcess's initial thread handle. Recover it using documented
        # Toolhelp APIs while the new process is suspended and cannot create other threads.
        snapshot = self.api.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if snapshot == ct.c_void_p(-1).value:
            raise ct.WinError(ct.get_last_error())
        try:
            entry = ThreadEntry()
            entry.size = ct.sizeof(entry)
            available = self.api.Thread32First(snapshot, ct.byref(entry))
            while available:
                if entry.size < ThreadEntry.pid.offset + ct.sizeof(wt.DWORD):
                    raise OSError("Windows thread snapshot omitted the owning process ID")
                if entry.pid == pid:
                    thread = self.api.OpenThread(THREAD_SUSPEND_RESUME, False, entry.tid)
                    if not thread:
                        raise ct.WinError(ct.get_last_error())
                    try:
                        count = self.api.ResumeThread(thread)
                        if count == 0xFFFFFFFF:
                            raise ct.WinError(ct.get_last_error())
                        if count != 1:
                            raise OSError("Expected exactly one suspended primary thread")
                    finally:
                        self._close_handle(thread)
                    return
                entry.size = ct.sizeof(entry)
                available = self.api.Thread32Next(snapshot, ct.byref(entry))
            raise OSError("Suspended child primary thread was not found")
        finally:
            self._close_handle(snapshot)

    def stop(self, timeout=15):
        """Terminate all ordinary descendants, even after their parent exits; await drain."""
        if not self.api.TerminateJobObject(self.handle, 1):
            raise ct.WinError(ct.get_last_error())
        deadline = time.monotonic() + timeout
        while True:
            info = Accounting()
            if not self.api.QueryInformationJobObject(self.handle, JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                                                     ct.byref(info), ct.sizeof(info), None):
                raise ct.WinError(ct.get_last_error())
            if info.active == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def _close_handle(self, handle):
        if not self.api.CloseHandle(handle):
            raise ct.WinError(ct.get_last_error())

    def close(self):
        if self.handle:
            self._close_handle(self.handle)
            self.handle = None
