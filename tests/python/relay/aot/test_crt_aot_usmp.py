# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
""" This file contains test that use USMP + AoT using C runtime APIs"""

from collections import OrderedDict
import re

import numpy as np
import pytest

import tvm
from tvm import relay
from tvm.relay import transform
from tvm.relay.op.annotation import compiler_begin, compiler_end
from tvm.relay.backend import Executor, Runtime
from tvm import WorkspaceMemoryPools, PoolInfo
from tvm.micro import model_library_format as mlf
from tvm.micro.testing.aot_test_utils import parametrize_aot_options
from tvm.testing.aot import (
    AOTTestModel,
    AOTTestRunner,
    generate_ref_data,
    compile_and_run,
    compile_models,
    run_and_check,
    create_relay_module_and_inputs_from_tflite_file,
)
from tvm.testing.usmp import is_tvm_backendallocworkspace_calls


def _check_for_no_tvm_backendallocworkspace_calls(mod: tvm.runtime.module):
    assert (
        is_tvm_backendallocworkspace_calls(mod) is False
    ), "This is failing because USMP was unable to plan for every tir.allocate node."


@pytest.mark.parametrize(
    "workspace_byte_alignment,main_workspace_size",
    [
        (8, 17280),
        (16, 17280),
        (256, 17792),
    ],
)
def test_memory_planning(workspace_byte_alignment, main_workspace_size):
    """Checks calculated workspace against known values"""
    mod, params = tvm.relay.testing.synthetic.get_workload()
    target = "c"
    runtime = Runtime("crt")
    executor = Executor(
        "aot",
        {
            "workspace-byte-alignment": workspace_byte_alignment,
        },
    )
    with tvm.transform.PassContext(
        opt_level=3,
        config={
            "tir.disable_vectorize": True,
            "tir.disable_storage_rewrite": True,
            "tir.usmp.enable": True,
            "tir.usmp.algorithm": "greedy_by_conflicts",
        },
    ):
        lib = tvm.relay.build(mod, target, executor=executor, runtime=runtime, params=params)
    # The workspace_size dictionary will have an entry for both the 'primitive' and 'host'
    # targets, though both are identical.
    for size in lib.function_metadata["__tvm_main__"].workspace_sizes.values():
        assert size == main_workspace_size


@parametrize_aot_options
@pytest.mark.parametrize("groups,weight_shape", [(1, 32), (32, 1)])
def test_conv2d(interface_api, use_unpacked_api, test_runner, groups, weight_shape):
    """Test a subgraph with a single conv2d operator."""
    dtype = "float32"
    ishape = (1, 32, 14, 14)
    wshape = (32, weight_shape, 3, 3)
    pass_config = {"tir.usmp.enable": True}
    test_runner = AOTTestRunner(
        makefile=test_runner.makefile,
        prologue=test_runner.prologue,
        epilogue=test_runner.epilogue,
        includes=test_runner.includes,
        parameters=test_runner.parameters,
        pass_config=pass_config,
    )

    data0 = relay.var("data", shape=ishape, dtype=dtype)
    weight0 = relay.var("weight", shape=wshape, dtype=dtype)
    out = relay.nn.conv2d(data0, weight0, kernel_size=(3, 3), padding=(1, 1), groups=groups)
    main_f = relay.Function([data0, weight0], out)
    mod = tvm.IRModule()
    mod["main"] = main_f
    mod = transform.InferType()(mod)

    i_data = np.random.uniform(0, 1, ishape).astype(dtype)
    w1_data = np.random.uniform(0, 1, wshape).astype(dtype)

    inputs = OrderedDict([("data", i_data), ("weight", w1_data)])

    output_list = generate_ref_data(mod, inputs)
    compile_and_run(
        AOTTestModel(module=mod, inputs=inputs, outputs=output_list),
        test_runner,
        interface_api,
        use_unpacked_api,
    )
    compiled_test_mods = compile_models(
        models=AOTTestModel(module=mod, inputs=inputs, outputs=output_list),
        interface_api=interface_api,
        use_unpacked_api=use_unpacked_api,
        pass_config=test_runner.pass_config,
    )

    for compiled_model in compiled_test_mods:
        _check_for_no_tvm_backendallocworkspace_calls(compiled_model.executor_factory.lib)

    run_and_check(
        models=compiled_test_mods,
        runner=test_runner,
        interface_api=interface_api,
    )


@pytest.mark.parametrize("merge_compiler_regions", [False, True])
def test_byoc_microtvm(merge_compiler_regions):
    """
    This is a simple test to check BYOC capabilities of AOT
    with and without merging compiler regions to test for https://github.com/apache/tvm/issues/9036
    """
    use_unpacked_api = False
    interface_api = "packed"
    test_runner = AOTTestRunner(pass_config={"tir.usmp.enable": True})

    input_x = relay.var("x", shape=(10, 10))
    input_w0 = relay.var("w0", shape=(10, 10))
    input_w1 = relay.var("w1", shape=(10, 10))

    # z0 = x + w0
    marked_input_x = compiler_begin(input_x, "ccompiler")
    marked_input_w0 = compiler_begin(input_w0, "ccompiler")
    add_x_and_w0 = relay.add(marked_input_x, marked_input_w0)
    end_inner_add = compiler_end(add_x_and_w0, "ccompiler")

    # z1 = z0 + w1
    marked_inner_add = compiler_begin(end_inner_add, "ccompiler")
    marked_w1 = compiler_begin(input_w1, "ccompiler")
    add_nested_and_w1 = relay.add(marked_inner_add, marked_w1)
    end_outer_add = compiler_end(add_nested_and_w1, "ccompiler")

    # z2 = z0 + z1
    final_add = relay.add(end_inner_add, end_outer_add)

    relay_func = relay.Function([input_x, input_w0, input_w1], final_add)
    mod = tvm.IRModule()
    mod["main"] = relay_func

    if merge_compiler_regions:
        mod = transform.MergeCompilerRegions()(mod)

    mod = transform.PartitionGraph("mod_name")(mod)
    mod = transform.InferType()(mod)

    x_data = [("x", np.random.rand(10, 10).astype("float32"))]
    w_data = [("w{}".format(i), np.random.rand(10, 10).astype("float32")) for i in range(2)]

    map_inputs = OrderedDict(x_data + w_data)
    output_list = generate_ref_data(mod, map_inputs)

    compiled_test_mods = compile_models(
        AOTTestModel(name="my_mod", module=mod, inputs=map_inputs, outputs=output_list),
        interface_api=interface_api,
        use_unpacked_api=use_unpacked_api,
        pass_config=test_runner.pass_config,
    )

    for compiled_model in compiled_test_mods:
        _check_for_no_tvm_backendallocworkspace_calls(compiled_model.executor_factory.lib)

    run_and_check(
        models=compiled_test_mods,
        runner=test_runner,
        interface_api=interface_api,
    )


MOBILENET_V1_URL = (
    "https://storage.googleapis.com/download.tensorflow.org/models/"
    + "mobilenet_v1_2018_08_02/mobilenet_v1_1.0_224_quant.tgz",
    "mobilenet_v1_1.0_224_quant.tflite",
)
MOBILENET_V2_URL = (
    "https://storage.googleapis.com/download.tensorflow.org/models/"
    + "tflite_11_05_08/mobilenet_v2_1.0_224_quant.tgz",
    "mobilenet_v2_1.0_224_quant.tflite",
)


@pytest.mark.parametrize(
    "model_url, usmp_algo, workspace_size,",
    [
        (MOBILENET_V1_URL, "greedy_by_size", 4845696),
        (MOBILENET_V1_URL, "greedy_by_conflicts", 4444288),
        (MOBILENET_V1_URL, "hill_climb", 3240064),
    ],
)
def test_tflite_model_u1_usecase(model_url, usmp_algo, workspace_size):
    """
    This checks for ML models and the memory used by them
    when using USMP with different algorithms
    """
    pytest.importorskip("tflite")

    import tvm.relay.testing.tf as tf_testing  # pylint: disable=import-outside-toplevel

    use_unpacked_api = True
    interface_api = "c"
    test_runner = AOTTestRunner(
        pass_config={"tir.usmp.enable": True, "tir.usmp.algorithm": usmp_algo}
    )

    tflite_model_file = tf_testing.get_workload_official(
        model_url[0],
        model_url[1],
    )
    mod, inputs, params = create_relay_module_and_inputs_from_tflite_file(tflite_model_file)
    output_list = generate_ref_data(mod, inputs, params)

    compiled_test_mods = compile_models(
        AOTTestModel(module=mod, inputs=inputs, outputs=output_list, params=params),
        interface_api=interface_api,
        use_unpacked_api=use_unpacked_api,
        pass_config=test_runner.pass_config,
    )

    for compiled_model in compiled_test_mods:
        _check_for_no_tvm_backendallocworkspace_calls(compiled_model.executor_factory.lib)

    # Checking the workspace size reported in model library format
    mlf_memory_map = mlf._build_function_memory_map(
        compiled_test_mods[0].executor_factory.function_metadata
    )
    assert mlf_memory_map["main"][0]["workspace_size_bytes"] == workspace_size
    # That should match to workspace size that will be codegen'd to the entry point.
    allocated_pool_info = list(
        dict(compiled_test_mods[0].executor_factory.executor_codegen_metadata.pool_inputs).values()
    )[0]
    assert allocated_pool_info.allocated_size == workspace_size

    run_and_check(
        models=compiled_test_mods,
        runner=test_runner,
        interface_api=interface_api,
    )


def _get_workspace_size_define_macro(pool_name: str, model_name="default") -> str:
    """This function converts pool names to compiler generated
    workspace pool size macros"""

    prefix = "TVMGEN_" + model_name.upper() + "_"
    postfix = "_WORKSPACE_POOL_SIZE"
    return prefix + pool_name.upper() + postfix


def _add_module_prefix(suffix: str, model_name="default") -> str:
    """A helper function create struct types"""
    return "tvmgen_" + model_name + "_" + suffix


@pytest.mark.parametrize(
    "model_url, usmp_algo",
    [
        (MOBILENET_V1_URL, "greedy_by_size"),
    ],
)
def test_tflite_model_u3_usecase_single_external_pool(model_url, usmp_algo):
    """This checks for inference with USMP using external pool placed in the application"""
    pytest.importorskip("tflite")

    import tvm.relay.testing.tf as tf_testing  # pylint: disable=import-outside-toplevel

    use_unpacked_api = True
    interface_api = "c"

    pool_name = "my_memory_pool"
    target = tvm.target.Target("c")
    workspace_memory_pools = WorkspaceMemoryPools(
        [PoolInfo(pool_name, {target: PoolInfo.READ_WRITE_ACCESS})]
    )
    test_runner = AOTTestRunner(
        pass_config={"tir.usmp.enable": True, "tir.usmp.algorithm": usmp_algo},
        prologue=f"""
        __attribute__((section(".data.tvm"), aligned(16)))
        static uint8_t {pool_name}[{_get_workspace_size_define_macro(pool_name)}];
        """,
    )

    tflite_model_file = tf_testing.get_workload_official(
        model_url[0],
        model_url[1],
    )
    mod, inputs, params = create_relay_module_and_inputs_from_tflite_file(tflite_model_file)
    output_list = generate_ref_data(mod, inputs, params)

    compiled_test_mods = compile_models(
        AOTTestModel(module=mod, inputs=inputs, outputs=output_list, params=params),
        interface_api=interface_api,
        use_unpacked_api=use_unpacked_api,
        pass_config=test_runner.pass_config,
        workspace_memory_pools=workspace_memory_pools,
        target=target,
    )

    for compiled_model in compiled_test_mods:
        _check_for_no_tvm_backendallocworkspace_calls(compiled_model.executor_factory.lib)

    run_and_check(
        models=compiled_test_mods,
        runner=test_runner,
        interface_api=interface_api,
    )


@pytest.mark.parametrize(
    "model_url, usmp_algo",
    [
        (MOBILENET_V1_URL, "greedy_by_size"),
    ],
)
def test_tflite_model_u3_usecase_two_external_pools(model_url, usmp_algo):
    """This checks for inference using two external pools placed in the application"""
    pytest.importorskip("tflite")

    import tvm.relay.testing.tf as tf_testing  # pylint: disable=import-outside-toplevel

    use_unpacked_api = True
    interface_api = "c"

    target = tvm.target.Target("c")
    workspace_memory_pools = WorkspaceMemoryPools(
        [
            PoolInfo(
                "my_memory_pool_1", {target: PoolInfo.READ_WRITE_ACCESS}, size_hint_bytes=2500000
            ),
            PoolInfo("my_memory_pool_2", {target: PoolInfo.READ_WRITE_ACCESS}),
        ]
    )
    test_runner = AOTTestRunner(
        pass_config={"tir.usmp.enable": True, "tir.usmp.algorithm": usmp_algo},
        prologue=f"""
        __attribute__((section(".data.tvm"), aligned(16)))
        static uint8_t my_memory_pool_1[{_get_workspace_size_define_macro("my_memory_pool_1")}];
        __attribute__((section(".data.tvm"), aligned(16)))
        static uint8_t my_memory_pool_2[{_get_workspace_size_define_macro("my_memory_pool_2")}];
        """,
    )

    tflite_model_file = tf_testing.get_workload_official(
        model_url[0],
        model_url[1],
    )
    mod, inputs, params = create_relay_module_and_inputs_from_tflite_file(tflite_model_file)
    output_list = generate_ref_data(mod, inputs, params)

    compiled_test_mods = compile_models(
        AOTTestModel(module=mod, inputs=inputs, outputs=output_list, params=params),
        interface_api=interface_api,
        use_unpacked_api=use_unpacked_api,
        pass_config=test_runner.pass_config,
        workspace_memory_pools=workspace_memory_pools,
        target=target,
    )

    for compiled_model in compiled_test_mods:
        _check_for_no_tvm_backendallocworkspace_calls(compiled_model.executor_factory.lib)

    run_and_check(
        models=compiled_test_mods,
        runner=test_runner,
        interface_api=interface_api,
    )


@pytest.mark.parametrize(
    "model_urls, usmp_algo",
    [
        ((MOBILENET_V1_URL, MOBILENET_V2_URL), "greedy_by_size"),
    ],
)
def test_two_models_with_a_single_external_pool(model_urls, usmp_algo):
    """This checks for inference using a single large enough common pool"""
    pytest.importorskip("tflite")

    import tvm.relay.testing.tf as tf_testing  # pylint: disable=import-outside-toplevel

    use_unpacked_api = True
    interface_api = "c"

    target = tvm.target.Target("c")
    workspace_memory_pools = WorkspaceMemoryPools(
        [PoolInfo("my_memory_pool", {target: PoolInfo.READ_WRITE_ACCESS})]
    )
    test_runner = AOTTestRunner(
        pass_config={"tir.usmp.enable": True, "tir.usmp.algorithm": usmp_algo},
        prologue=f"""
        #define MAX(A, B) ((A > B) ? A : B)
        __attribute__((section(".data.tvm"), aligned(16)))
        static uint8_t my_memory_pool[MAX({_get_workspace_size_define_macro("my_memory_pool", "mod1")},{_get_workspace_size_define_macro("my_memory_pool", "mod2")})];
        """,
    )

    tflite_model_file1 = tf_testing.get_workload_official(
        model_urls[0][0],
        model_urls[0][1],
    )
    mod1, inputs1, params1 = create_relay_module_and_inputs_from_tflite_file(tflite_model_file1)
    output_list1 = generate_ref_data(mod1, inputs1, params1)

    tflite_model_file2 = tf_testing.get_workload_official(
        model_urls[1][0],
        model_urls[1][1],
    )
    mod2, inputs2, params2 = create_relay_module_and_inputs_from_tflite_file(tflite_model_file2)
    output_list2 = generate_ref_data(mod2, inputs2, params2)

    compiled_test_mods = compile_models(
        [
            AOTTestModel(
                name="mod1", module=mod1, inputs=inputs1, outputs=output_list1, params=params1
            ),
            AOTTestModel(
                name="mod2", module=mod2, inputs=inputs2, outputs=output_list2, params=params2
            ),
        ],
        interface_api=interface_api,
        use_unpacked_api=use_unpacked_api,
        pass_config=test_runner.pass_config,
        workspace_memory_pools=workspace_memory_pools,
        target=target,
    )

    for compiled_model in compiled_test_mods:
        _check_for_no_tvm_backendallocworkspace_calls(compiled_model.executor_factory.lib)

    run_and_check(
        models=compiled_test_mods,
        runner=test_runner,
        interface_api=interface_api,
    )


@pytest.mark.parametrize(
    "model_url, usmp_algo",
    [
        (MOBILENET_V1_URL, "greedy_by_size"),
    ],
)
def test_tflite_model_u4_usecase_single_external_pool(model_url, usmp_algo):
    """This checks for inference with USMP using external pool placed in the application"""
    pytest.importorskip("tflite")

    import tvm.relay.testing.tf as tf_testing  # pylint: disable=import-outside-toplevel

    use_unpacked_api = True
    interface_api = "c"

    pool_name = "my_memory_pool"
    target = tvm.target.Target("c")
    workspace_memory_pools = WorkspaceMemoryPools(
        [PoolInfo(pool_name, {target: PoolInfo.READ_WRITE_ACCESS})]
    )

    tflite_model_file = tf_testing.get_workload_official(
        model_url[0],
        model_url[1],
    )
    mod, inputs, params = create_relay_module_and_inputs_from_tflite_file(tflite_model_file)
    output_list = generate_ref_data(mod, inputs, params)

    input_name, input_data = list(inputs.items())[0]
    input_size_bytes = input_data.size * input_data.itemsize
    test_runner = AOTTestRunner(
        pass_config={
            "tir.usmp.enable": True,
            "tir.usmp.algorithm": usmp_algo,
            "tir.usmp.use_workspace_io": True,
        },
        prologue=f"""
        #include <string.h>
        __attribute__((section(".data.tvm"), aligned(16)))
        static uint8_t {pool_name}[{_get_workspace_size_define_macro(pool_name)}];
        struct {_add_module_prefix("workspace_pools")} {_add_module_prefix("workspace_pools")} = {{
            .{pool_name} = {pool_name}
        }};
        struct {_add_module_prefix("inputs")} {_add_module_prefix("inputs")} = {_add_module_prefix("map_inputs")}(&{_add_module_prefix("workspace_pools")});
        memcpy({_add_module_prefix("inputs")}.{input_name}, tvmgen_default_input_data_input, {input_size_bytes});
        struct {_add_module_prefix("outputs")} {_add_module_prefix("outputs")} = {_add_module_prefix("map_outputs")}(&{_add_module_prefix("workspace_pools")});
        """,
    )

    compiled_test_mods = compile_models(
        AOTTestModel(module=mod, inputs=inputs, outputs=output_list, params=params),
        interface_api=interface_api,
        use_unpacked_api=use_unpacked_api,
        pass_config=test_runner.pass_config,
        workspace_memory_pools=workspace_memory_pools,
        target=target,
    )

    for compiled_model in compiled_test_mods:
        _check_for_no_tvm_backendallocworkspace_calls(compiled_model.executor_factory.lib)

    run_and_check(
        models=compiled_test_mods,
        runner=test_runner,
        interface_api=interface_api,
        use_workspace_io=True,
    )


@pytest.mark.parametrize(
    "model_url, usmp_algo",
    [
        (MOBILENET_V1_URL, "greedy_by_size"),
    ],
)
def test_tflite_model_u4_usecase_two_external_pools(model_url, usmp_algo):
    """This checks for inference with USMP using external pool placed in the application"""
    pytest.importorskip("tflite")

    import tvm.relay.testing.tf as tf_testing  # pylint: disable=import-outside-toplevel

    use_unpacked_api = True
    interface_api = "c"

    target = tvm.target.Target("c")
    workspace_memory_pools = WorkspaceMemoryPools(
        [
            PoolInfo(
                "my_memory_pool_1", {target: PoolInfo.READ_WRITE_ACCESS}, size_hint_bytes=2500000
            ),
            PoolInfo("my_memory_pool_2", {target: PoolInfo.READ_WRITE_ACCESS}),
        ]
    )

    tflite_model_file = tf_testing.get_workload_official(
        model_url[0],
        model_url[1],
    )
    mod, inputs, params = create_relay_module_and_inputs_from_tflite_file(tflite_model_file)
    output_list = generate_ref_data(mod, inputs, params)

    input_name, input_data = list(inputs.items())[0]
    input_size_bytes = input_data.size * input_data.itemsize
    test_runner = AOTTestRunner(
        pass_config={
            "tir.usmp.enable": True,
            "tir.usmp.algorithm": usmp_algo,
            "tir.usmp.use_workspace_io": True,
        },
        prologue=f"""
        #include <string.h>
        __attribute__((section(".data.tvm"), aligned(16)))
        static uint8_t my_memory_pool_1[{_get_workspace_size_define_macro("my_memory_pool_1")}];
        __attribute__((section(".data.tvm"), aligned(16)))
        static uint8_t my_memory_pool_2[{_get_workspace_size_define_macro("my_memory_pool_2")}];
        struct {_add_module_prefix("workspace_pools")} {_add_module_prefix("workspace_pools")} = {{
            .my_memory_pool_1 = my_memory_pool_1,
            .my_memory_pool_2 = my_memory_pool_2,
        }};
        struct {_add_module_prefix("inputs")} {_add_module_prefix("inputs")} = {_add_module_prefix("map_inputs")}(&{_add_module_prefix("workspace_pools")});
        memcpy({_add_module_prefix("inputs")}.{input_name}, tvmgen_default_input_data_input, {input_size_bytes});
        struct {_add_module_prefix("outputs")} {_add_module_prefix("outputs")} = {_add_module_prefix("map_outputs")}(&{_add_module_prefix("workspace_pools")});
        """,
    )

    compiled_test_mods = compile_models(
        AOTTestModel(module=mod, inputs=inputs, outputs=output_list, params=params),
        interface_api=interface_api,
        use_unpacked_api=use_unpacked_api,
        pass_config=test_runner.pass_config,
        workspace_memory_pools=workspace_memory_pools,
        target=target,
    )

    for compiled_model in compiled_test_mods:
        _check_for_no_tvm_backendallocworkspace_calls(compiled_model.executor_factory.lib)

    run_and_check(
        models=compiled_test_mods,
        runner=test_runner,
        interface_api=interface_api,
        use_workspace_io=True,
    )


def test_incompatible_interface_api_errors():
    """Ensures an error is thrown if not using the C interface API"""
    mod, params = tvm.relay.testing.synthetic.get_workload()
    target = "c"
    runtime = Runtime("crt")
    executor = Executor(
        "aot",
        {
            "interface-api": "packed",
        },
    )

    with pytest.raises(
        tvm.TVMError,
        match=re.escape(
            "tir.usmp.use_workspace_io option is only compatible with interface_api c.\n"
            "Please use interface_api c to be able to enable tir.usmp.use_workspace_io"
        ),
    ):
        with tvm.transform.PassContext(
            opt_level=3,
            config={"tir.usmp.enable": True, "tir.usmp.use_workspace_io": True},
        ):
            tvm.relay.build(mod, target, executor=executor, runtime=runtime, params=params)


if __name__ == "__main__":
    tvm.testing.main()
