#!/usr/bin/env python3
# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations
from os import PathLike
from pathlib import Path
from typing import Optional, Dict
from importlib import import_module
import json

from code.common.systems.system_list import SystemClassifications

import importlib.util
import time
import subprocess
import os
import sys

from nvmitten.constants import Precision
from nvmitten.nvidia.builder import (TRTBuilder,
                                     MLPerfInferenceEngine,
                                     LegacyBuilder)
from nvmitten.pipeline import Operation, ScratchSpace
from nvmitten.utils import logging

from code.common.mitten_compat import ArgDiscarder
from code.common.constants import WorkloadSetting, ModelOpt
LLAMA2Component = import_module("code.llama2-70b.tensorrt.constants").LLAMA2Component


class LLAMA2EngineBuilder(TRTBuilder,
                          MLPerfInferenceEngine,
                          ArgDiscarder):
    """LLAMA2 offloads the engine building to the implementation in the TRT-LLM examples. This class still is a sublass
    of nvmitten.nvidia.builder.TRTBuilder solely for consistency and to use the same class initializer.
    """

    def __init__(self,
                 workload_setting: WorkloadSetting,
                 config_ver: str = "default",
                 model_path: str = "build/models/Llama2/Llama-2-70b-chat-hf",
                 quant_model_path: str = "FP8_quantized/",
                 trt_llm_path: str = "build/TRTLLM",
                 # Override the normal default values
                 workspace_size: int = 60 << 30,
                 # Benchmark specific values
                 batch_size: int = 16,
                 use_inflight_batching: bool = False,
                 tensor_parallelism: int = 2,
                 pipeline_parallelism: int = 1,
                 max_num_tokens: int = 16384,
                 **kwargs):
        super().__init__(workspace_size=workspace_size, **kwargs)

        self.config_ver = config_ver
        self.batch_size = batch_size
        self.model_path = model_path
        self.quant_model_path = Path(model_path) / Path(quant_model_path)
        self.use_inflight_batching = use_inflight_batching
        self.workload_setting = workload_setting

        # read checkpoint config
        with open(self.quant_model_path / "config.json", 'r') as f:
            config = json.load(f)

        self.quant_algo = config["quantization"]["quant_algo"]

        self.dtype = config["dtype"]

        # TODO: Parameterize these
        self.tp_size = tensor_parallelism
        self.pp_size = pipeline_parallelism
        self.world_size = self.tp_size * self.pp_size
        assert self.world_size > 0
        # required for generating engine
        self.max_num_tokens = max_num_tokens

        # https://gitlab-master.nvidia.com/ftp/tekit/-/tree/main/examples/llama?ref_type=heads#fp8-post-training-quantization
        # Please refer to README.md for quantization.
        if not self.quant_model_path.exists():
            raise FileNotFoundError(f"Could not locate Llama2 fp8 quantized checkpoint model path: ({self.quant_model_path}). Please check README.md")
            # TODO If the default model does not exist, build a quantized model locally

        self.trt_llm_path = Path(trt_llm_path)

        if self.precision != Precision.FP16:
            raise NotImplementedError(f"Precision {self.precision} is not supported yet.")

    def engine_dir(self, scratch_space: ScratchSpace) -> Path:
        system_id = self.system.get_id()
        name = self.benchmark.valstr()
        scenario = self.scenario.valstr()
        return scratch_space.path / "engines" / system_id / name / self.workload_setting.model_opt.valstr() / scenario

    def build_quantized_model(self):
        """
        Use AMMO to build the quantized FP8 model for TRTLLM usage
        """
        quantize_script_path = self.trt_llm_path / "examples/llama/quantize.py"
        if not quantize_script_path.exists():
            raise FileNotFoundError(f"Could not locate Llama2 quantize script ({quantize_script_path}), please run `make clone_trt_llm`")

        quantize_dir = self.quant_model_path
        quantize_dir.mkdir(parents=True, exist_ok=True)
        flags = [
            f"--model_dir={self.model_path}",
            f"--dtype={self.dtype}",
            "--qformat=fp8",
            f"--export_path={quantize_dir}",
            "--calib_size=1024"
        ]

        quantize_cmd = [sys.executable, str(quantize_script_path.absolute())] + flags
        logging.info(f"Building Llama2 FP8 quantization model in {quantize_dir}.")
        logging.info(f"Command: {' '.join(quantize_cmd)}")

        stdout_log = self.quant_model_path.with_suffix(".stdout")
        stderr_log = self.quant_model_path.with_suffix(".stderr")

        tik = time.time()
        ret = subprocess.run(quantize_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        tok = time.time()

        # Save stdout and stderr logs
        with stdout_log.open(mode='w') as f:
            f.write(ret.stdout)
        with stderr_log.open(mode='w') as f:
            f.write(ret.stderr)

        if ret.returncode != 0:
            logging.error(ret.stderr)
            raise RuntimeError(f"Quantization recipe failed. Logs dumped to {stderr_log}.")

        logging.info(f"Quantized model completes in {tok-tik}s. Saved to {self.quant_model_path}")

    def create_network(self):
        logging.info(f"Llama2 network is created implicitly in TRTLLM")

    def build_engine(self,
                     network: trt.INetworkDefinition,
                     builder_config: trt.IBuilderConfig,
                     batch_size: int,
                     engine_fpath: PathLike):
        """Builds the engine via the Llama2 builder provided in the TRT-LLM examples.

        Args:
            network: Unused.
            builder_config: Unused.
            batch_size (int): Batch size to build the engine for.
            engine_fpath (PathLike): Location to save the engine file(s) to.
        """
        if not importlib.util.find_spec("tensorrt_llm"):
            raise ModuleNotFoundError("Cannot import tensorrt_llm module. Please run `make build_trt_llm`.")

        build_script = Path("tensorrt_llm/commands/build.py")
        build_script_path = self.trt_llm_path / build_script
        if not build_script_path.exists():
            raise FileNotFoundError(f"Could not locate Llama2 build script ({build_script_path}), please run `make clone_trt_llm`")

        engine_fpath = Path(engine_fpath)
        if engine_fpath.is_file():
            logging.warning(f"{engine_fpath} already exists. This file will be overwritten")
        engine_dir = engine_fpath.parent
        engine_dir.mkdir(parents=True, exist_ok=True)

        max_input_len = 1024
        if SystemClassifications.is_soc():
            max_output_len = 768
        else:
            max_output_len = 1024

        flags = [f"--gpt_attention_plugin={self.dtype}",
                 f"--max_batch_size={self.batch_size}",
                 f"--max_input_len={max_input_len}",
                 f"--max_seq_len={max_input_len + max_output_len}",
                 "--max_beam_width=1",
                 f"--max_num_tokens={self.max_num_tokens}",
                 f"--output_dir={str(engine_dir.absolute())}",
                 f"--checkpoint_dir={str(self.quant_model_path.absolute())}",
                 "--context_fmha=enable",
                 "--remove_input_padding=enable",
                 "--paged_kv_cache=enable",
                 "--use_custom_all_reduce=enable",
                 ]

        if self.quant_algo == "FP8":
            flags += ["--use_fused_mlp"]
        elif self.quant_algo == "W4A16_AWQ":
            pass

        if self.workload_setting.model_opt == ModelOpt.DepthPrunedMedusa:
            flags += ["--speculative_decoding_mode=medusa"]
        elif self.workload_setting.model_opt == ModelOpt.Sparse:
            flags += ["--weight_sparsity"]

        if self.world_size > 1:
            flags.append(f"--workers={self.world_size}")
            flags.append("--reduce_fusion=enable")

        build_cmd = [sys.executable, "-m", '.'.join(build_script.with_suffix('').parts)] + flags

        # Leave here in case any MACRO flag is needed
        custom_env = os.environ.copy()

        logging.info(f"Building Llama2 engine in {engine_dir}.")
        logging.info(f"Command executing in build/TRTLLM dir: {' '.join(build_cmd)}")

        stdout_log = engine_fpath.with_suffix(".stdout")
        stderr_log = engine_fpath.with_suffix(".stderr")

        tik = time.time()
        ret = subprocess.run(build_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             cwd="build/TRTLLM", env=custom_env)
        tok = time.time()

        # Save stdout and stderr logs
        with stdout_log.open(mode='w') as f:
            f.write(ret.stdout)
        with stderr_log.open(mode='w') as f:
            f.write(ret.stderr)

        if ret.returncode != 0:
            logging.error(ret.stderr)
            raise RuntimeError(f"Engine build failed. Logs dumped to {engine_dir}.")

        logging.info(f"Engine build complete in {tok-tik}s. Saved to {engine_fpath}")


class LLAMA2EngineBuilderOp(Operation,
                            ArgDiscarder):
    COMPONENT_BUILDER_MAP = {
        LLAMA2Component.LLAMA2: LLAMA2EngineBuilder,
    }

    @classmethod
    def immediate_dependencies(cls):
        # TODO: Integrate dataset scripts as deps
        return None

    def __init__(self,
                 *args,
                 # Benchmark specific values
                 batch_size: Dict[LLAMA2Component, int] = None,
                 **kwargs):
        """Creates a LLAMA2EngineBuilderOp.

        Args:
            batch_size (Dict[str, int]): Component and its batch size to build the engine for)
        """
        super().__init__(*args, **kwargs)
        if not batch_size:
            logging.warning("No batch_size dict provided for LLAMA2EngineBuilderOp. Setting to default value {LLAMA2Component.LLAMA2 : 1}")
            batch_size = {LLAMA2Component.LLAMA2: 1}
        self.builders = []
        for component, component_batch_size in batch_size.items():
            builder = LLAMA2EngineBuilderOp.COMPONENT_BUILDER_MAP[component](*args, batch_size=component_batch_size, **kwargs)
            self.builders.append(builder)

    def run(self, scratch_space, dependency_outputs):
        for builder in self.builders:
            engine_dir = builder.engine_dir(scratch_space)
            # We distinguish engines by the dir name for TRTLLM
            engine_dir = engine_dir / f"bs{builder.batch_size}-{builder.config_ver}-tp{builder.tp_size}-pp{builder.pp_size}"
            # For TRT-LLM, the engine name is fixed.
            engine_name = f"rank0.engine"
            engine_fpath = engine_dir / engine_name

            builder.build_engine(None, None, builder.batch_size, engine_fpath)


class LLAMA2(LegacyBuilder):
    """Temporary spoofing class to wrap around Mitten to adhere to the old API.
    """

    def __init__(self, args):
        super().__init__(LLAMA2EngineBuilderOp(**args))
