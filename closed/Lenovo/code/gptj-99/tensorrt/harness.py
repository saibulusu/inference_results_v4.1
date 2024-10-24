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

from code.common import logging, args_to_string, run_command, dict_get
from code.common.harness import BaseBenchmarkHarness
import code.common.arguments as common_args


class GPTJHarness(BaseBenchmarkHarness):
    """GPTJ harness."""

    def __init__(self, args, benchmark):
        super().__init__(args, benchmark)
        custom_args = [
            "gpu_inference_streams",
            "gpu_copy_streams",
            "devices",
            "kvcache_free_gpu_mem_frac",
            "tensor_parallelism",
            "use_inflight_batching",
            "enable_sort",
            "llm_gen_config_path",
            "use_token_latencies",
            "trtllm_batching_mode",
            "trtllm_batch_sched_policy",
        ]
        self.flag_builder_custom_args = common_args.LOADGEN_ARGS + common_args.SHARED_ARGS + custom_args
        self.env_vars["TRTLLM_DISABLE_XQA_JIT"] = "1"

        if (vboost_slider := dict_get(args, 'vboost_slider', 0)) != 0:
            def set_vboost(value: int = 0):
                logging.info(f"setting vboost to {value if value != 0 else 'gpu default'}")
                run_command(f"sudo nvidia-smi boost-slider --vboost {value}")

            self.before_fn = lambda: set_vboost(vboost_slider)
            self.after_fn = set_vboost

    def _get_harness_executable(self):
        """Return path to GPT harness binary."""
        return "./build/bin/harness_llm"

    def _build_custom_flags(self, flag_dict):
        s = args_to_string(flag_dict) + " --scenario " + self.scenario.valstr() + " --model " + self.name
        return s

    def _get_engine_fpath(self, device_type, _, batch_size):
        # Override this function to pick up the right engine file
        return f"{self.engine_dir}/bs{batch_size}-{self.config_ver}/rank0.engine"
