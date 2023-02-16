#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

#!/usr/bin/env python3

import os
import tempfile
import unittest
import uuid

import torch
from torch import distributed as dist, nn
from torch.distributed._composable import fully_shard
from torch.distributed._shard.sharded_tensor import ShardedTensor

from torch.distributed.checkpoint import (
    FileSystemReader,
    FileSystemWriter,
    load_state_dict,
    save_state_dict,
)
from torch.distributed.fsdp.fully_sharded_data_parallel import FullyShardedDataParallel
from torch.distributed.fsdp.wrap import ModuleWrapPolicy
from torch.distributed.launcher.api import elastic_launch, LaunchConfig
from torch.distributed.optim import (
    _apply_optimizer_in_backward as apply_optimizer_in_backward,
)
from torchrec.distributed.shard import shard as trec_shard
from torchrec.distributed.sharding_plan import row_wise
from torchrec.distributed.test_utils.test_model import ModelInput, TestSparseNN
from torchrec.modules.embedding_configs import EmbeddingBagConfig
from torchrec.optim.keyed import CombinedOptimizer, KeyedOptimizerWrapper
from torchrec.optim.optimizers import in_backward_optimizer_filter
from torchrec.optim.rowwise_adagrad import RowWiseAdagrad
from torchrec.optim.warmup import WarmupOptimizer, WarmupPolicy, WarmupStage
from torchrec.test_utils import skip_if_asan


class FullyShardTest(unittest.TestCase):
    @classmethod
    def _run(cls, param_path: str, opt_path: str) -> None:  # noqa
        rank = int(os.environ["LOCAL_RANK"])
        world_size = int(os.environ["WORLD_SIZE"])
        device: torch.device = torch.device(f"cuda:{rank}")
        torch.cuda.set_device(device)
        dist.init_process_group(backend="nccl")
        num_float_features = 32

        tables = [
            EmbeddingBagConfig(
                num_embeddings=(i + 1) * 10,
                embedding_dim=(i + 1) * 4,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
            )
            for i in range(3)
        ]
        weighted_tables = [
            EmbeddingBagConfig(
                num_embeddings=(i + 1) * 10,
                embedding_dim=(i + 1) * 4,
                name="weighted_table_" + str(i),
                feature_names=["weighted_feature_" + str(i)],
            )
            for i in range(2)
        ]
        m = TestSparseNN(
            tables=tables,
            num_float_features=num_float_features,
            weighted_tables=weighted_tables,
            dense_device=device,
        )
        apply_optimizer_in_backward(
            RowWiseAdagrad,
            m.sparse.parameters(),
            {"lr": 0.01},
        )
        m.sparse.ebc = trec_shard(
            module=m.sparse.ebc,
            device=device,
            plan=row_wise(),
        )
        m.sparse.weighted_ebc = trec_shard(
            module=m.sparse.weighted_ebc,
            device=device,
            plan=row_wise(),
        )
        m.dense = fully_shard(
            m.dense,
            device_id=device.index,
            policy=ModuleWrapPolicy({nn.Linear}),
        )
        m.over = fully_shard(
            m.over,
            device_id=device.index,
            policy=ModuleWrapPolicy({nn.Linear}),
        )

        dense_opt = KeyedOptimizerWrapper(
            dict(in_backward_optimizer_filter(m.named_parameters(), include=False)),
            lambda params: torch.optim.Adam(
                params,
                lr=0.01,
                betas=(0.9, 0.999),
                eps=1e-5,
                weight_decay=1e-05,
            ),
        )
        optims = []
        sparse_grad_parameter_names = set()
        for name, p in in_backward_optimizer_filter(m.named_parameters(), include=True):
            # Add learning rate scheduler
            warmup = WarmupOptimizer(
                # pyre-ignore
                p._in_backward_optimizers[0],
                [
                    WarmupStage(
                        policy=WarmupPolicy.LINEAR,
                        max_iters=1000,
                        value=0.1,
                        lr_scale=1.0,
                    )
                ],
                lr=0.01,  # initial learning rate
                param_name="__sparse_warmup",
            )
            optims.append((name, warmup))
            sparse_grad_parameter_names.add(name)
        assert len(sparse_grad_parameter_names) == 5
        fused_opt_scheduled = CombinedOptimizer(optims)
        dense_opt_scheduled = WarmupOptimizer(
            dense_opt,
            [
                WarmupStage(
                    policy=WarmupPolicy.LINEAR,
                    max_iters=1000,
                    value=0.15,
                    lr_scale=1.0,
                )
            ],
            lr=0.01,
            param_name="__dense_warmup",
        )
        opt: CombinedOptimizer = CombinedOptimizer(
            [fused_opt_scheduled, (dense_opt_scheduled)]
        )
        # Runs a dummy optimizer step, which allows to initialize
        #   optimizer state, which is typically lazy.
        # This allows us to do in-place loading of optimizer state from a checkpoint.
        # Remark that fused optimizer needs special case as its states are ShardedTensors.
        # This is the reason we need to pass the sparse_grad_parameter_names as parameters.
        opt.init_state(sparse_grad_parameter_names)
        opt.save_param_groups(True)
        model_param_names = set(dict(m.named_parameters()).keys())
        opt_param_keys = set(opt.params.keys())
        assert model_param_names.issubset(opt_param_keys)

        ######## run one iteration ########
        _, local_batch = ModelInput.generate(
            batch_size=8,
            world_size=world_size,
            num_float_features=num_float_features,
            tables=tables,
            weighted_tables=weighted_tables,
        )
        batch = local_batch[0].to(device)
        m(batch)[1].sum().backward()
        opt.step()

        state_dict = m.state_dict()
        param_writer = FileSystemWriter(path=param_path)
        param_reader = FileSystemReader(path=param_path)
        save_state_dict(state_dict, param_writer)

        # use FSDP.optim_state_dict() API
        opt_state_dict = FullyShardedDataParallel.optim_state_dict(m, opt)
        opt_writer = FileSystemWriter(path=opt_path)
        opt_reader = FileSystemReader(path=opt_path)
        # use Distributed checkpointing API
        save_state_dict(opt_state_dict, opt_writer)

        p_sum = torch.zeros(1, device=device)
        for p in m.parameters():
            with torch.no_grad():
                if isinstance(p, ShardedTensor):
                    if not p.local_shards():
                        continue
                    p = p.local_tensor()
                p_sum += p.sum()
                p.zero_()
                assert p.sum() == 0
        o_sum = torch.zeros(1, device=device)
        for p_v in opt.state_dict()["state"].values():
            for name, t in p_v.items():
                if name == "step":
                    continue
                if isinstance(t, ShardedTensor):
                    if not t.local_shards():
                        continue
                    t = t.local_tensor()
                o_sum += t.sum()
                t.zero_()
                assert t.sum() == 0

        load_state_dict(state_dict, param_reader)
        missing, unexpected = m.load_state_dict(state_dict)
        assert len(missing) == 0 and len(unexpected) == 0

        load_state_dict(opt_state_dict, opt_reader)
        # use FSDP.optim_state_dict_to_load() API
        new_opt_state_dict = FullyShardedDataParallel.optim_state_dict_to_load(
            opt_state_dict, m, opt, is_named_optimizer=True
        )
        opt.load_state_dict(new_opt_state_dict)

        p_sum_loaded = torch.zeros(1, device=device)
        for p in m.parameters():
            with torch.no_grad():
                if isinstance(p, ShardedTensor):
                    if not p.local_shards():
                        continue
                    p = p.local_tensor()
                p_sum_loaded += p.sum()
        assert p_sum.allclose(p_sum_loaded)

        o_sum_loaded = torch.zeros(1, device=device)
        for p_v in opt.state_dict()["state"].values():
            for name, t in p_v.items():
                if name == "step":
                    continue
                if isinstance(t, ShardedTensor):
                    if not t.local_shards():
                        continue
                    t = t.local_tensor()
                o_sum_loaded += t.sum()
        assert o_sum.allclose(o_sum_loaded)

    @skip_if_asan
    # pyre-ignore[56]
    @unittest.skipIf(
        torch.cuda.device_count() <= 1,
        "Not enough GPUs, this test requires at least two GPUs",
    )
    def test_composable_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as param_path, tempfile.TemporaryDirectory() as opt_path:
            lc = LaunchConfig(
                min_nodes=1,
                max_nodes=1,
                nproc_per_node=2,
                run_id=str(uuid.uuid4()),
                rdzv_backend="c10d",
                rdzv_endpoint=os.path.join(tmpdir, "rdzv"),
                rdzv_configs={"store_type": "file"},
                start_method="spawn",
                monitor_interval=1,
                max_restarts=0,
            )
            elastic_launch(config=lc, entrypoint=self._run)(param_path, opt_path)
