import functools
from functools import partial
from typing import Callable, Dict, Tuple, TypeVar

import jax
import jax.numpy as jnp
from jax.experimental.pjit import with_sharding_constraint
from jax.interpreters.pxla import PartitionSpec

import haliax as hax
import haliax.nn as hnn
from haliax import Axis, NamedArray
from haliax.jax_utils import named_call
from haliax.partitioning import ResourceAxis, ResourceMapping, auto_sharded
from levanter.jax_utils import reduce


def quick_gelu(x):
    return x * hnn.sigmoid(1.702 * x)


ACT2FN: Dict[str, Callable] = {
    "relu": hnn.relu,
    "silu": hnn.silu,
    "swish": hnn.swish,
    "gelu": partial(hnn.gelu, approximate=False),
    "gelu_new": partial(hnn.gelu, approximate=True),
    "quick_gelu": quick_gelu,
}


class RunningMean(object):
    """Numerically stable running mean for an arbitrary array"""

    def __init__(self, shape=(), dtype=jnp.float32):
        self.mean = jnp.zeros(shape, dtype)
        self.count = 0

    def update(self, x):
        self.count += 1
        self.mean += (x - self.mean) / self.count


M = TypeVar("M")
X = TypeVar("X")


@named_call
def accumulate_gradients(f: Callable[[M, X], Tuple[float, M]], model: M, *inputs: X) -> Tuple[float, M]:
    zero = (jnp.zeros(()), jax.tree_util.tree_map(lambda m: jnp.zeros_like(m), model), 0)

    def compute_and_accumulate(acc, *input):
        loss, grad = f(model, *input)
        acc_loss, acc_grad, n = acc
        return loss + acc_loss, jax.tree_map(jnp.add, acc_grad, grad), n + 1

    total_loss, total_grad, total_n = reduce(compute_and_accumulate, zero, *inputs)

    return total_loss / total_n, jax.tree_map(lambda x: x / total_n, total_grad)


# cf https://github.com/google-research/t5x/blob/main/t5x/trainer.py#L617
@named_call
def accumulate_gradients_sharded(
    f: Callable[[M, X], Tuple[float, M]],
    model: M,
    *inputs: X,
    data_axis_size: int,
    per_device_parallelism: int,
    compute_axis_mapping: ResourceMapping,
    parameter_axis_mapping: ResourceMapping,
) -> Tuple[float, M]:
    """
    Accumulate gradients across a sharded dataset, keeping a local copy of the gradient on each row of the data
     parallel axis. (If the model is not sharded, then a copy of the gradient is on each individual device.)

     Parameters:
        f: a function that takes a model and a batch of inputs and returns a tuple of (loss, gradient)
        data_axis_size: the size of the data parallel axis
        per_device_parallelism: how many examples to process at once on each device
        inputs: inputs with a leading batch axis, which will be reshaped/split
        compute_axis_mapping: a ResourceMapping for doing compute. The model should be sharded this way
        parameter_axis_mapping: a ResourceMapping for doing parameter updates. The model should be sharded this way
    """
    # data comes in as (batch, ...), and we'll reshape to (data_axis_size, num_micro_steps, per_device_parallelism, ...)
    batch_size = jnp.shape(inputs[0])[0]
    microbatch_size = data_axis_size * per_device_parallelism
    num_micro_steps = batch_size // microbatch_size
    assert num_micro_steps * microbatch_size == batch_size

    # do gradient accumulation on the data parallel axis, with model partitioned according to compute_axis_mapping
    with hax.axis_mapping(compute_axis_mapping, merge=False):
        with jax.named_scope("mass reshape"):

            def _reshape(x):
                x = x.reshape((microbatch_size, num_micro_steps) + x.shape[1:])
                return with_sharding_constraint(x, PartitionSpec(ResourceAxis.DATA, *(None,) * (len(x.shape) - 1)))

            inputs = jax.tree_util.tree_map(_reshape, inputs)

        Microbatch = Axis("microbatch", microbatch_size)

        with jax.named_scope("accumulate grad vmap"), hax.axis_mapping(
            {Microbatch.name: ResourceAxis.DATA}, merge=True
        ):
            losses, grads = hax.vmap(accumulate_gradients, axis=Microbatch)(f, model, *inputs)
            grads = auto_sharded(grads)

    # compute means and shard according to the parameter_axis_mapping
    with jax.named_scope("reduce grads"), hax.axis_mapping(parameter_axis_mapping):
        # losses and grads have Data leading axis
        grads = hax.mean(grads, axis=Microbatch)
        grads = auto_sharded(grads)
        loss = jnp.mean(losses)

    return loss, grads


# from https://github.com/google/jax/issues/4285
def recursive_checkpoint(funs, threshold=2):
    if len(funs) == 1:
        return funs[0]
    elif len(funs) == 2:
        f1, f2 = funs
        return lambda x: f2(f1(x))
    elif len(funs) <= threshold:
        return functools.reduce(lambda f, g: lambda x: g(f(x)), funs)
    else:
        f1 = recursive_checkpoint(funs[: len(funs) // 2])
        f2 = recursive_checkpoint(funs[len(funs) // 2 :])
        return lambda x: f2(jax.remat(f1)(x))


def cross_entropy_loss_and_log_normalizers(
    pred_y: NamedArray,
    Label: Axis,
    target_y: NamedArray,
) -> Tuple[NamedArray, NamedArray]:
    """
    Compute the cross entropy loss and log normalizers for a batch of predictions and targets.

    :param pred_y: a NamedArray with the Label axis (and possibly others for e.g. batch and seq) containing the logits
    :param Label: the Label axis
    :param target_y: a NamedArray with the Label axis (and possibly others) containing the targets

    :return: tuple of two named arrays, with "per position" losses and log normalizers
    """
    log_normalizers = hax.nn.logsumexp(pred_y, Label)
    neg_log_normalized = log_normalizers - pred_y

    loss = hax.dot(Label, target_y, neg_log_normalized)

    return loss, log_normalizers


def cross_entropy_loss(
    pred_y: NamedArray,
    Label: Axis,
    target_y: NamedArray,
) -> NamedArray:
    loss, _ = cross_entropy_loss_and_log_normalizers(pred_y, Label, target_y)
    return loss
