# Copyright 2024 The Aibrix Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# 	http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Adapted from vLLM
#
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import ctypes
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from shutil import which


import torch
from packaging.version import Version, parse
from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext
from setuptools_scm import get_version
from torch.utils.cpp_extension import CUDA_HOME, ROCM_HOME

ROOT_DIR = Path(__file__).parent
logger = logging.getLogger(__name__)

AIBRIX_TARGET_DEVICE = os.getenv("AIBRIX_TARGET_DEVICE", "cuda").strip().lower()

if sys.platform.startswith("darwin") and AIBRIX_TARGET_DEVICE != "cpu":
    logger.warning(
        "AIBRIX_TARGET_DEVICE automatically set to `cpu` due to macOS"
    )
    AIBRIX_TARGET_DEVICE = "cpu"
elif not (
    sys.platform.startswith("linux") or sys.platform.startswith("darwin")
):
    logger.warning("%s is not supported!", sys.platform)
    AIBRIX_TARGET_DEVICE = "empty"
elif (
    sys.platform.startswith("linux")
    and (torch.version.cuda is None or CUDA_HOME is None)
    and os.getenv("AIBRIX_TARGET_DEVICE") is None
    and torch.version.hip is None
):
    # if cuda or hip is not available and AIBRIX_TARGET_DEVICE is not set,
    # fallback to cpu
    AIBRIX_TARGET_DEVICE = "cpu"

MAIN_CUDA_VERSION = "12.8"


def is_sccache_available() -> bool:
    return which("sccache") is not None


def is_ccache_available() -> bool:
    return which("ccache") is not None


def is_ninja_available() -> bool:
    return which("ninja") is not None


def is_url_available(url: str) -> bool:
    from urllib.request import urlopen

    status = None
    try:
        with urlopen(url) as f:
            status = f.status
    except Exception:
        return False
    return status == 200


class CMakeExtension(Extension):
    def __init__(self, name: str, cmake_lists_dir: str = ".", **kwa) -> None:
        super().__init__(name, sources=[], py_limited_api=True, **kwa)
        self.cmake_lists_dir = os.path.abspath(cmake_lists_dir)


class cmake_build_ext(build_ext):
    # A dict of extension directories that have been configured.
    did_config: dict[str, bool] = {}

    #
    # Determine number of compilation jobs and optionally nvcc compile threads.
    #
    def compute_num_jobs(self):
        # `num_jobs` is either the value of the MAX_JOBS environment variable
        # (if defined) or the number of CPUs available.
        num_jobs = os.getenv("MAX_JOBS", None)
        if num_jobs is not None:
            num_jobs = int(num_jobs)
            logger.info("Using MAX_JOBS=%d as the number of jobs.", num_jobs)
        else:
            try:
                # os.sched_getaffinity() isn't universally available, so fall
                #  back to os.cpu_count() if we get an error here.
                num_jobs = len(os.sched_getaffinity(0))
            except AttributeError:
                num_jobs = os.cpu_count()

        nvcc_threads = None
        if _is_cuda() and get_nvcc_cuda_version() >= Version("11.2"):
            # `nvcc_threads` is either the value of the NVCC_THREADS
            # environment variable (if defined) or 1.
            # when it is set, we reduce `num_jobs` to avoid
            # overloading the system.
            nvcc_threads = os.getenv("NVCC_THREADS", None)
            if nvcc_threads is not None:
                nvcc_threads = int(nvcc_threads)
                logger.info(
                    "Using NVCC_THREADS=%d as the number of nvcc threads.",
                    nvcc_threads,
                )
            else:
                nvcc_threads = 1
            num_jobs = max(1, num_jobs // nvcc_threads)

        return num_jobs, nvcc_threads

    #
    # Perform cmake configuration for a single extension.
    #
    def configure(self, ext: CMakeExtension) -> None:
        # If we've already configured using the CMakeLists.txt for
        # this extension, exit early.
        if ext.cmake_lists_dir in cmake_build_ext.did_config:
            return

        cmake_build_ext.did_config[ext.cmake_lists_dir] = True

        # Select the build type.
        # Note: optimization level + debug info are set by the build type
        default_cfg = "Debug" if self.debug else "RelWithDebInfo"
        cfg = os.getenv("CMAKE_BUILD_TYPE", None) or default_cfg

        cmake_args = [
            "-DCMAKE_BUILD_TYPE={}".format(cfg),
            "-DAIBRIX_TARGET_DEVICE={}".format(AIBRIX_TARGET_DEVICE),
        ]

        verbose = bool(int(os.getenv("VERBOSE", "0")))
        if verbose:
            cmake_args += ["-DCMAKE_VERBOSE_MAKEFILE=ON"]

        if is_sccache_available():
            cmake_args += [
                "-DCMAKE_C_COMPILER_LAUNCHER=sccache",
                "-DCMAKE_CXX_COMPILER_LAUNCHER=sccache",
                "-DCMAKE_CUDA_COMPILER_LAUNCHER=sccache",
                "-DCMAKE_HIP_COMPILER_LAUNCHER=sccache",
            ]
        elif is_ccache_available():
            cmake_args += [
                "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
                "-DCMAKE_CUDA_COMPILER_LAUNCHER=ccache",
                "-DCMAKE_HIP_COMPILER_LAUNCHER=ccache",
            ]

        # Pass the python executable to cmake so it can find an exact
        # match.
        cmake_args += ["-DAIBRIX_PYTHON_EXECUTABLE={}".format(sys.executable)]

        # Pass the python path to cmake so it can reuse the build dependencies
        # on subsequent calls to python.
        cmake_args += ["-DAIBRIX_PYTHON_PATH={}".format(":".join(sys.path))]

        # Override the base directory for FetchContent downloads to $ROOT/.deps
        # This allows sharing dependencies between profiles,
        # and plays more nicely with sccache.
        # To override this, set the FETCHCONTENT_BASE_DIR environment variable.
        fc_base_dir = os.path.join(ROOT_DIR, ".deps")
        fc_base_dir = os.getenv("FETCHCONTENT_BASE_DIR", fc_base_dir)
        cmake_args += ["-DFETCHCONTENT_BASE_DIR={}".format(fc_base_dir)]

        #
        # Setup parallelism and build tool
        #
        num_jobs, nvcc_threads = self.compute_num_jobs()

        if nvcc_threads:
            cmake_args += ["-DNVCC_THREADS={}".format(nvcc_threads)]

        if is_ninja_available():
            build_tool = ["-G", "Ninja"]
            cmake_args += [
                "-DCMAKE_JOB_POOL_COMPILE:STRING=compile",
                "-DCMAKE_JOB_POOLS:STRING=compile={}".format(num_jobs),
            ]
        else:
            # Default build tool to whatever cmake picks.
            build_tool = []
        # Make sure we use the nvcc from CUDA_HOME
        if _is_cuda():
            cmake_args += [f"-DCMAKE_CUDA_COMPILER={CUDA_HOME}/bin/nvcc"]
        subprocess.check_call(
            ["cmake", ext.cmake_lists_dir, *build_tool, *cmake_args],
            cwd=self.build_temp,
        )

    def build_extensions(self) -> None:
        # Ensure that CMake is present and working
        try:
            subprocess.check_output(["cmake", "--version"])
        except OSError as e:
            raise RuntimeError("Cannot find CMake executable") from e

        # Create build directory if it does not exist.
        if not os.path.exists(self.build_temp):
            os.makedirs(self.build_temp)

        targets = []

        def target_name(s: str) -> str:
            return s.removeprefix("aibrix_kvcache.")

        # Build all the extensions
        for ext in self.extensions:
            self.configure(ext)
            targets.append(target_name(ext.name))

        num_jobs, _ = self.compute_num_jobs()

        build_args = [
            "--build",
            ".",
            f"-j={num_jobs}",
            *[f"--target={name}" for name in targets],
        ]

        subprocess.check_call(["cmake", *build_args], cwd=self.build_temp)

        # Install the libraries
        for ext in self.extensions:
            # Install the extension into the proper location
            outdir = Path(self.get_ext_fullpath(ext.name)).parent.absolute()

            # Skip if the install directory is the same as the build directory
            if outdir == self.build_temp:
                continue

            # CMake appends the extension prefix to the install path,
            # and outdir already contains that prefix, so we need to remove it.
            prefix = outdir
            for _ in range(ext.name.count(".")):
                prefix = prefix.parent

            # prefix here should actually be the same for all components
            install_args = [
                "cmake",
                "--install",
                ".",
                "--prefix",
                prefix,
                "--component",
                target_name(ext.name),
            ]
            subprocess.check_call(install_args, cwd=self.build_temp)

    def run(self):
        # First, run the standard build_ext command to compile the extensions
        super().run()


def _is_hpu() -> bool:
    # if AIBRIX_TARGET_DEVICE env var was set explicitly, skip HPU autodetection
    if os.getenv("AIBRIX_TARGET_DEVICE", None) == AIBRIX_TARGET_DEVICE:
        return AIBRIX_TARGET_DEVICE == "hpu"

    # if AIBRIX_TARGET_DEVICE was not set explicitly, check if hl-smi succeeds,
    # and if it doesn't, check if habanalabs driver is loaded
    is_hpu_available = False
    try:
        out = subprocess.run(["hl-smi"], capture_output=True, check=True)
        is_hpu_available = out.returncode == 0
    except (FileNotFoundError, PermissionError, subprocess.CalledProcessError):
        if sys.platform.startswith("linux"):
            try:
                output = subprocess.check_output(
                    "lsmod | grep habanalabs | wc -l", shell=True
                )
                is_hpu_available = int(output) > 0
            except (
                ValueError,
                FileNotFoundError,
                PermissionError,
                subprocess.CalledProcessError,
            ):
                pass
    return is_hpu_available


def _no_device() -> bool:
    return AIBRIX_TARGET_DEVICE == "empty"


def _is_cuda() -> bool:
    has_cuda = torch.version.cuda is not None and CUDA_HOME is not None
    return (
        AIBRIX_TARGET_DEVICE == "cuda"
        and has_cuda
        and not (_is_neuron() or _is_tpu() or _is_hpu())
    )


def _is_hip() -> bool:
    return (
        AIBRIX_TARGET_DEVICE == "cuda" or AIBRIX_TARGET_DEVICE == "rocm"
    ) and torch.version.hip is not None


def _is_neuron() -> bool:
    return AIBRIX_TARGET_DEVICE == "neuron"


def _is_tpu() -> bool:
    return AIBRIX_TARGET_DEVICE == "tpu"


def _is_cpu() -> bool:
    return AIBRIX_TARGET_DEVICE == "cpu"


def _is_xpu() -> bool:
    return AIBRIX_TARGET_DEVICE == "xpu"


def _build_custom_ops() -> bool:
    return _is_cuda() or _is_hip() or _is_cpu()


def get_rocm_version():
    # Get the Rocm version from the ROCM_HOME/bin/librocm-core.so
    # see https://github.com/ROCm/rocm-core/blob/d11f5c20d500f729c393680a01fa902ebf92094b/rocm_version.cpp#L21
    try:
        librocm_core_file = Path(ROCM_HOME) / "lib" / "librocm-core.so"
        if not librocm_core_file.is_file():
            return None
        librocm_core = ctypes.CDLL(librocm_core_file)
        VerErrors = ctypes.c_uint32
        get_rocm_core_version = librocm_core.getROCmVersion
        get_rocm_core_version.restype = VerErrors
        get_rocm_core_version.argtypes = [
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        major = ctypes.c_uint32()
        minor = ctypes.c_uint32()
        patch = ctypes.c_uint32()

        if (
            get_rocm_core_version(
                ctypes.byref(major), ctypes.byref(minor), ctypes.byref(patch)
            )
            == 0
        ):
            return f"{major.value}.{minor.value}.{patch.value}"
        return None
    except Exception:
        return None


def get_neuronxcc_version():
    import sysconfig

    site_dir = sysconfig.get_paths()["purelib"]
    version_file = os.path.join(site_dir, "neuronxcc", "version", "__init__.py")

    # Check if the command was executed successfully
    with open(version_file) as fp:
        content = fp.read()

    # Extract the version using a regular expression
    match = re.search(r"__version__ = '(\S+)'", content)
    if match:
        # Return the version string
        return match.group(1)
    else:
        raise RuntimeError("Could not find Neuron version in the output")


def get_nvcc_cuda_version() -> Version:
    """Get the CUDA version from nvcc.

    Adapted from https://github.com/NVIDIA/apex/blob/8b7a1ff183741dd8f9b87e7bafd04cfde99cea28/setup.py
    """
    assert CUDA_HOME is not None, "CUDA_HOME is not set"
    nvcc_output = subprocess.check_output(
        [CUDA_HOME + "/bin/nvcc", "-V"], universal_newlines=True
    )
    output = nvcc_output.split()
    release_idx = output.index("release") + 1
    nvcc_cuda_version = parse(output[release_idx].split(",")[0])
    return nvcc_cuda_version


def get_gaudi_sw_version():
    """
    Returns the driver version.
    """
    # Enable console printing for `hl-smi` check
    output = subprocess.run(
        "hl-smi",
        shell=True,
        text=True,
        capture_output=True,
        env={"ENABLE_CONSOLE": "true"},
    )
    if output.returncode == 0 and output.stdout:
        return (
            output.stdout.split("\n")[2]
            .replace(" ", "")
            .split(":")[1][:-1]
            .split("-")[0]
        )
    return "0.0.0"  # when hl-smi is not available


def get_aibrix_version() -> str:
    version = get_version(
        root="../..",
        write_to="python/aibrix_kvcache/aibrix_kvcache/_version.py",
    )

    sep = "+" if "+" not in version else "."  # dev versions might contain +

    if _no_device():
        if AIBRIX_TARGET_DEVICE == "empty":
            version += f"{sep}empty"
    elif _is_cuda():
        cuda_version = str(get_nvcc_cuda_version())
        if cuda_version != MAIN_CUDA_VERSION:
            cuda_version_str = cuda_version.replace(".", "")[:3]
            # skip this for source tarball, required for pypi
            if "sdist" not in sys.argv:
                version += f"{sep}cu{cuda_version_str}"
    elif _is_hip():
        # Get the Rocm Version
        rocm_version = get_rocm_version() or torch.version.hip
        if rocm_version and rocm_version != MAIN_CUDA_VERSION:
            version += f"{sep}rocm{rocm_version.replace('.', '')[:3]}"
    elif _is_neuron():
        # Get the Neuron version
        neuron_version = str(get_neuronxcc_version())
        if neuron_version != MAIN_CUDA_VERSION:
            neuron_version_str = neuron_version.replace(".", "")[:3]
            version += f"{sep}neuron{neuron_version_str}"
    elif _is_hpu():
        # Get the Intel Gaudi Software Suite version
        gaudi_sw_version = str(get_gaudi_sw_version())
        if gaudi_sw_version != MAIN_CUDA_VERSION:
            gaudi_sw_version = gaudi_sw_version.replace(".", "")[:3]
            version += f"{sep}gaudi{gaudi_sw_version}"
    elif _is_tpu():
        version += f"{sep}tpu"
    elif _is_cpu():
        if AIBRIX_TARGET_DEVICE == "cpu":
            version += f"{sep}cpu"
    elif _is_xpu():
        version += f"{sep}xpu"
    else:
        raise RuntimeError("Unknown runtime environment")

    return version


def get_requirements() -> list[str]:
    """Get Python package dependencies from requirements.txt."""
    requirements_dir = ROOT_DIR / "requirements"

    def _read_requirements(filename: str) -> list[str]:
        with open(requirements_dir / filename) as f:
            requirements = f.read().strip().split("\n")
        resolved_requirements = []
        for line in requirements:
            if line.startswith("-r "):
                resolved_requirements += _read_requirements(line.split()[1])
            elif (
                not line.startswith("--")
                and not line.startswith("#")
                and line.strip() != ""
            ):
                resolved_requirements.append(line)
        return resolved_requirements

    requirements = _read_requirements("core.txt")
    return requirements


ext_modules = []

if _build_custom_ops():
    if _is_cuda():
        ext_modules.append(CMakeExtension(name="aibrix_kvcache._aibrix_C"))

package_data = {}

if _no_device():
    ext_modules = []

if not ext_modules:
    cmdclass = {}
else:
    cmdclass = {"build_ext": cmake_build_ext}

setup(
    # static metadata should rather go in pyproject.toml
    version=get_aibrix_version(),
    ext_modules=ext_modules,
    install_requires=get_requirements(),
    extras_require={},
    cmdclass=cmdclass,
    package_data=package_data,
)
