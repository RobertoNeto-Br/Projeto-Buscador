"""
Gerenciador de memória e reciclagem segura (sem perda de dados) para containers
com RAM limitada (ex.: Discloud 100-200 MB).
"""

from __future__ import annotations

import ctypes
import gc
import logging
import os
import sys

logger = logging.getLogger("buscador.memory")


def get_ram_mb() -> float:
    """Retorna o uso atual de RAM (RSS) do processo em MB sem dependências externas."""
    # Linux (Discloud / Docker)
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        kb = int(line.split()[1])
                        return round(kb / 1024.0, 2)
        except Exception:
            pass

    # Windows (desenvolvimento local)
    if sys.platform == "win32":
        try:
            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_bool
            handle = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return round(counters.WorkingSetSize / (1024.0 * 1024.0), 2)
        except Exception:
            pass

    return 0.0


def _malloc_trim_linux() -> bool:
    """Devolve blocos livres do heap Python/glibc imediatamente ao sistema operacional no Linux."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
        return True
    except Exception:
        return False


def recycle_memory() -> dict:
    """
    Executa reciclagem completa de memória:
    1) Coleta de lixo em todas as gerações (quebra referências cíclicas do BeautifulSoup/httpx)
    2) Devolução de heap livre ao sistema operacional via malloc_trim(0) no Linux
    """
    before = get_ram_mb()
    collected = gc.collect()
    gc.collect()
    trimmed = _malloc_trim_linux()
    after = get_ram_mb()
    freed = round(max(0.0, before - after), 2)

    logger.debug(
        "Reciclagem de memória: %.2f MB -> %.2f MB (liberado: %.2f MB | gc=%d | trim=%s)",
        before,
        after,
        freed,
        collected,
        trimmed,
    )
    return {
        "before_mb": before,
        "after_mb": after,
        "freed_mb": freed,
        "gc_collected": collected,
        "malloc_trim": trimmed,
    }


def get_pid() -> int:
    return os.getpid()
