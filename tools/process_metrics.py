"""Read Windows process CPU time and working set without an optional dependency."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys


class ProcessMetrics:
    def __init__(self, pid):
        if sys.platform != "win32":
            raise RuntimeError("process resource sampling currently requires Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
        self.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        self.handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def sample(self):
        times = [wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(value) for value in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        memory = MemoryCounters()
        memory.cb = ctypes.sizeof(memory)
        if not self.psapi.GetProcessMemoryInfo(self.handle, ctypes.byref(memory), memory.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        cpu = sum((value.dwHighDateTime << 32) | value.dwLowDateTime for value in times[2:]) / 10_000_000
        return dict(cpuSeconds=cpu, workingSetBytes=memory.WorkingSetSize)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class MemoryCounters(ctypes.Structure):
    # https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
            "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
