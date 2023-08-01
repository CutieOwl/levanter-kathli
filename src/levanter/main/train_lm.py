import logging
import os
from dataclasses import dataclass, field
from typing import Optional, Union

import equinox as eqx
import jax.random as jrandom
import jmp
import wandb

import haliax as hax
import haliax.random
from haliax import Axis
from haliax.jax_utils import filter_eval_shape
from haliax.nn import cross_entropy_loss
from haliax.partitioning import named_jit, round_axis_for_partitioning

import levanter
from levanter import callbacks
from levanter.compat.hf_checkpoints import HFCompatConfig
from levanter.data import ReplicatedBatchLoader, ShardedBatchLoader
from levanter.data.text import CausalLmDataset, LMDatasetConfig, LmExample
from levanter.grad_accum import accumulate_gradients_sharded
from levanter.logging import capture_time, log_time_to_wandb
from levanter.models.gpt2 import Gpt2Config
from levanter.models.lm_model import LmConfig, LmHeadModel
from levanter.trainer import OptimizerConfig, StepInfo, TrainerConfig, TrainerHooks
from levanter.utils.jax_utils import parameter_count
from levanter.utils.py_utils import non_caching_cycle


logger = logging.getLogger(__name__)


@dataclass
class TrainLmConfig:
    data: LMDatasetConfig = field(default_factory=LMDatasetConfig)
    trainer: TrainerConfig = field(default_factory=TrainerConfig)
    model: LmConfig = field(default_factory=Gpt2Config)
    optimizer: OptimizerConfig = field(default_factory=OptimizerConfig)

    # config related to continued pretraining
    initialize_from_hf: Union[bool, str] = False
    """if provided, this will override the model config in the config. if true, use the default hf checkpoint for this model class"""
    use_hf_model_config: bool = False  # if true, replace the model config with the hf config from the checkpoint

    # TODO: atm we don't support loading from a checkpoint that has a different tokenizer. this is a bit annoying
    # TODO: atm you have to at least specify a levanter model config with the same type as the hf checkpoint

    fcm_prob: float = 0.0  # forgetful context masking prob. recommended 0.15

    hf_save_path: Optional[str] = None
    hf_upload: Optional[str] = None
    hf_save_steps: int = 10000


def main(config: TrainLmConfig):
    tokenizer = config.data.the_tokenizer

    # this is some unpleasant code to allow us to initialize from a hf checkpoint. If this is your first read through,
    # I recommend skipping it for now
    if config.initialize_from_hf:
        assert isinstance(config.model, HFCompatConfig)
        converter = config.model.default_hf_checkpoint_converter
        if tokenizer.vocab != converter.tokenizer.vocab:
            logger.warning("The tokenizers appear to be different. You may want to check this.")

        if isinstance(config.initialize_from_hf, str):
            converter = converter.replaced(reference_checkpoint=config.initialize_from_hf, tokenizer=tokenizer)
        else:
            converter = converter.replaced(tokenizer=tokenizer)

        if config.use_hf_model_config:
            # TODO: log diff of old and new config
            # NB: gross mutability
            config.model = converter.config_from_hf_config(converter.default_hf_config)
    elif isinstance(config.model, HFCompatConfig):
        converter = config.model.default_hf_checkpoint_converter
        converter = converter.replaced(tokenizer=tokenizer)
    else:
        converter = None

    # initialize training config *after* we've done the hf stuff b/c we might have changed the model config
    config.trainer.initialize(config)

    # randomness in jax is tightly controlled by "keys" which are the states of the random number generators
    # this makes deterministic training pretty easy
    seed = config.trainer.seed
    data_key, loader_key, model_key, training_key = jrandom.split(jrandom.PRNGKey(seed), 4)

    # some axes we need
    Batch = Axis("batch", config.trainer.train_batch_size)
    EvalBatch = Axis("batch", config.trainer.eval_batch_size)
    Pos = config.model.Pos
    KeyPos = config.model.KeyPos

    # We have two axis_mappings: one for storing the model and optimizer states, and one for compute
    # This allows Zero-3-style parameter sharding, where we shard the parameters and optimizer state across the mesh
    compute_axis_mapping = config.trainer.compute_axis_mapping
    parameter_axis_mapping = config.trainer.parameter_axis_mapping

    eval_loader = ReplicatedBatchLoader(
        CausalLmDataset(config.data.token_seq_dataset("validation", Pos.size), Pos, KeyPos),
        config.trainer.device_mesh,
        EvalBatch,
        compute_axis_mapping,
    )

    train_loader = ShardedBatchLoader(
        CausalLmDataset(config.data.token_seq_dataset("train", Pos.size), Pos, KeyPos),
        # TokenSeqDataset(config.data.build_or_load_cache("train"), Pos),
        config.trainer.device_mesh,
        Batch,
        compute_axis_mapping,
    )

    with config.trainer.device_mesh as mesh:
        # to do partitioning, our dimensions have to be divisible by the size of the physical axes they're mapped to
        # For most things, we just insist you specify the config right, but tokenizers often have strange numbers of
        # tokens: gpt-2 has 50257, for example. So we round up.
        vocab_size = len(tokenizer)
        Vocab = round_axis_for_partitioning(Axis("vocab", vocab_size), parameter_axis_mapping)
        if vocab_size != Vocab.size:
            logger.info(f"Rounding vocab size from {vocab_size} to {Vocab.size} for partitioning")

        # Mixed Precision: We use the "jmp" library to handle mixed precision training. It basically has three dtypes:
        # 1) compute (typically bfloat16)
        # 2) parameter (typically float32)
        # 3) output (sometimes float32)
        # I like to think of these as "semantic" dtypes: compute is the dtype we do most of our math in, parameter is
        # the dtype we store our parameters in, and output is the dtype we use for loss calculations.
        mp: jmp.Policy = config.trainer.mp

        # We use Optax for our optimizer. It's a pretty standard library for optimizers in JAX.
        optimizer = config.optimizer.build(config.trainer.num_train_steps)

        def compute_loss(model: LmHeadModel, example: LmExample, key, inference):
            print("compute_loss example", example)
            print("compute_loss key", key)
            with hax.axis_mapping(compute_axis_mapping):
                model = mp.cast_to_compute(model)

                pred_y = model(example.tokens, example.attn_mask, key=key, inference=inference)
                pred_y = mp.cast_to_output(pred_y)

                target_y = hax.nn.one_hot(example.targets, Vocab, dtype=pred_y.dtype)

                return cross_entropy_loss(pred_y, Vocab, target_y, where=example.loss_mask, reduction_axis=Pos)

        @named_jit(axis_resources=parameter_axis_mapping)
        def train_loss(model, example, key):
            print("tbl example", example)
            print("tbl key", key)
            return hax.mean(compute_loss(model, example, key, False)).scalar()

        @named_jit(axis_resources=parameter_axis_mapping, donate_args=True)
        def train_step(model, opt_state, examples: LmExample, key):
            grad_loss = eqx.filter_value_and_grad(train_loss)

            print("train_step example", examples)
            print("train_step key", key)
            loss, grads = accumulate_gradients_sharded(
                grad_loss,
                Batch,
                model,
                examples,
                key=key,
                per_device_parallelism=config.trainer.per_device_parallelism,
                parameter_axis_mapping=parameter_axis_mapping,
            )

            # distribute gradients across the mesh and apply them
            updates, opt_state = optimizer.update(grads, opt_state, params=model)
            model = eqx.apply_updates(model, updates)

            return loss, model, opt_state

        @named_jit(axis_resources=parameter_axis_mapping)
        def eval_loss(model, example):
            return hax.mean(compute_loss(model, example, None, True)).scalar()

        # initialize the model
        # There are a few ways we might initialize the model
        # * from a checkpoint during training
        # * from scratch
        # * from an hf pretrained model
        def init_model_and_opt_state(model_key):
            # This function
            # 1) initializes model weights and opt_state
            # 2) ensures all model weights are the right dtype
            model = config.model.build(Vocab, key=model_key)
            model = mp.cast_to_param(model)
            opt_state = optimizer.init(model)
            return model, opt_state

        # first get the shape of the model and optimizer state
        model, opt_state = filter_eval_shape(init_model_and_opt_state, model_key)
        wandb.summary["parameter_count"] = parameter_count(model)

        # second, try to load the model and opt state from a checkpoint. This may throw if we required a
        # checkpoint but it wasn't found.
        model, (opt_state, training_key), resume_step = config.trainer.maybe_load_checkpoint(
            model,
            (opt_state, training_key),
            axis_mapping=parameter_axis_mapping,
            mesh=mesh,
        )

        if resume_step is None:
            # no checkpoint was found, so we need to initialize the model and opt state
            if config.initialize_from_hf:
                # initialize from an hf pretrained model
                logger.info(
                    "No training checkpoint found. Initializing model from HF checkpoint"
                    f" '{converter.reference_checkpoint}'"
                )
                model = converter.load_pretrained(config.model, axis_mapping=parameter_axis_mapping)
                opt_state = named_jit(optimizer.init, axis_resources=parameter_axis_mapping)(model)
            else:
                logger.info("No checkpoint found. Starting from scratch.")
                model, opt_state = named_jit(init_model_and_opt_state, axis_resources=parameter_axis_mapping)(
                    model_key
                )

        # boilerplate hooks and such
        engine = TrainerHooks()
        engine.add_hook(callbacks.pbar_logger(total=config.trainer.num_train_steps), every=1)
        engine.add_hook(callbacks.log_to_wandb, every=1)
        engine.add_hook(callbacks.log_performance_stats(Pos.size, config.trainer.train_batch_size), every=1)
        engine.add_hook(
            callbacks.compute_validation_loss(eval_loss, eval_loader, max_batches=config.trainer.max_eval_batches),
            every=config.trainer.steps_per_eval,
        )
        engine.add_hook(callbacks.wandb_xla_logger(config.trainer.wandb), every=config.trainer.steps_per_eval)
        # engine.add_hook(callbacks.log_memory_usage(), every=1)
        checkpointer = config.trainer.checkpointer.create(config.trainer.run_id)
        engine.add_hook(checkpointer.on_step, every=1)  # checkpointer manages its own frequency
        if config.hf_save_path is not None:
            full_save_path = os.path.join(config.hf_save_path, config.trainer.run_id)
            from levanter.compat.hf_checkpoints import save_hf_checkpoint_callback

            engine.add_hook(
                save_hf_checkpoint_callback(full_save_path, converter),
                every=config.hf_save_steps,
            )

        # visualize log probs
        @named_jit(axis_resources=parameter_axis_mapping)
        def compute_log_probs(model, example: LmExample):
            """This method differs from eval_loss in that it skips the mean call, so we get a loss for each token"""
            with hax.axis_mapping(compute_axis_mapping):
                model = mp.cast_to_compute(model)

                pred_y = model(example.tokens, example.attn_mask, inference=True, key=None)
                pred_y = mp.cast_to_output(pred_y)
                targets = hax.nn.one_hot(example.tokens, Vocab, dtype=pred_y.dtype)
                loss = cross_entropy_loss(pred_y, Vocab, targets, where=example.loss_mask, reduction=None)
                logprobs = -loss
                # roll forward to get the loss for each predicted token
                logprobs = haliax.roll(logprobs, 1, Pos)
                return logprobs.rearrange((EvalBatch, Pos)).array

        engine.add_hook(
            callbacks.compute_and_visualize_log_probs(
                eval_loader, tokenizer, compute_log_probs, os.path.join(config.trainer.run_dir, "log_probs")
            ),
            every=config.trainer.steps_per_eval,
        )

        # data loader. may need to seek to the right place if we're resuming
        iter_data = non_caching_cycle(train_loader)

        if resume_step is not None:
            # step is after the batch, so we need to seek to step
            # TODO: implement iter_data.seek(resume_step +1)
            import tqdm

            for _ in tqdm.tqdm(range(resume_step + 1), desc="seeking data for resume"):
                next(iter_data)
            initial_step = resume_step + 1
        else:
            initial_step = 0

        # assign these here in case num_train_steps == 0
        step_loss = 0.0
        step_time = lambda: 0.0  # noqa: E731

        # finally, run the training loop
        for step in range(initial_step, config.trainer.num_train_steps):
            with capture_time() as step_time:
                with log_time_to_wandb("throughput/loading_time", step=step):
                    example = next(iter_data)
                    my_key, training_key = jrandom.split(training_key, 2)

                print("training_key", my_key)
                print("example", example)
                jax_step_loss, model, opt_state = train_step(model, opt_state, example, my_key)
                step_loss = jax_step_loss.item()

            with log_time_to_wandb("throughput/hook_time", step=step):
                engine.run_hooks(StepInfo(step, model, opt_state, step_loss, training_key, step_duration=step_time()))

        last_step = StepInfo(
            config.trainer.num_train_steps,
            model,
            opt_state,
            step_loss,
            training_key,
            step_duration=step_time(),
        )

        engine.run_hooks(last_step, force=True)
        checkpointer.on_step(last_step, force=True)


if __name__ == "__main__":
    levanter.config.main(main)()
