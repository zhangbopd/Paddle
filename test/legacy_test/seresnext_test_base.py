# Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
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

import numpy as np
import seresnext_net
from parallel_executor_test_base import DeviceType, TestParallelExecutorBase

from paddle.base import core


class TestResnetBase(TestParallelExecutorBase):
    def _compare_result_with_origin_model(
        self, check_func, use_device, delta2=1e-5, compare_separately=True
    ):
        if use_device == DeviceType.CUDA and not core.is_compiled_with_cuda():
            return

        (
            func_1_first_loss,
            func_1_last_loss,
            func_1_loss_area,
        ) = self.check_network_convergence(
            seresnext_net.model,
            feed_dict=seresnext_net.feed_dict(use_device),
            iter=seresnext_net.iter(use_device),
            batch_size=seresnext_net.batch_size(use_device),
            use_device=use_device,
            use_reduce=False,
            optimizer=seresnext_net.optimizer,
        )

        func_2_first_loss, func_2_last_loss, func_2_loss_area = check_func(
            seresnext_net.model,
            feed_dict=seresnext_net.feed_dict(use_device),
            iter=seresnext_net.iter(use_device),
            batch_size=seresnext_net.batch_size(use_device),
            use_device=use_device,
        )

        if compare_separately:
            self.assertAlmostEqual(
                func_1_first_loss, func_2_first_loss, delta=1e-5
            )
            self.assertAlmostEqual(
                func_1_last_loss, func_2_last_loss, delta=delta2
            )
        else:
            np.testing.assert_allclose(
                func_1_loss_area, func_2_loss_area, rtol=delta2
            )
            self.assertAlmostEqual(
                func_1_first_loss, func_2_first_loss, delta=1e-5
            )
            self.assertAlmostEqual(
                func_1_last_loss, func_2_last_loss, delta=delta2
            )
