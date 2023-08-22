from typing import Tuple, Dict, Any, Optional
import os
from functools import partial
import argparse
import pickle
from tqdm import tqdm
import jax
import jax.numpy as jnp
from jax.experimental.ode import odeint
import matplotlib.pyplot as plt
from tensorboardX import SummaryWriter
from flax import linen as nn
import optax
import numpy as np
import scipy.integrate
from deer.seq1d import solve_ivp, seq1d
from PIL import Image
from dataloaders.lra_image import CIFAR10DataModule
import torch
import pdb

# # run on cpu
# jax.config.update('jax_platform_name', 'cpu')
# enable float 64
jax.config.update('jax_enable_x64', True)


class MLP(nn.Module):
    ndim: int
    dtype: Any

    @nn.compact
    def __call__(self, x):
        x = nn.Dense(self.ndim, dtype=self.dtype)(x)
        return x


@partial(jax.jit, static_argnames=("model", "mlp", "method"))
def rollout(
    model: nn.Module,
    params: Any,
    mlp: nn.Module,
    mlp_params: Any,
    y0: jnp.ndarray,
    inputs: jnp.ndarray,
    yinit_guess: Optional[jnp.ndarray] = None,
    method: str = "deer_rnn",
    nclasses: int = 10
) -> jnp.ndarray:
    # roll out the model's predictions with y being the state
    # y0: (nstates,)
    # inputs: (ntpts,)
    # yinit_guess: (ntpts, nstates)
    # returns: (ntpts, nstates)

    def model_func(carry, inputs, params):
        return model.apply(params, carry, inputs)[0]
    if method == "deer_rnn":
        y = seq1d(model_func, y0, inputs, params, yinit_guess)  # (nbatch, nseq, nstates)
        return mlp.apply(mlp_params, y[-1, :]), y
    else:
        raise NotImplementedError()


def compute_metrics(
    logits: jnp.ndarray,
    labels: jnp.ndarray
) -> Dict[str, jnp.ndarray]:
    loss = jnp.mean(optax.softmax_cross_entropy_with_integer_labels(logits, labels))
    accuracy = jnp.mean(jnp.argmax(logits, -1) == labels)
    metrics = {
        'loss': loss,
        'accuracy': accuracy
    }
    return metrics


@partial(jax.jit, static_argnames=("model", "mlp", "method"))
def loss_fn(
    model: nn.Module,
    params: Any,
    mlp: nn.Module,
    mlp_params: Any,
    y0: jnp.ndarray,
    batch: Tuple[jnp.ndarray, jnp.ndarray],
    all_yinit_guess: Optional[jnp.ndarray] = None,
    method: str = "deer_rnn"
) -> jnp.ndarray:
    """
    batch: (x, y). x.shape == (nbatch, nseq, ndim), y.shape == (nbatch,)
    """
    # compute the loss
    # batch: (batch_size, ntpts, ninp), (batch_size, ntpts, nstates)
    # yinit_guess (ndata, ntpts, nstates)
    # weight: (ntpts,)
    x, y = batch

    # TODO
    # y0 and yinit_guess
    yinit_guess = all_yinit_guess
    # y0 = all_yinit_guess[:, 0, :]

    # ypred: (batch_size, nclass)
    ypred, yinit_guess = jax.vmap(
        rollout, in_axes=(None, None, None, None, 0, 0, 0)
    )(model, params, mlp, mlp_params, y0, x, yinit_guess)

    metrics = compute_metrics(ypred, y)
    loss, accuracy = metrics["loss"], metrics["accuracy"]
    return loss, (accuracy, yinit_guess)


@partial(jax.jit, static_argnames=("model", "mlp", "optimizer", "method"))
def update_step(
    model: nn.Module,
    mlp: nn.Module,
    optimizer: optax.GradientTransformation,
    combined_params: optax.Params,
    opt_state: Any,
    batch: Tuple[jnp.ndarray, jnp.ndarray],
    y0: jnp.ndarray,
    all_yinit_guess: jnp.ndarray,
    method: str = "deer_rnn"
) -> Tuple[optax.Params, Any, jnp.ndarray, jnp.ndarray]:
    (loss, (accuracy, yinit_guess)), grad = jax.value_and_grad(
        loss_fn,
        argnums=(1, 3),
        has_aux=True
    )(model, combined_params["params"], mlp, combined_params["mlp_params"], y0, batch, all_yinit_guess, method)
    updates, opt_state = optimizer.update({"params": grad[0], "mlp_params": grad[1]}, opt_state)
    combined_params = optax.apply_updates(combined_params, updates)
    return combined_params, opt_state, loss, accuracy, yinit_guess


def prep_batch(
    batch: Tuple[torch.Tensor, torch.Tensor]
) -> Tuple[jnp.ndarray, jnp.ndarray]:
    assert len(batch) == 2
    x, y = batch
    x = jnp.asarray(x.numpy())
    y = jnp.asarray(y.numpy())
    return x, y


def main():
    # set up argparse for the hyperparameters above
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--nepochs", type=int, default=999999999)
    parser.add_argument("--batch_size", type=int, default=16)
    parser.add_argument("--version", type=int, default=0)
    parser.add_argument("--method", type=str, default="deer")
    parser.add_argument("--nstates", type=int, default=256)
    parser.add_argument("--nsequence", type=int, default=1024)
    parser.add_argument("--nclass", type=int, default=10)
    args = parser.parse_args()

    method = args.method
    nstates = args.nstates
    nsequence = args.nsequence
    nclass = args.nclass
    dtype = jnp.float32

    # check the path
    logpath = "logs"
    path = os.path.join(logpath, f"version_{args.version}")
    # if os.path.exists(path):
    #     raise ValueError(f"Path {path} already exists!")
    os.makedirs(path, exist_ok=True)

    # set up the model and optimizer
    key = jax.random.PRNGKey(args.seed)
    subkey1, subkey2, subkey3, subkey4, key = jax.random.split(key, 5)
    model = nn.GRUCell(features=nstates, dtype=dtype, param_dtype=dtype)
    carry = model.initialize_carry(
        subkey1,
        (args.batch_size, nstates)
    )  # (batch_size, nstates)
    inputs = jax.random.normal(
        subkey2,
        (args.batch_size, nsequence, 1),
        dtype=dtype
    )  # (batch_size, nsequence, nstates)
    params = model.init(key, carry, inputs[:, 0, :])

    mlp = MLP(nclass, dtype)
    dummy_x = jax.random.normal(
        subkey3,
        (args.batch_size, nstates),
        dtype=dtype
    )  # (batch_size, nstates)
    mlp_params = mlp.init(subkey4, dummy_x)

    optimizer = optax.adam(learning_rate=args.lr)
    combined_params = {"params": params, "mlp_params": mlp_params}
    opt_state = optimizer.init(combined_params)

    yinit_guess = jnp.zeros((args.batch_size, nsequence, nstates), dtype=dtype)
    y0 = jnp.zeros((args.batch_size, nstates), dtype=dtype)

    # get the summary writer
    summary_writer = SummaryWriter(log_dir=path)

    # training loop
    step = 0
    dm = CIFAR10DataModule(
        data_dir="/home/yhl48/seq2seq/data",
        batch_size=args.batch_size
    )
    dm.setup()
    for epoch in range(args.nepochs):
        for i, batch in enumerate(dm.train_dataloader()):
            batch = dm.on_before_batch_transfer(batch, i)
            batch = prep_batch(batch)
            # x, y = prep_batch(batch)
            # print(carry.shape, x.shape, yinit_guess.shape)
            # ypred = jax.vmap(
            #     rollout, in_axes=(None, None, None, None, 0, 0, 0)
            # )(model, params, mlp, mlp_params, carry, x, yinit_guess)
            # pdb.set_trace()
            # TODO update steps here
            combined_params, opt_state, loss, accuracy, yinit_guess = update_step(
                model, mlp, optimizer, combined_params,
                opt_state, batch,
                y0, yinit_guess
            )
            summary_writer.add_scalar("train_loss", loss, step)
            step += 1

if __name__ == "__main__":
    main()
