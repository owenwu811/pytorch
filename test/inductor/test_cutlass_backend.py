# Owner(s): ["module: inductor"]
import logging
import os
import unittest
from typing import Callable, List, Optional
from unittest import mock

import torch
from torch._dynamo.utils import counters
from torch._inductor import config
from torch._inductor.ir import ChoiceCaller
from torch._inductor.test_case import run_tests, TestCase
from torch.testing._internal.common_cuda import SM75OrLater, SM80OrLater, SM90OrLater
from torch.testing._internal.common_utils import (
    instantiate_parametrized_tests,
    parametrize,
)

from torch.testing._internal.inductor_utils import HAS_CPU, HAS_CUDA

torch.set_float32_matmul_precision("high")
if HAS_CUDA:
    torch.cuda.memory._set_allocator_settings("expandable_segments:False")

_CUTLASS_DIR = os.path.join(os.path.dirname(__file__), "../../third_party/cutlass/")

log = logging.getLogger(__name__)

HAS_CUDA = HAS_CUDA and not torch.version.hip
SM75OrLater = SM75OrLater and not torch.version.hip
SM80OrLater = SM80OrLater and not torch.version.hip
SM90OrLater = SM90OrLater and not torch.version.hip


def _get_path_without_sccache() -> str:
    """
    Get the PATH environment variable without sccache.
    """
    path_envs = os.environ.get("PATH", "").split(":")
    path_envs = [env for env in path_envs if "/opt/cache/bin" not in env]
    return ":".join(path_envs)


@instantiate_parametrized_tests
class TestCutlassBackend(TestCase):
    def setUp(self):
        # The new inductor cache refresh mechanism
        # introduced with https://github.com/pytorch/pytorch/pull/122661
        # interacts badly with persistent subprocesses during
        # autotuning. So we need to disable automatic cache refresh
        # before calling setUp() on the parent class.
        old_disable_fresh_cache_envvar = os.environ.get(
            "INDUCTOR_TEST_DISABLE_FRESH_CACHE", ""
        )
        try:
            os.environ["INDUCTOR_TEST_DISABLE_FRESH_CACHE"] = "1"
            super().setUp()
        finally:
            os.environ[
                "INDUCTOR_TEST_DISABLE_FRESH_CACHE"
            ] = old_disable_fresh_cache_envvar
        torch.random.manual_seed(1234)

    @unittest.skipIf(not SM75OrLater, "need sm_75")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    @unittest.mock.patch.dict(os.environ, {"PATH": _get_path_without_sccache()})
    def test_max_autotune_cutlass_threshold(self):
        """
        Make sure Cutlass GEMM threshold works as intended.
        """

        if torch.version.hip:
            return

        torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False

        def mm(a, b):
            return a @ b

        a = torch.randn(100, 10).cuda().half()
        b = torch.randn(10, 100).cuda().half()

        with config.patch(
            {
                "max_autotune": True,
                "autotune_in_subproc": True,
                "max_autotune_gemm_backends": "CUTLASS,ATen",
                "compile_threads": 4,
                "cuda.cutlass_backend_min_gemm_size": 100000,
                "cuda.cutlass_dir": _CUTLASS_DIR,
                "cuda.cutlass_max_profiling_configs": 2,
            }
        ):
            from torch._inductor.codegen.cuda.cuda_kernel import CUDATemplateCaller

            with mock.patch(
                "torch._inductor.select_algorithm.autotune_select_algorithm"
            ) as mocked_select_algorithm:
                Y_compiled = torch.compile(mm, dynamic=False)(a, b)
                Y = mm(a, b)
                passed_choice_callers: List[ChoiceCaller] = mocked_select_algorithm[0][
                    1
                ]
                assert all(
                    isinstance(cc, ChoiceCaller) for cc in passed_choice_callers
                ), "Argument 1 to autotune_select_algorithm should be a list of ChoiceCaller instances"
                # We expect that no Cutlass Kernels are considered, due to the threshold
                assert all(
                    not isinstance(cc, CUDATemplateCaller)
                    for cc in passed_choice_callers
                ), "Cutlass Kernels should have been filtered, GEMM size is too small"
            torch.testing.assert_close(Y_compiled, Y)

    @unittest.skipIf(not SM75OrLater, "need sm_75")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    @unittest.mock.patch.dict(os.environ, {"PATH": _get_path_without_sccache()})
    def test_max_autotune_precompile(self):
        """
        Make sure autotuning mm in sub processes work without crashes.
        """

        if torch.version.hip:
            return

        torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False

        def mm(a, b):
            return a @ b

        a = torch.randn(100, 10).cuda().half()
        b = torch.randn(10, 100).cuda().half()

        with config.patch(
            {
                "max_autotune": True,
                "autotune_in_subproc": True,
                "max_autotune_gemm_backends": "CUTLASS,Triton,ATen",
                "compile_threads": 4,
                "cuda.cutlass_dir": _CUTLASS_DIR,
                "cuda.cutlass_max_profiling_configs": 2,
            }
        ):
            Y_compiled = torch.compile(mm, dynamic=False)(a, b)
            Y = mm(a, b)
            torch.testing.assert_close(Y_compiled, Y)

    # TODO: Enable dynamic test cases when dynamic support is added.
    @unittest.skipIf(not SM75OrLater, "need sm_75")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    @parametrize("dynamic", (False, True))
    @parametrize("max_autotune_gemm_backends", ("CUTLASS", "ATen,Triton,CUTLASS"))
    @unittest.mock.patch.dict(os.environ, {"PATH": _get_path_without_sccache()})
    def test_max_autotune_cutlass_backend_regular_mm(
        self, dynamic: bool, max_autotune_gemm_backends: str
    ):
        """
        Make sure autotuning mm in sub processes work without crashes.
        """

        if max_autotune_gemm_backends == "CUTLASS" and torch.version.hip:
            return

        torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False

        def mm(a, b):
            return a @ b

        a = torch.randn(100, 10).cuda().half()
        b = torch.randn(10, 100).cuda().half()

        with config.patch(
            {
                "max_autotune": True,
                "autotune_in_subproc": False,
                "max_autotune_gemm_backends": max_autotune_gemm_backends,
                "cuda.cutlass_dir": _CUTLASS_DIR,
                "cuda.cutlass_max_profiling_configs": 2,
            }
        ):
            Y_compiled = torch.compile(mm, dynamic=dynamic)(a, b)
            Y = mm(a, b)
            torch.testing.assert_close(Y_compiled, Y)

    def _test_max_autotune_cutlass_backend_epilogue_fusion(
        self,
        dynamic: bool = False,
        max_autotune_gemm_backends: str = "CUTLASS",
        mixed_precision=False,
        fp16=True,
        expected_fuse_count=0,
        mm: Callable[[torch.Tensor, torch.Tensor], torch.Tensor] = None,
        batch_size: Optional[int] = None,
    ):
        torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = (
            mixed_precision
        )

        # Note: The ops that are available
        # also depend on the alignment of the shapes
        # so if these shapes don't all align to at least 8 elements
        # it can happen that no Cutlass 3.x op is available
        # that allows fusions
        if batch_size is None:
            a = torch.randn(256, 32).cuda()
            b = torch.randn(32, 256).cuda()
        else:
            a = torch.randn(batch_size, 256, 32).cuda()
            b = torch.randn(batch_size, 32, 256).cuda()
        if fp16:
            a = a.half()
            b = b.half()

        with config.patch(
            {
                "max_autotune": True,
                "autotune_in_subproc": False,
                "max_autotune_gemm_backends": max_autotune_gemm_backends,
                "cuda.cutlass_dir": _CUTLASS_DIR,
                "cuda.cutlass_max_profiling_configs": 4,
                "cuda.cutlass_only_evt_capable_ops": True,
                "cuda.version": "12.2",  # required to enable the Kernels we need
            }
        ):
            counters["inductor"]["cuda_epilogue_fusion_counter"] = 0
            Y_compiled = torch.compile(mm, dynamic=dynamic)(a, b)
            Y = mm(a, b)
            actual_count = counters["inductor"]["cuda_epilogue_fusion_counter"]
            assert (
                actual_count == expected_fuse_count
            ), f"Expected fuse count of {expected_fuse_count} but got {actual_count}"
            torch.testing.assert_close(Y_compiled, Y, atol=1e-2, rtol=1e-2)

    @unittest.skipIf(not SM90OrLater, "need sm_90")
    @unittest.skipIf(torch.version.hip, "HIP not supported")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    def test_max_autotune_cutlass_backend_simple_fusion_fp16(self):
        def mm(a, b):
            return (a @ b) * 3.0

        #  The pointwise ops seem to be pre-fused into a single Pointwise
        self._test_max_autotune_cutlass_backend_epilogue_fusion(
            mixed_precision=False, fp16=True, expected_fuse_count=0, mm=mm
        )

    @unittest.skipIf(not SM90OrLater, "need sm_90")
    @unittest.skipIf(torch.version.hip, "HIP not supported")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    def test_max_autotune_cutlass_backend_simple_fusion_fp16_fp32acc(self):
        def mm(a, b):
            return (a @ b) * 3.0

        self._test_max_autotune_cutlass_backend_epilogue_fusion(
            mixed_precision=True, fp16=True, expected_fuse_count=0, mm=mm
        )

    @unittest.skipIf(not SM90OrLater, "need sm_90")
    @unittest.skipIf(torch.version.hip, "HIP not supported")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    def test_max_autotune_cutlass_backend_chained_fusion_fp16(self):
        def mm(a, b):
            return (a @ b) * 3.3 - 1.234

        #  The pointwise ops seem to be pre-fused into a single Pointwise
        self._test_max_autotune_cutlass_backend_epilogue_fusion(
            mixed_precision=False, fp16=True, expected_fuse_count=0, mm=mm
        )

    @unittest.skipIf(not SM90OrLater, "need sm_90")
    @unittest.skipIf(torch.version.hip, "HIP not supported")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    def test_max_autotune_cutlass_backend_chained_fusion_fp16_fp32acc(self):
        def mm(a, b):
            return (a @ b) * 3.3 - 1.234

        self._test_max_autotune_cutlass_backend_epilogue_fusion(
            mixed_precision=True, fp16=True, expected_fuse_count=0, mm=mm
        )

    @unittest.skipIf(not SM90OrLater, "need sm_90")
    @unittest.skipIf(torch.version.hip, "HIP not supported")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    def test_max_autotune_cutlass_backend_relu_fusion_fp16(self):
        def mm(a, b):
            return torch.nn.functional.relu((a @ b) * 3.3 - 1.234)

        self._test_max_autotune_cutlass_backend_epilogue_fusion(
            mixed_precision=False, fp16=True, expected_fuse_count=0, mm=mm
        )

    @unittest.skipIf(not SM90OrLater, "need sm_90")
    @unittest.skipIf(torch.version.hip, "HIP not supported")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    def test_max_autotune_cutlass_backend_relu_fusion_fp16_fp32acc(self):
        def mm(a, b):
            return torch.nn.functional.relu((a @ b) * 3.3 - 1.234)

        #  The pointwise ops seem to be pre-fused into a single Pointwise
        self._test_max_autotune_cutlass_backend_epilogue_fusion(
            mixed_precision=True, fp16=True, expected_fuse_count=0, mm=mm
        )

    @unittest.skipIf(not SM90OrLater, "need sm_90")
    @unittest.skipIf(torch.version.hip, "HIP not supported")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    def test_max_autotune_cutlass_backend_relu6_fusion_fp16_fp32acc(self):
        def mm(a, b):
            return torch.clamp(torch.nn.functional.relu(a @ b), max=6.0)

        #  The pointwise ops seem to be pre-fused into a single Pointwise
        self._test_max_autotune_cutlass_backend_epilogue_fusion(
            mixed_precision=True, fp16=True, expected_fuse_count=0, mm=mm
        )

    @unittest.skipIf(not SM90OrLater, "need sm_90")
    @unittest.skipIf(torch.version.hip, "HIP not supported")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    def test_max_autotune_cutlass_backend_no_fusion_dtype_mismatch(self):
        def mm(a, b):
            # this should not be fused, since the output dtype is different from the matmul dtype
            return (a @ b).to(torch.float32) * 0.00001

        self._test_max_autotune_cutlass_backend_epilogue_fusion(
            mixed_precision=True, fp16=True, expected_fuse_count=0, mm=mm
        )

    def test_max_autotune_cutlass_backend_simple_bmm(self):
        def bmm(a, b):
            return torch.bmm(a, b)

        self._test_max_autotune_cutlass_backend_epilogue_fusion(  # test bmm
            mixed_precision=False,
            fp16=True,
            expected_fuse_count=0,
            mm=bmm,
            batch_size=10,
        )

    @unittest.skipIf(not SM90OrLater, "need sm_90")
    @unittest.skipIf(torch.version.hip, "HIP not supported")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    def test_max_autotune_cutlass_backend_shape_dependent_normalization_fusion(self):
        def mm(a, b):
            return (a @ b) / b.size(1)

        self._test_max_autotune_cutlass_backend_epilogue_fusion(
            mixed_precision=True, fp16=True, expected_fuse_count=0, mm=mm
        )

    # TODO: Enable dynamic test cases when dynamic support is added.
    @unittest.skipIf(not SM75OrLater, "need sm_75")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    @parametrize("dynamic", (False,))
    @parametrize("max_autotune_gemm_backends", ("CUTLASS", "ATen,Triton,CUTLASS"))
    @unittest.mock.patch.dict(os.environ, {"PATH": _get_path_without_sccache()})
    def test_max_autotune_cutlass_backend_mm_bias(
        self, dynamic: bool = False, max_autotune_gemm_backends: str = "CUTLASS"
    ):
        """
        Make sure autotuning mm in sub processes work without crashes.
        """

        if max_autotune_gemm_backends == "CUTLASS" and torch.version.hip:
            return

        torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False

        def mm(a, b, bias):
            return torch.nn.functional.linear(a, b, bias)

        a = torch.randn(2048, 4096).cuda().half()
        bias = torch.randn(2048).cuda().half()

        with config.patch(
            {
                "max_autotune": True,
                "autotune_in_subproc": False,
                "max_autotune_gemm_backends": max_autotune_gemm_backends,
                "cuda.cutlass_dir": _CUTLASS_DIR,
                "cuda.cutlass_max_profiling_configs": 2,
            }
        ):
            Y = mm(a, a, bias)
            Y_compiled = torch.compile(mm, dynamic=dynamic)(a, a, bias)
            torch.testing.assert_close(Y_compiled, Y, atol=1e-1, rtol=1e-1)

    # TODO: Enable dynamic test cases when dynamic support is added.
    @unittest.skipIf(not SM75OrLater, "need sm_75")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    @parametrize("dynamic", (False,))
    @parametrize("max_autotune_gemm_backends", ("CUTLASS", "ATen,Triton,CUTLASS"))
    @unittest.mock.patch.dict(os.environ, {"PATH": _get_path_without_sccache()})
    def test_max_autotune_cutlass_backend_addmm(
        self, dynamic, max_autotune_gemm_backends
    ):
        """
        Make sure autotuning addmm in sub processes work without crashes.
        """

        if max_autotune_gemm_backends == "CUTLASS" and torch.version.hip:
            return

        torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False

        def addmm(x, a, b, alpha, beta):
            return torch.addmm(x, a, b, alpha=alpha, beta=beta)

        def compare_results(
            m: int, k: int, n: int, alpha: float, beta: float, x_shape: List[int]
        ) -> None:
            x = torch.randn(x_shape).cuda().half()
            a = torch.randn(m, k).cuda().half()
            b = torch.randn(k, n).cuda().half()
            y_expected = addmm(x, a, b, alpha, beta)

            compiled_fn = torch.compile(addmm, dynamic=dynamic)
            y = compiled_fn(x, a, b, alpha, beta)
            torch.testing.assert_close(y, y_expected)

        with config.patch(
            {
                "max_autotune": True,
                # Some Cutlass Kernels fail with IMA on this example, which leads to unrecoverable CUDA errors
                # unless we tune in a subproc here.
                "autotune_in_subproc": True,
                "max_autotune_gemm_backends": max_autotune_gemm_backends,
                "cuda.cutlass_dir": _CUTLASS_DIR,
                "cuda.cutlass_max_profiling_configs": 4,
            }
        ):
            # No broadcast
            compare_results(4096, 25728, 2048, 2.0, 0.4, [4096, 2048])
            # Broadcast first dim.
            compare_results(4096, 25728, 2048, 2.0, 0.4, [2048])
            # Broadcast last dim.
            compare_results(4096, 25728, 2048, 2.0, 0.4, [4096, 1])

    # TODO: Enable dynamic test cases when dynamic support is added.
    @unittest.skipIf(not SM80OrLater, "need sm_80")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    @parametrize("dynamic", (False,))
    @parametrize("max_autotune_gemm_backends", ("CUTLASS", "CUTLASS,ATen"))
    @unittest.mock.patch.dict(os.environ, {"PATH": _get_path_without_sccache()})
    def test_max_autotune_cutlass_backend_int_mm(
        self, dynamic: bool, max_autotune_gemm_backends: str
    ):
        """
        Make sure autotuning mm in sub processes work without crashes.
        """

        if "CUTLASS" in max_autotune_gemm_backends.upper() and torch.version.hip:
            return

        def mm(a, b):
            return torch._int_mm(a, b)

        # CUTLASS only supports row-major/column-major combination of
        # layouts for this operation, thus the transpose of tensor b
        # (on the other side, Triton at the moment doesn't support
        # this combination, so it's excluded from the test).  Also,
        # for CUTLASS alignment requirements, number of columns in
        # both tensors has to be divisible by 16.
        a = torch.randint(0, 5, (100, 16), dtype=torch.int8).cuda()
        b = torch.randint(0, 5, (32, 16), dtype=torch.int8).cuda().T

        with config.patch(
            {
                "max_autotune": True,
                "autotune_in_subproc": False,
                "max_autotune_gemm_backends": max_autotune_gemm_backends,
                "cuda.cutlass_dir": _CUTLASS_DIR,
                "cuda.cutlass_max_profiling_configs": 2,
            }
        ):
            Y_compiled = torch.compile(mm, dynamic=dynamic)(a, b)
            Y = mm(a, b)
            torch.testing.assert_close(Y_compiled, Y)

    # TODO: Enable dynamic test cases when dynamic support is added.
    @unittest.skipIf(not SM80OrLater, "need sm_80")
    @unittest.skipIf(config.is_fbcode(), "fbcode requires different CUTLASS path setup")
    @parametrize("dynamic", (False,))
    @parametrize("max_autotune_gemm_backends", ("CUTLASS", "CUTLASS,Triton,ATen"))
    @unittest.mock.patch.dict(os.environ, {"PATH": _get_path_without_sccache()})
    def test_max_autotune_cutlass_backend_mixed_mm(
        self, dynamic: bool, max_autotune_gemm_backends: str
    ):
        """
        Make sure autotuning mm in sub processes work without crashes.
        """

        if max_autotune_gemm_backends == "CUTLASS" and torch.version.hip:
            return

        torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False

        def mm(a, b):
            return torch.mm(a, b.to(torch.half))

        # CUTLASS only supports row-major/column-major combination of
        # layouts for this operation, thus the transpose of tensor b.
        # Also, for CUTLASS alignment requirements, number of columns
        # of the first tensor has to be divisible by 16.
        a = torch.randn(100, 16).cuda().half()
        b = torch.randint(0, 5, (100, 16), dtype=torch.int8).cuda().T

        with config.patch(
            {
                "max_autotune": True,
                "autotune_in_subproc": False,
                "max_autotune_gemm_backends": max_autotune_gemm_backends,
                "cuda.cutlass_dir": _CUTLASS_DIR,
                "cuda.cutlass_max_profiling_configs": 2,
                "use_mixed_mm": True,
            }
        ):
            Y_compiled = torch.compile(mm, dynamic=dynamic)(a, b)
            Y = mm(a, b)
            torch.testing.assert_close(Y_compiled, Y)


if __name__ == "__main__":
    from torch._inductor.utils import is_big_gpu

    # Set env to make it work in CI.
    if HAS_CUDA and HAS_CPU and is_big_gpu(0):
        run_tests()
