# Copyright (C) 2021 THL A29 Limited, a Tencent company.
# All rights reserved.
# Licensed under the BSD 3-Clause License (the "License"); you may
# not use this file except in compliance with the License. You may
# obtain a copy of the License at
# https://opensource.org/licenses/BSD-3-Clause
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.
# See the AUTHORS file for names of contributors.

import torch
from tests.bert_classification import BertForSequenceClassification, get_bert_data_loader
from transformers import BertConfig
import enum
import time
import sys

from checkpoint import checkpoint
import logging
import torch
from utils import see_memory_usage
from fp16 import FP16_Module, FP16_Optimizer
import time
import argparse

from client import HybridPSClient, PSTensorStatus, AccessType
from manager import HybridPSManager
from client import setup_hybrid_ps_hooks
# from utils.zero_hook import HookedModule
from ops import CPUAdam, TorchAdam
import utils.global_timer as global_timer

parser = argparse.ArgumentParser(
    description='Checkpointing for Memory Saving.')
parser.add_argument('--use_ckp',
                    dest='use_ckp',
                    action='store_true',
                    help='using checkpointing for memory saveing.')
parser.add_argument('--res_check',
                    dest='res_check',
                    action='store_true',
                    help='check results correctness of checkpointing.')
parser.add_argument('--use_fp16',
                    dest='use_fp16',
                    action='store_true',
                    help='using FP16 for training.')
parser.add_argument('--use_ps',
                    dest='use_ps',
                    action='store_true',
                    help='using Hybrid PS for training.')


def check_grads_status(model, status):
    for name, param in model.named_parameters(recurse=True):
        param_status = param.ps_attr.get_status(AccessType.GRAD)
        assert param_status == status, f"{name} {param.ps_attr.ps_shape} {param_status} vs {status}"


def show_params(model, is_ps, step):
    print(f'show params {step}')
    for name, param in model.named_parameters(recurse=True):
        print(
            name,
            torch.sum(param) if not is_ps else torch.sum(param.ps_data_tensor),
            param.shape, param.requires_grad)


def time_profiler():
    client_prepare_device_elapse = global_timer.client_prepare_device_elapse
    client_access_elapse = global_timer.client_access_elapse
    client_release_elapse = global_timer.client_release_elapse
    cpu_adam_elapse = global_timer.cpu_adam_elapse
    cpu_adam_f_elapse = global_timer.cpu_adam_f_elapse

    client_delete_free_chunks_elapse = global_timer.client_delete_free_chunks_elapse

    logging.info(f'client access elapse')
    logging.info(f'* client_access_elapse {client_access_elapse} ')
    logging.info(
        f'* chunk_list_access_param_elapse {global_timer.chunk_list_access_param_elapse}'
    )
    logging.info(
        f'* client_access_part2_elapse(prepare_device + chunk_move) {global_timer.client_access_part2_elapse}'
    )
    logging.info(
        f'** client_prepare_device_elapse {client_prepare_device_elapse}')
    logging.info(
        f'*** client_prepare_device_manager_elapse {global_timer.client_prepare_device_manager_elapse}'
    )
    logging.info(
        f'*** chunk_to_move_out_for_room_making_elapse {global_timer.chunk_to_move_out_for_room_making_elapse}'
    )

    logging.info(
        f'** client_chunk_move_elapse {global_timer.client_chunk_move_elapse}')
    logging.info(f'** chunk_move_elapse {global_timer.chunk_move_elapse}')
    logging.info(
        f'*** cpu_gpu_move_elapse {global_timer.cpu_gpu_move_elapse} sec, times {global_timer.cpu_gpu_move_times}, amount {global_timer.cpu_gpu_move_data_amount/1e6} MB, Bandwidth {global_timer.cpu_gpu_move_data_amount/1e6/(global_timer.cpu_gpu_move_elapse + 1e-10)} MB/s'
    )

    logging.info(
        f'* cpu_adam_elapse {cpu_adam_elapse} cpu_adam_f_elapse {cpu_adam_f_elapse}'
    )

    logging.info(f'client release elapse')
    logging.info(f'* client_release_elapse {client_release_elapse}')
    logging.info(
        f'* client_delete_free_chunks_elapse {client_delete_free_chunks_elapse} memory_delete_elapse {global_timer.memory_delete_elapse} delete_free_chunks_part1 {global_timer.delete_free_chunks_part1}'
    )


def get_model_size(model):
    numel = 0
    for name, param in model.named_parameters(recurse=True):
        numel += param.numel()
    print(f"model size {numel/1e9} B")


def calculate_model_size(config):
    V = config.vocab_size
    N = config.num_attention_heads
    H = config.hidden_size
    L = config.num_hidden_layers
    P = config.max_position_embeddings
    numel = (V + P + (L + 1) * N + 5) * H + (L * N + 1) * (H**2)
    Embedding_numel = H * (V + P + 4)
    QKV_numel = (H * H + H) * 3
    MLP_numel = H * (4 * H) + (4 * H) + (4 * H) * H + H
    print(f"Embedding_numel layer {Embedding_numel/1e9} B")
    print(f"QKV_numel layer {QKV_numel/1e9} B")
    print(f"MLP_numel layer {MLP_numel/1e9} B")
    print(f"calcalated model size {numel/1e9} B")


def calucate_MAC(config, batch_size, sequence_length):
    B = batch_size
    S = sequence_length
    V = config.vocab_size
    N = config.num_attention_heads
    H = config.hidden_size
    L = config.num_hidden_layers
    P = config.max_position_embeddings
    cisg_total_macs = 72 * B * S * N * H**2 + 12 * B * N * H * S**2
    nvidia_total_macs = 96 * B * S * L * H**2 * (1 + S / (6 * H) + V /
                                                 (16 * L * H))

    print(f'cisg_total_macs total MACs {cisg_total_macs}')
    print(f'nvidia total MACs {nvidia_total_macs}')
    print(f'diff csig/nvidia {cisg_total_macs / nvidia_total_macs}')
    return nvidia_total_macs


def test_bert_model(is_ckp: bool = False,
                    is_fp16: bool = False,
                    is_ps: bool = False,
                    batch_size=32,
                    hidden_dim=768,
                    sequence_length=256,
                    num_layer=12,
                    stop_step=10):
    logging.info(f'test a simple model with checkpoit {is_ckp} FP16 {is_fp16}')
    logging.info(
        f'batch_size {batch_size}, hidden_dim {hidden_dim}, sequence_length {sequence_length}, num_layer {num_layer}'
    )

    device = torch.device('cuda:0')

    if is_ckp:
        cfg = BertConfig(gradient_checkpointing=True,
                         hidden_dim=hidden_dim,
                         max_position_embeddings=sequence_length,
                         num_hidden_layers=num_layer)
    else:
        cfg = BertConfig(hidden_dim=hidden_dim,
                         max_position_embeddings=sequence_length,
                         num_hidden_layers=num_layer)
    model = BertForSequenceClassification(cfg)
    get_model_size(model)
    calculate_model_size(cfg)

    total_macs = calucate_MAC(cfg, batch_size, sequence_length)

    model.cuda()
    model.train()

    see_memory_usage(
        f"ckp {is_ckp} fp16 {is_fp16} ps {is_ps} after model init", force=True)

    if is_fp16:
        model = FP16_Module(model)

    data_loader = get_bert_data_loader(
        batch_size=batch_size,
        total_samples=1000,
        sequence_length=sequence_length,
        device=device,
        data_type=torch.half if is_fp16 else torch.float)

    loss_res = []

    if is_ps:
        manager = HybridPSManager()
        manager.init([1024 * 1024 * 1024 * 2] * 1,
                     [1024 * 1024 * 1024 * 4 * 4])
        # chunk 512 MB, good for CPU-GPU bandwidth
        client = HybridPSClient(gpu_index=0,
                                default_chunk_size=1024 * 1024 * 128)

        optimizer = CPUAdam(client, model.parameters(), lr=0.001)
        # optimizer = TorchAdam(model.parameters(), lr=0.001)
        setup_hybrid_ps_hooks(model, client)

        # hook_module = HookedModule(model, client)
        # hook_module.setup_zero_stage3_hooks()
        # model = hook_module.module
    else:
        optimizer = TorchAdam(model.parameters(), lr=0.001)

    if is_fp16:
        optimizer = FP16_Optimizer(optimizer, client=client if is_ps else None)

    if is_ps:
        client.register_model_optimizer(model, optimizer)

    start_time = time.time()
    for n, batch in enumerate(data_loader):
        step_start_time = time.time()
        output = model(input_ids=batch[0], labels=batch[1])
        loss = output.loss
        logits = output.logits
        # if torch.distributed.get_rank() == 0:
        print(f"LOSS: {loss.item()}")
        loss_res.append(loss.item())

        if is_fp16:
            optimizer.zero_grad(set_grads_to_None=True)
            optimizer.backward(loss, update_master_grads=False)
            # TODO(jiaruifang) 前三层embedding没有post-backward hook的触发
            # 最好有hook可以在每次BWD之后，把所有参数的
            # 状态更新
            if is_ps:
                client.release_all_data_grad(PSTensorStatus.HOLD)
                # check_grads_status(model, PSTensorStatus.HOLD)
        else:
            optimizer.zero_grad()
            loss.backward()
            # is_ps, 此时grad应该都是HOLD状态
            if is_ps:
                client.release_all_data_grad(PSTensorStatus.HOLD)
                # check_grads_status(model, PSTensorStatus.HOLD)

        if is_fp16:
            # pass
            optimizer.update_master_grads()

        # chunk 0和 chunk 1还在compute状态
        optimizer.step()
        see_memory_usage(
            f"ckp {is_ckp} fp16 {is_fp16} ps {is_ps}  after step {n}",
            force=True)

        # if is_ps:
        #     # TODO(jiaruifang) debug info
        #     logging.info(f'show chunk schema in step {n}')
        #     for info in client.chunk_tensor_index.generate_all_tensor_info():
        #         info.showme()

        # if is_ps:
        #     client.release_all_data_grad(PSTensorStatus.HOLD)
        setp_elapse = time.time() - step_start_time
        logging.info(
            f"ckp {is_ckp} fp16 {is_fp16} ps {is_ps}  elapse {setp_elapse}, sec/iter, {total_macs/1e9/setp_elapse} GFlops"
        )
        if n == stop_step: break

    elapse = time.time() - start_time
    logging.info(
        f"ckp {is_ckp} fp16 {is_fp16} ps {is_ps}  elapse {elapse/(stop_step+1)} sec/iter total elapse {elapse} sec"
    )
    logging.info(f"{total_macs/1e9/ (elapse/(stop_step+1))} GFlops")
    time_profiler()
    return loss_res


if __name__ == "__main__":
    logging.basicConfig(
        format=
        '%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
        datefmt='%Y-%m-%d:%H:%M:%S',
        level=logging.INFO)

    args = parser.parse_args()
    use_ckp = args.use_ckp
    use_fp16 = args.use_fp16
    use_ps = args.use_ps
    # 检查结果正确性
    res_check = args.res_check

    # hidden_dim 1024, batch 16, seqence_leng 1024, ckp True.
    # PS is able to run the training, while PyTorch failed.

    plan = "B"
    if plan == "A":
        # HybridPS可以，PyTorch不可以
        # use_ckp: True, use_fp16: True, adam default on CPU, not interleave data and grad
        # manager.init([1024 * 1024 * 1024 * 2] * 1, [1024 * 1024 * 1024 * 4 * 4])
        # client = HybridPSClient(gpu_index=0,
        #                         default_chunk_size=1024 * 1024 * 128)
        # 949.234980710749 GFlops (RTX 2060 peak 6.451 TFLOPS)

        # use_ckp: True, use_fp16: True, adam default on GPU, interleave data and grad
        # 39.95365033945278 GFlop, 30% nvidia-smi (adam on GPU)
        # client = HybridPSClient(gpu_index=0,
        #                         default_chunk_size=1024 * 1024 * 32)

        # 2287.22 Gflops B = 4
        # 3047.70 Gflops B = 8
        # 3047.70 Gflops B = 16
        hidden_dim = 3072  #2048
        batch_size = 16
        sequence_length = 1024
        num_layer = 60
    elif plan == 'B':
        # HybridPS and Pytorch都可以
        # Pytorch: 1.2852387428283691 sec
        # HybridPS: 6.879993915557861 sec
        # client_prepare_device_elapse 0.0 client_access_elapse 2.211916446685791 client_release_elapse 2.442206859588623
        # cpu_adam_elapse 3.7840416431427 cpu_adam_f_elapse 3.7840394973754883
        hidden_dim = 1536
        batch_size = 8
        sequence_length = 1024
        num_layer = 12
    elif plan == 'C':
        # use ckp
        # HybridPS and PyTorch is OK
        # 没有prepare device开销
        hidden_dim = 768
        batch_size = 8
        sequence_length = 1024
        num_layer = 12
    elif plan == 'D':
        # use ckp
        # HybridPS and PyTorch is OK
        # 没有prepare device开销
        hidden_dim = 4096  #2048
        batch_size = 2
        sequence_length = 1536
        num_layer = 120

    if not res_check:
        # 训练参数，可以自己定义
        torch.manual_seed(0)
        test_bert_model(is_ckp=use_ckp,
                        is_fp16=use_fp16,
                        is_ps=use_ps,
                        batch_size=batch_size,
                        hidden_dim=hidden_dim,
                        sequence_length=sequence_length,
                        num_layer=num_layer)

    # calculate_mem_need(hidden_dim = hidden_dim, batch_size = batch_size, is_fp16 = use_fp16)

    if res_check:
        torch.manual_seed(0)
        loss_list = test_bert_model(is_ckp=use_ckp,
                                    is_fp16=use_fp16,
                                    is_ps=True,
                                    hidden_dim=hidden_dim,
                                    batch_size=batch_size,
                                    sequence_length=sequence_length,
                                    num_layer=num_layer)

        torch.cuda.empty_cache()
        print("*" * 50)
        torch.manual_seed(0)
        loss_ref_list = test_bert_model(is_ckp=use_ckp,
                                        is_fp16=use_fp16,
                                        is_ps=False,
                                        hidden_dim=hidden_dim,
                                        batch_size=batch_size,
                                        sequence_length=sequence_length,
                                        num_layer=num_layer)

        print('ps', loss_list)
        print('ref', loss_ref_list)
        import numpy as np
        print(np.array(loss_list) - np.array(loss_ref_list))
        for loss, loss_ref in zip(loss_list, loss_ref_list):
            assert loss == loss_ref
