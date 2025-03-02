#   Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
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

import unittest

import numpy as np

import paddle
from paddle import base
from paddle.base import Program, core, program_guard


def _reference_instance_norm_naive(x, scale, bias, epsilon, mean, var):
    x_shape = x.shape
    if len(x_shape) == 2:
        x = np.reshape(x, (x.shape[0], x.shape[1], 1, 1))
    n, c, h, w = x.shape

    mean_tile = np.reshape(mean, (n, c, 1, 1))
    mean_tile = np.tile(mean_tile, (1, 1, h, w))
    var_tile = np.reshape(var, (n, c, 1, 1))
    var_tile = np.tile(var_tile, (1, 1, h, w))

    x_norm = (x - mean_tile) / np.sqrt(var_tile + epsilon)
    scale_tile = np.reshape(scale, (1, c, 1, 1))
    scale_tile = np.tile(scale_tile, (n, 1, h, w))
    bias_tile = np.reshape(bias, (1, c, 1, 1))
    bias_tile = np.tile(bias_tile, (n, 1, h, w))
    y = scale_tile * x_norm + bias_tile
    if len(x_shape) == 2:
        y = np.reshape(y, x_shape)
    return y, mean, var


def _reference_instance_norm_grad(x, d_y, scale, mean, var, epsilon):
    # d_scale = sum(d_y * (x-mean) / sqrt(var+epsilon))
    # d_offset = sum(d_y)
    # d_x = scale / sqrt(var+epsilon) * (d_y - np.mean(d_y, axis=(2,3)) - (x-mean)/sqrt(var+epsilon)* np.mean(y_grad * (x-mean)/sqrt(var+epsilon), axis=(2,3)))
    n, c, h, w = x.shape

    d_bias = np.sum(d_y, axis=(0, 2, 3))

    mean_tile = np.reshape(mean, (n, c, 1, 1))
    mean_tile = np.tile(mean_tile, (1, 1, h, w))
    var_tile = np.reshape(var, (n, c, 1, 1))
    var_tile = np.tile(var_tile, (1, 1, h, w))

    d_scale = np.sum(d_y * (x - mean_tile) * var_tile, axis=(0, 2, 3))
    var_inv = var_tile
    scale_tile = np.reshape(scale, (1, c, 1, 1))
    scale_tile = np.tile(scale_tile, (n, 1, h, w))

    d_x = (
        scale_tile
        * var_inv
        * (
            d_y
            - np.mean(d_y, axis=(2, 3), keepdims=True)
            - (x - mean_tile)
            * var_inv
            * np.mean(
                d_y * (x - mean_tile) * var_inv, axis=(2, 3), keepdims=True
            )
        )
    )
    return d_x, d_scale, d_bias


def _cal_mean_variance(x, epsilon, mean_shape):
    mean = np.reshape(np.mean(x, axis=(2, 3)), mean_shape)
    var = np.reshape(np.var(x, axis=(2, 3)), mean_shape)
    return mean, var


def instance_norm_wrapper(x, weight=None, bias=None, esp=1e-05):
    return paddle.nn.functional.instance_norm(
        x, None, None, weight, bias, True, 0.9, esp
    )


class TestInstanceNormOpTraining(unittest.TestCase):
    def setUp(self):
        self.epsilon = 1e-5
        self.init_test_case()

    def init_test_case(self):
        self.shape = [2, 3, 4, 5]
        self.no_grad_set = set()
        self.fetch_list = [
            'y',
            'saved_mean',
            'saved_variance',
            'x@GRAD',
            'scale@GRAD',
            'bias@GRAD',
        ]

    def __assert_close(self, tensor, np_array, msg, atol=1e-4):
        np.testing.assert_allclose(
            np.array(tensor), np_array, rtol=1e-05, atol=atol, err_msg=msg
        )

    def set_global_mean_var(self, mean_shape, x):
        mean, variance = _cal_mean_variance(x, self.epsilon, mean_shape)
        return mean, variance

    def test_forward_backward(self):
        def test_with_place(place, shape):
            paddle.enable_static()
            epsilon = self.epsilon
            n, c, h, w = shape[0], shape[1], shape[2], shape[3]
            scale_shape = [c]
            mean_shape = [n * c]

            np.random.seed()
            x = np.random.random_sample(shape).astype(np.float32)
            scale = np.random.random_sample(scale_shape).astype(np.float32)
            bias = np.random.random_sample(scale_shape).astype(np.float32)
            mean, variance = self.set_global_mean_var(mean_shape, x)
            d_y = np.random.random_sample(shape).astype(np.float32)

            y, saved_mean, variance_tmp = _reference_instance_norm_naive(
                x, scale, bias, epsilon, mean, variance
            )

            saved_variance = 1 / np.sqrt(variance_tmp + epsilon)

            d_x, d_scale, d_bias = _reference_instance_norm_grad(
                x, d_y, scale, saved_mean, saved_variance, epsilon
            )

            var_dict = locals()
            var_dict['y@GRAD'] = d_y
            var_dict['x@GRAD'] = d_x
            var_dict['scale@GRAD'] = d_scale
            var_dict['bias@GRAD'] = d_bias

            var_names = [
                'x',
                'scale',
                'bias',
                'y',
                'saved_mean',
                'saved_variance',
            ]
            ground_truth = {name: var_dict[name] for name in var_names}

            program = base.Program()
            with base.program_guard(program):
                block = program.global_block()
                for name in ground_truth:
                    block.create_var(
                        name=name,
                        dtype='float32',
                        shape=ground_truth[name].shape,
                    )
                in_op = block.append_op(
                    type="instance_norm",
                    inputs={
                        "X": block.var("x"),
                        "Scale": block.var("scale"),
                        "Bias": block.var("bias"),
                    },
                    outputs={
                        "Y": block.var("y"),
                        "SavedMean": block.var("saved_mean"),
                        "SavedVariance": block.var("saved_variance"),
                    },
                    attrs={
                        "epsilon": epsilon,
                    },
                )

                block.create_var(name="y@GRAD", dtype='float32', shape=y.shape)

                grad_op_desc_list, op_grad_to_var = core.get_grad_op_desc(
                    in_op.desc, self.no_grad_set, []
                )
                grad_op_desc = grad_op_desc_list[0]
                new_op_desc = block.desc.append_op()
                new_op_desc.copy_from(grad_op_desc)
                for var_name in grad_op_desc.output_arg_names():
                    block.desc.var(var_name.encode("ascii"))
                grad_op_desc.infer_var_type(block.desc)
                grad_op_desc.infer_shape(block.desc)
                for arg in grad_op_desc.output_arg_names():
                    grad_var = block.desc.find_var(arg.encode("ascii"))
                    grad_var.set_dtype(core.VarDesc.VarType.FP32)

                program._sync_with_cpp()

                exe = base.Executor(place)
                out = exe.run(
                    program,
                    feed={
                        name: var_dict[name]
                        for name in ['x', 'scale', 'bias', 'y@GRAD']
                    },
                    fetch_list=self.fetch_list,
                )

            for id, name in enumerate(self.fetch_list):
                self.__assert_close(var_dict[name], out[id], name)
            print("op test forward passes: ", str(place))
            paddle.disable_static()

        places = [core.CPUPlace()]

        if core.is_compiled_with_cuda() and core.op_support_gpu(
            "instance_norm"
        ):
            places.append(core.CUDAPlace(0))
        for place in places:
            test_with_place(place, self.shape)


class TestInstanceNormOpTrainingCase1(TestInstanceNormOpTraining):
    def init_test_case(self):
        self.shape = [2, 3, 4, 5]
        self.no_grad_set = {'scale@GRAD', 'bias@GRAD'}
        self.fetch_list = ['y', 'saved_mean', 'saved_variance', 'x@GRAD']


class TestInstanceNormOpTrainingCase2(TestInstanceNormOpTraining):
    def init_test_case(self):
        self.shape = [20, 50, 4, 5]
        self.no_grad_set = {'scale@GRAD', 'bias@GRAD'}
        self.fetch_list = ['y', 'saved_mean', 'saved_variance', 'x@GRAD']


class TestInstanceNormOpError(unittest.TestCase):
    def test_errors(self):
        paddle.enable_static()
        with program_guard(Program(), Program()):
            # the input of instance_norm must be Variable.
            x1 = base.create_lod_tensor(
                np.array([-1, 3, 5, 5]), [[1, 1, 1, 1]], base.CPUPlace()
            )
            self.assertRaises(TypeError, paddle.static.nn.instance_norm, x1)

            # the input dtype of instance_norm must be float32 or float64
            x2 = paddle.static.data(
                name='x2', shape=[-1, 3, 4, 5, 6], dtype="int32"
            )
            self.assertRaises(TypeError, paddle.static.nn.instance_norm, x2)
        paddle.disable_static()


class TestInstanceNormOpErrorCase1(unittest.TestCase):
    def test_errors(self):
        paddle.enable_static()
        with program_guard(Program(), Program()):
            # the first dimension of input for instance_norm must between [2d, 5d]
            x = paddle.static.data(name='x', shape=[3], dtype="float32")
            self.assertRaises(ValueError, paddle.static.nn.instance_norm, x)
        paddle.disable_static()


if __name__ == '__main__':
    unittest.main()
