"""Fail-fast GPU preflight. Run with a target environment's interpreter:

    <env>/bin/python scripts/preflight_gpu.py

Exits non-zero, with a specific reason, unless torch is importable, CUDA is
available, the device's compute capability matches what is expected, and a real
kernel actually executes on it.

Why this exists: the setup scripts only run `pip install`. They never import
torch and never touch a GPU, so they exit 0 on a machine where the services
cannot possibly start. The services then die inside a FastAPI startup handler,
detached, and the failure surfaces only in a log file. This script turns that
late, silent failure into an early, loud one.

Run it TWICE per environment: once right after torch is installed (before the
expensive dependency install), and once after, because packages that declare an
unpinned torch dependency can silently replace a CUDA-matched wheel with a
default-index one.

Expected capability defaults to the Blackwell RTX 50-series (12, 0). Override
with LA_EXPECT_CC, for example `LA_EXPECT_CC=8.9` for Ada, or set it empty to
accept whatever the device reports.
"""
import os
import sys


def fail(msg: str) -> "None":
    print(f"PREFLIGHT FAIL: {msg}", flush=True)
    sys.exit(1)


try:
    import torch
except ImportError as exc:
    fail(f"torch is not importable in this environment: {exc}")

print(f"torch {torch.__version__}", flush=True)

if not torch.cuda.is_available():
    fail(
        "torch.cuda.is_available() is False. The installed wheel is probably a "
        "CPU build. Reinstall from the CUDA index, for example:\n"
        "  pip install --force-reinstall --no-deps torch torchaudio "
        "--index-url https://download.pytorch.org/whl/cu128"
    )

if torch.cuda.device_count() < 1:
    fail("CUDA reports available but no devices are visible")

name = torch.cuda.get_device_name(0)
major, minor = torch.cuda.get_device_capability(0)
print(f"device 0: {name}  compute capability {major}.{minor}", flush=True)

expect = os.environ.get("LA_EXPECT_CC", "12.0").strip()
if expect:
    want = tuple(int(p) for p in expect.split("."))
    if (major, minor) != want:
        fail(
            f"expected compute capability {expect}, device reports {major}.{minor}. "
            "Either the wrong GPU is visible or the torch wheel targets a different "
            "architecture."
        )

# Capability reporting can succeed on a build whose kernels were never compiled
# for this architecture. Only a real kernel launch proves the wheel works.
try:
    x = torch.randn(512, 512, device="cuda")
    y = (x @ x).sum().item()
    torch.cuda.synchronize()
except Exception as exc:  # noqa: BLE001  the point is to catch anything
    fail(
        f"a matmul on the GPU raised {type(exc).__name__}: {exc}\n"
        "The wheel loads but has no kernels for this architecture. Install a build "
        "that targets it (cu128 or newer for sm_120)."
    )

if not (y == y):  # NaN check without importing math
    fail("matmul produced NaN, the CUDA install is unhealthy")

total = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
free, _ = torch.cuda.mem_get_info()
print(f"vram: {total:.1f} GiB total, {free / (1024 ** 3):.1f} GiB free", flush=True)
print("PREFLIGHT OK: cuda available, capability matches, kernels execute", flush=True)
