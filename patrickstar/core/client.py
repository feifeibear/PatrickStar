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

import os
from typing import List

import torch

import patrickstar.utils.global_timer as global_timer
from patrickstar.utils import logger, get_world_size, get_rank
from .chunk_list import ChunkList, ChunkType
from .chunk_tensor_index import ChunkTensorIndex
from .const import AccessType, ChunkStatus, TensorStatus, TrainingStage
from .hook import setup_patrickstar_hooks
from .parameter import register_param, is_param_registered, ParamType


class PatrickStarClient(object):
    r"""The client for managing chunks."""

    def __init__(self, rank: int, default_chunk_size: int):
        self.pid = os.getpid()

        self.local_rank = rank

        self.module = None

        self.default_chunk_size = default_chunk_size
        self.chunk_tensor_index = ChunkTensorIndex(self.default_chunk_size)
        self.chunk_list = ChunkList(self.local_rank)

        self._time_profile = True

        if torch.distributed.is_initialized():
            self.cpu_comm_group = torch.distributed.new_group(backend="gloo")
        else:
            self.cpu_comm_group = None

        self.dummy_param_list = []
        self.torch_param_list = []
        self.param_fp16_to_param_fp32_map = {}
        self.chunk_based_param_fp16 = []

        # for post backward hook
        self.grad_accs = []

    def init(self, model, optimizer):
        r"""Initialize and store model and optimizer"""

        self.module = model
        self.optimizer = optimizer
        if get_rank() == 0:
            self.display_chunk_info()
        # Here we register the forward and backward hooks.
        self.register_model_hook(model)

    def append_chunk(self, data_type, chunk_type, is_dummy=False):
        r"""Append a new chunk to chunk_list and chunk_tensor_index.

        Args:
            data_type: :class:`torch.dtype`.
            chunk_type: :class:`ChunkType`.
            is_dummy: bool.
        Returns:
            chunk_id of the newly created chunk and
            (comm_group_id, comm_group_offset)
        """
        chunk_id = self.chunk_list.generate_chunk_id()
        comm_group_id, comm_group_offset = self.chunk_list.new_chunk(
            chunk_id,
            self.default_chunk_size,
            data_type,
            is_dummy=is_dummy,
            chunk_type=chunk_type,
        )
        self.chunk_tensor_index.add_chunk(
            chunk_id,
            comm_group_id,
            comm_group_offset,
            chunk_type,
        )
        return chunk_id, (comm_group_id, comm_group_offset)

    def append_dummy_chunk(self, data_type: torch.dtype, chunk_type: ChunkType):
        r"""Append a dummy chunk to the corresponding chunk_list"""
        chunk_id, (comm_group_id, comm_group_offset) = self.append_chunk(
            torch.half, chunk_type, is_dummy=True
        )

        dummy = torch.nn.Parameter(
            torch.tensor([], dtype=data_type), requires_grad=False
        )
        # Add a dummy param to dummy chunk, so that the chunk can be set in HOLD status.
        register_param(
            dummy, ParamType.CHUNK_BASED, torch.half, f"dummy_{comm_group_id}"
        )
        self.dummy_param_list.append(dummy)
        self.chunk_tensor_index.add_tensor(
            chunk_id,
            self.dummy_param_list[-1].ps_attr.data_id(),
            0,
            dummy.numel(),
            self.dummy_param_list[-1],
            AccessType.DATA,
        )

        logger.info(
            f"Append a dummy chunk to the Chunk List {chunk_type} "
            f"comm group ({comm_group_id} {comm_group_offset})"
        )

    def delete_param(self, param, access_type):
        """
        TODO(jiaruifang) Remove tensor of the param
        """
        chunk_id = self.chunk_tensor_index.get_chunk_id(param, access_type)
        self.chunk_tensor_index.delete_tensor(chunk_id, param, access_type)

    def append_tensor(
        self,
        param_list: List[torch.nn.Parameter],
        data_type: torch.dtype,
        access_type: AccessType,
        chunk_type: ChunkType,
    ):
        r"""Append params to the last chunk of type `chunk_type`.

        Append the whole list of param into the same chunk. If the last
        chunk doesn't fit, append a new chunk and try to insert params in it.

        Args:
            param_list: list of `torch.nn.Parameter`.
            data_type: :class:`torch.dtype`. Can be different from param.
            access_type: :class:`AccessType`.
            chunk_type: :class:`ChunkType`.
        """
        assert isinstance(data_type, torch.dtype)
        assert isinstance(access_type, AccessType)
        if not self.chunk_list.is_empty(chunk_type):
            last_chunk_id = self.chunk_list.last_chunk_id(chunk_type)
            if self.chunk_tensor_index.try_insert_tensor_list(
                last_chunk_id, param_list, access_type
            ):
                return
        chunk_id, _ = self.append_chunk(data_type, chunk_type)
        if not self.chunk_tensor_index.try_insert_tensor_list(
            chunk_id, param_list, access_type
        ):
            raise RuntimeError(
                f"Can not append a tensor to chunk_tensor_index."
                f"Overall size of param list is larger than the default chunk size {self.default_chunk_size}."
            )
        return

    def append_tensor_as_ref(
        self, param, data_type, access_type, chunk_type, ref_param
    ):
        r"""Append param to the last chunk with regard to the ref_param's location.

        When adding optimizer params, e.g. the variance and momentum of adam to
        chunk list, we hope they are of the same order as their corresponding
        fp16 params. Here the `param` is the optimizer params and `ref_param` is
        the fp16 param.
        Notice that the chunk_id of param and ref_param are different.

        Args:
            param: :class:`torch.nn.Parameter`.
            data_type: :class:`torch.dtype`. Can be different from param.
            access_type: :class:`AccessType`.
            chunk_type: :class:`ChunkType`.
            ref_param: :class:`torch.nn.Parameter`.
        """
        chunk_id = self.chunk_tensor_index.get_optimizer_state_chunk_id(
            ref_param, access_type, chunk_type
        )
        if chunk_id is None:
            chunk_id, _ = self.append_chunk(data_type, chunk_type)
        if not self.chunk_tensor_index.try_insert_tensor(chunk_id, param, access_type):
            raise RuntimeError("Failed to insert optimizer param w.r.t its ref_param.")
        self.chunk_tensor_index.register_optimizer_state_chunk_id(
            ref_param, access_type, chunk_type, chunk_id
        )

    def param_fp16_chunks_max_mem_usage(self):
        r"""Return the total memory used by param fp16 chunks in bytes.

        In distributed environment, the return value includes remote chunks
        from allgather.
        """
        world_size = get_world_size()
        # 本进程自己管理的Chunk，和Group Chunk Buff会分配的Chunk
        return (
            self.chunk_tensor_index.chunk_num(ChunkType.PARAM_FP16)
            * self.default_chunk_size
            * 2
            / world_size
            + (world_size - 1) * self.default_chunk_size * 2
        )

    def set_all_tensors_status_in_chunk(self, chunk_id, new_status):
        r"""Set the status of all tensors in a chunk.

        Notice that the status of the chunk will change as well.
        And this method has nothing to do with whether the payload of
        the chunk is allocated or not.
        """
        for info in self.chunk_tensor_index.generate_tensor_info_in_order(chunk_id):
            param = info.param
            access_type = info.access_type
            old_status = param.ps_attr.get_status(access_type)
            self.chunk_list.update_status(chunk_id, old_status, new_status)
            param.ps_attr.set_status(new_status, access_type)

    def register_model_hook(self, model):
        setup_patrickstar_hooks(model, self)

    def chunk_ids_generator(self, chunk_type: ChunkType):
        return self.chunk_list.chunk_ids_generator(chunk_type)

    def is_local_param(self, param, access_type):
        r"""Check if param is in local chunk"""
        chunk_id = self.chunk_tensor_index.get_chunk_id(param, access_type)
        return self.chunk_tensor_index.is_local_chunk(chunk_id)

    def _fetch_remote_chunks(
        self,
        chunk_id_list,
        local_chunk_id,
        compute_device,
        param_name,
    ):
        r"""Fetch the remote chunks to local.

        Args:
            chunk_id_list: list of int. The id of the chunks in a same comm group.
            local_chunk_id: int. The id of the local chunk in the comm group.
            compute_device: :class:`torch.device`.
            param_name: str.
        """
        rank = get_rank()

        # During FWD, when there are param in the chunk group being visited for
        # the first time, collect the chunk group to local.
        # How can we determine if a chunk group is being visited for the first time,
        # so that we can trigger the correct allgather?
        # When the first param is visited, the remote chunk should be of status
        # RELEASED, therefore, we do the allgather when the status of chunks are
        # changing form HOLD_AFTER_FWD(HOLD_ADFTER_BWD) to RELEASED.
        has_released_chunk = False
        for i in chunk_id_list:
            if self.chunk_list[i].get_status() == ChunkStatus.RELEASED:
                has_released_chunk = True
                break
        if not has_released_chunk:
            return

        if self._time_profile:
            global_timer.my_timer.start_profile("CLIENT_fetch_remote_chunks")

        logger.debug(
            f"rank {rank} fetch {param_name} remote chunks {chunk_id_list} local chunk {local_chunk_id}"
        )
        allgather_payload_buff = []

        comm_data_amount = 0
        for chunk_id in chunk_id_list:
            if chunk_id != local_chunk_id:
                self.chunk_list.prepare_device(
                    compute_device, self.chunk_list[chunk_id].get_chunk_space()
                )
                # TODO(jiaruifang) We may reuse a comm_buffer here.
                self.chunk_list[chunk_id].allocate_payload(compute_device)
                # Make sure the newly allocated chunk is not moved to other deviced
                # before allgather.
                self.chunk_list[chunk_id].pin()
            self.set_all_tensors_status_in_chunk(chunk_id, TensorStatus.HOLD)
            allgather_payload_buff.append(self.chunk_list[chunk_id].payload)
        comm_data_amount = (
            len(allgather_payload_buff) * allgather_payload_buff[0].numel() * 2
        )  # half = 2 bytes
        for chunk_id in chunk_id_list:
            self.chunk_list[chunk_id].unpin()

        assert (
            torch.distributed.is_initialized()
        ), "torch distributed is not initialized during allgather"
        if self._time_profile:
            global_timer.my_timer.start_profile("CLIENT_fetch_remote_chunks_allgather")

        logger.info(f"rank {rank} allgather {chunk_id_list}")
        torch.distributed.all_gather(
            allgather_payload_buff,
            self.chunk_list[local_chunk_id].payload,
            async_op=False,
        )

        allgather_payload_buff = []
        if self._time_profile:
            global_timer.my_timer.finish_profile("CLIENT_fetch_remote_chunks_allgather")
            global_timer.data_move_cnter.update(
                "CLIENT_fetch_remote_chunks_allgather", comm_data_amount
            )
            global_timer.my_timer.finish_profile("CLIENT_fetch_remote_chunks")

    def access_dist(
        self,
        param: torch.nn.Parameter,
        access_type: AccessType,
        compute_device: torch.device,
    ) -> torch.Tensor:
        r"""Visit tensor of param in distributed environment.

        Notice that this method also works at standalone circumstances.

        Args:
            param: :class:`torch.nn.Parameter`. The param to visit.
            access_type: :class:`AccessType`.
            compute_device: :class:`torch.device`.
            training_stage: :class:`TrainingStage`.
        Returns:
            The tensor of the params.
        """

        assert is_param_registered(
            param
        ), "Client can only access_dist tensor registered for PatrickStar."
        if param.ps_attr.param_type == ParamType.TORCH_BASED:
            if access_type == AccessType.DATA:
                return param.data
            elif access_type == AccessType.GRAD:
                return param.grad
            else:
                raise RuntimeError(f"{access_type} is not supported")

        if self._time_profile:
            global_timer.my_timer.start_profile("CLIENT_access_dist")

        # 1. Prepare the memory of the chunks. If the chunk is not one the
        #   compute device, then we need to move or allocate.
        chunk_id = self.chunk_tensor_index.get_chunk_id(param, access_type)

        rank = get_rank()

        if get_world_size() > 1:
            chunk_id_list = self.chunk_tensor_index.chunk_ids_of_comm_group(chunk_id)
            local_chunk_id = chunk_id_list[rank]

            logger.debug(
                f"rank {rank} access_dist access tensor {param.ps_attr.name} "
                f"local_chunk_id {local_chunk_id} chunk_id_list {chunk_id_list}"
            )

            # 1.1 Move the local chunk to compute device.
            self.chunk_list.access_chunk(local_chunk_id, compute_device)

            # Prevent the local chunk being moved to other devices during _fetch_remote_chunks.
            # Because its status is HOLD, pin.
            self.chunk_list[local_chunk_id].pin()

            # 1.2 Fetch the remote chunks to local.
            self._fetch_remote_chunks(
                chunk_id_list,
                local_chunk_id,
                compute_device,
                param.ps_attr.name,
            )
            self.chunk_list[local_chunk_id].unpin()

        # If the chunk is remote chunk and has been fetched before this `access_dist`,
        # The _fetch_remote_chunks will not do allgather. We need to move the chunk to
        # compute_device manually.
        self.chunk_list.access_chunk(chunk_id, compute_device)

        assert (
            self.chunk_list[chunk_id].payload is not None
        ), f"rank {rank} chunk id {chunk_id}' payload is None'"
        assert self.chunk_list[chunk_id].payload.device == compute_device, (
            f"rank {rank} chunk id {chunk_id}' payload is not on "
            f"{compute_device}, but on "
            f"{self.chunk_list[chunk_id].payload.device}"
        )

        # 2. Locate the param on the chunk.
        tensor_id = param.ps_attr.get_tensor_id(access_type)
        info = self.chunk_tensor_index.get_tensor_info(tensor_id)
        start_offset = info.start_offset
        numel = info.numel
        assert numel == param.ps_attr.numel, f"{numel} vs {param.ps_attr.numel}"

        param.ps_attr.set_tensor(
            self.chunk_list[chunk_id].payload.narrow(0, start_offset, numel),
            access_type,
        )

        # 3. Change the status of param tensor.
        # The status of chunk should be determined by the status of its tensors.
        old_status = param.ps_attr.get_status(access_type)

        # If the old status was FREE, we need to fill the param to zero.
        if old_status == TensorStatus.FREE:
            param.ps_attr.access_tensor(access_type).zero_()

        # Change the status of param to COMPUTE.
        self.chunk_list.update_status(chunk_id, old_status, TensorStatus.COMPUTE)
        param.ps_attr.set_status(TensorStatus.COMPUTE, access_type)

        if self._time_profile:
            global_timer.my_timer.finish_profile("CLIENT_access_dist")

        return param.ps_attr.access_tensor(access_type)

    def access(
        self,
        param: torch.nn.Parameter,
        access_type: AccessType,
        compute_device: torch.device,
    ):
        r"""Visit the data or grad of the local `param`.

        Steps:
            1. Find the chunk of the `param`.
            2.1 If the payload of the chunk exists,
                decide whether to move the chunk to `compute_device`.
            2.2 If the payload of the chunk does not exist,
                allocate the payload of on `compute_device`.
        Before moving or allocating, make sure there is enough space
        on the `compute_device`.

        Exceptions:
            The compute devices of 2 tensors in the same chunk is different.

        Notice that different from access_dist, this method will not do cross
        process communication.

        Args:
            param: :class:`torch.nn.Parameter`.
            access_type: :class:`AccessType`.
            compute_device: :class:`torch.device`.
        Returns:
            The tensor to access.
        """
        assert is_param_registered(
            param
        ), "client shall not access tensor not registered for PatrickStar"
        if param.ps_attr.param_type == ParamType.TORCH_BASED:
            if access_type == AccessType.DATA:
                return param.data
            elif access_type == AccessType.GRAD:
                return param.grad
            else:
                raise RuntimeError(f"{access_type} is not supported")

        if self._time_profile:
            global_timer.my_timer.start_profile("CLIENT_access")

        # 1. Prepare the memory of the chunks. If the chunk is not one the
        #   compute device, then we need to move or allocate.
        chunk_id = self.chunk_tensor_index.get_chunk_id(param, access_type)

        if chunk_id is None:
            raise RuntimeError(
                "FP16 training shall not meet tensors with no chunk assigned. "
                "Every tensor has to be assigned to a chunk during a tensor-chunk-mapping "
                "process before training."
            )

        self.chunk_list.access_chunk(chunk_id, compute_device)

        # 2. Locate the param on the chunk.
        tensor_id = param.ps_attr.get_tensor_id(access_type)
        info = self.chunk_tensor_index.get_tensor_info(tensor_id)
        start_offset = info.start_offset
        numel = info.numel
        assert numel == param.ps_attr.numel, f"{numel} vs {param.ps_attr.numel}"

        param.ps_attr.set_tensor(
            self.chunk_list[chunk_id].payload.narrow(0, start_offset, numel),
            access_type,
        )

        # 3. Change the status of param tensor.
        # The status of chunk should be determined by the status of its tensors.
        old_status = param.ps_attr.get_status(access_type)

        # If the old status was FREE, we need to fill the param to zero.
        if old_status == TensorStatus.FREE:
            param.ps_attr.access_tensor(access_type).zero_()

        self.chunk_list.update_status(chunk_id, old_status, TensorStatus.COMPUTE)
        param.ps_attr.set_status(TensorStatus.COMPUTE, access_type)

        if self._time_profile:
            global_timer.my_timer.finish_profile("CLIENT_access")
        return param.ps_attr.access_tensor(access_type)

    def access_data(self, param: torch.nn.Parameter, compute_device: torch.device):
        r"""move the PSTensor of param.data to `compute_device`."""
        return self.access(param, AccessType.DATA, compute_device)

    def access_grad(self, param: torch.nn.Parameter, compute_device: torch.device):
        r"""move the PSTensor of param.data to `compute_device`.

        NOTE() The device of grad should be determined by the device of param.data.
        """
        return self.access(param, AccessType.GRAD, compute_device)

    def release_dist(
        self,
        param: torch.nn.Parameter,
        access_type: AccessType,
        reset_to_status: TensorStatus,
        training_stage: TrainingStage,
        is_allreduce: bool,
    ):
        r"""Release the param in distributed environment.

        This means the data and grad of the param no longer need to
        stay in the current compute device.

        Steps:
            1. Update the status of tensor and chunk.
            2. If the chunk can be released,
                if `is_allreduce` is True, do reduce scatter to average the gradients.
                then released the payload.

        Args:
            param: :class:`torch.nn.Parameter`.
            access_type: :class:`AccessType`.
            reset_to_status: :class:`TensorStatus`. The status to reset tensor to.
            training_stage: :class:`TrainingStage`.
            is_allreduce: bool. Whether to do allreduce(reduce scatter).
                Notice that because user may use gradient checkpointing, the is_allreduce
                in TrainingStage.BWD doesn't equal to True.
        """
        if param.ps_attr.param_type == ParamType.TORCH_BASED:
            return

        if self._time_profile:
            global_timer.my_timer.start_profile("CLIENT_release_dist")
        rank = get_rank()

        assert isinstance(reset_to_status, TensorStatus)
        assert (
            training_stage == TrainingStage.FWD or training_stage == TrainingStage.BWD
        )
        assert torch.distributed.is_initialized()

        chunk_id = self.chunk_tensor_index.get_chunk_id(param, access_type)
        chunk_id_list = self.chunk_tensor_index.chunk_ids_of_comm_group(chunk_id)

        local_chunk_id = chunk_id_list[rank]

        logger.debug(
            f"rank {rank} release tensor {param.ps_attr.name} of chunk_id {chunk_id} to {reset_to_status}"
        )

        # Update the status of tensor and chunk.
        self.chunk_list.update_status(
            chunk_id, param.ps_attr.get_status(access_type), reset_to_status
        )
        param.ps_attr.set_status(reset_to_status, access_type)

        if access_type == AccessType.DATA:
            # NOTE(jiaruifang) device must be the same as the origin param.
            # Or it will affect hook of param.grad_fn.next_functions[0][0].
            param.data = torch.tensor(
                [], dtype=param.ps_attr.data_type, device=param.device
            )
        elif access_type == AccessType.GRAD:
            param.grad = None

        # Check if we finished using all tensors in all chunks of the chunk group,
        # then we can release the remote chunks.
        # The condition for releasing chunks are:
        #     FWD: All non-dummy chunks are of status HOLD_AFTER_FWD;
        #     BWD: All non-dummy chunks are of status HOLD_AFTER_BWD.
        world_size = get_world_size()
        if world_size > 1:
            all_chunks_ready = True
            for i in chunk_id_list:
                if training_stage == TrainingStage.FWD:
                    if (
                        not self.chunk_list[i].all_tensor_status(
                            TensorStatus.HOLD_AFTER_FWD
                        )
                        and not self.chunk_list[i].is_dummy()
                    ):
                        all_chunks_ready = False
                elif training_stage == TrainingStage.BWD:
                    if (
                        not self.chunk_list[i].all_tensor_status(
                            TensorStatus.HOLD_AFTER_BWD
                        )
                        and not self.chunk_list[i].is_dummy()
                    ):
                        all_chunks_ready = False

            if all_chunks_ready:
                if is_allreduce:
                    if self._time_profile:
                        global_timer.my_timer.start_profile(
                            "CLIENT_release_dist_reduce_scatter"
                        )
                    assert self.chunk_list[local_chunk_id].payload is not None
                    input_list = []
                    for i in chunk_id_list:
                        self.chunk_list.access_chunk(
                            i, torch.device(f"cuda:{self.local_rank}")
                        )
                        self.chunk_list[i].pin()
                        input_list.append(self.chunk_list[i].payload)
                    torch.distributed.reduce_scatter(
                        self.chunk_list[local_chunk_id].payload,
                        input_list,
                        op=torch.distributed.ReduceOp.SUM,
                        async_op=False,
                    )

                    self.chunk_list[local_chunk_id].payload /= world_size
                    if self._time_profile:
                        global_timer.data_move_cnter.update(
                            "CLIENT_release_dist_reduce_scatter",
                            self.chunk_list[local_chunk_id].payload.numel()
                            * 2
                            * world_size,
                        )
                        global_timer.my_timer.finish_profile(
                            "CLIENT_release_dist_reduce_scatter"
                        )

                # Remove the payload of remote chunks.
                for i in chunk_id_list:
                    self.chunk_list[i].unpin()
                    if i != local_chunk_id:
                        logger.debug(f"rank {rank} remove payload of chunk_id {i}")
                        self.chunk_list[i].release_payload()
                        self.set_all_tensors_status_in_chunk(i, TensorStatus.FREE)

        if self._time_profile:
            global_timer.my_timer.finish_profile("CLIENT_release_dist")

    def release(
        self,
        param: torch.nn.Parameter,
        access_type: AccessType,
        reset_to_status: TensorStatus = TensorStatus.HOLD,
    ):
        r"""Release the param in standalone environment.

        This means the data and grad of the param no longer need to
        stay in the current compute device.

        Steps:
            1. Update the status of tensor and chunk.
            2. If the chunk can be released released the payload.

        Args:
            param: :class:`torch.nn.Parameter`.
            access_type: :class:`AccessType`.
            reset_to_status: :class:`TensorStatus`. The status to reset tensor to.
        """
        if param.ps_attr.param_type == ParamType.TORCH_BASED:
            return
        if self._time_profile:
            global_timer.my_timer.start_profile("CLIENT_release")
        rank = self.local_rank
        assert isinstance(reset_to_status, TensorStatus)

        chunk_id = self.chunk_tensor_index.get_chunk_id(param, access_type)
        logger.debug(
            f"rank {rank} release a tensor of {access_type} chunk_id {chunk_id} to {reset_to_status}"
        )

        # Update the status of tensor and chunk.
        self.chunk_list.update_status(
            chunk_id, param.ps_attr.get_status(access_type), reset_to_status
        )
        param.ps_attr.set_status(reset_to_status, access_type)

        if access_type == AccessType.DATA:
            # NOTE(jiaruifang) device must be the same as the origin param.
            # Or it will affect hook of param.grad_fn.next_functions[0][0].
            param.data = torch.tensor(
                [], dtype=param.ps_attr.data_type, device=param.device
            )
        elif access_type == AccessType.GRAD:
            param.grad = None

        if self._time_profile:
            global_timer.my_timer.finish_profile("CLIENT_release")

    def release_data(
        self,
        param: torch.nn.Parameter,
        reset_to_status: TensorStatus = TensorStatus.HOLD,
    ):
        r"""release the param tensor to FREE or HOLD"""
        self.release(param, AccessType.DATA, reset_to_status)

    def release_grad(
        self,
        param: torch.nn.Parameter,
        reset_to_status: TensorStatus = TensorStatus.HOLD,
    ):
        self.release(param, AccessType.GRAD, reset_to_status)

    def reset(self):
        raise NotImplementedError

    def display_chunk_info(self):
        logger.info("Print chunk list info.")

        overall_size = 0
        for (
            type,
            type_chunk_list,
        ) in self.chunk_tensor_index.chunk_type_to_chunk_id_list_map.items():
            logger.info(f"Chunk list {type}")
            for chunk_id in type_chunk_list:
                chunk = self.chunk_list[chunk_id]
                (
                    comm_group_id,
                    comm_group_offset,
                    _,
                ) = self.chunk_tensor_index.chunk_id_to_comm_group_map[chunk_id]
                assert comm_group_id is not None

                logger.info(
                    f"Chunk id {chunk.chunk_id}, status {chunk.get_status()}, "
                    f"comm group {comm_group_id, comm_group_offset}, "
                    f"capacity {chunk.capacity / 1024 / 1024} M elems, "
                    f"dtype {chunk.data_type} device {chunk.get_device()}"
                )
                for info in self.chunk_tensor_index.generate_tensor_info_in_order(
                    chunk_id
                ):
                    assert info.chunk_id == chunk_id, f"{info.chunk_id} vs {chunk_id}"
                    logger.debug(
                        f"** tensor: chunk_id {chunk_id}, start {info.start_offset}, "
                        f"end {info.start_offset + info.numel}, size {info.numel}, "
                        f"tensor_id {info.tensor_id}, status {info.status()}, name {info.tensor_name}"
                    )
                overall_size += chunk.get_chunk_space()

        logger.info(f"OVERALL CHUNK SIZE {overall_size / 1024 / 1024 / 1024} GB")
