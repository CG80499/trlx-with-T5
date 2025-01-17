from typing import Callable

import torch
from trlx.data.accelerate_base_datatypes import PromptBatch
from trlx.data.ppo_types import PPORLElement
from trlx.model import BaseRLModel
from trlx.orchestrator import Orchestrator, register_orchestrator
from trlx.pipeline import BasePipeline
from trlx.utils import Clock
from trlx.utils.modeling import logprobs_from_logits, RunningMoments
from trlx.model.accelerate_ppo_model import T5AcceleratePPOModel

from time import time
import ray

import transformers

def _add_start_token_to_decoder_ids(decoder_input_ids, decoder_attention_mask):
    """Add padding to decoder_input_ids"""
    batch_size, seq_len = decoder_input_ids.shape
    padding = torch.zeros(batch_size, 1, dtype=decoder_input_ids.dtype).to(
        decoder_input_ids.device
    )
    decoder_attention_mask = torch.cat(
        [1 - padding, decoder_attention_mask], dim=1 # Start token is not masked
    )

    return torch.cat([padding, decoder_input_ids], dim=1), decoder_attention_mask

GPU_REFERENCE_MODEL = 2

@register_orchestrator
class PPOOrchestrator(Orchestrator):
    """
    Orchestrator that prepares data for PPO training: transforms samples from `pipeline` into `PPOBatch` and pushes them into model's `store`
    """

    def __init__(
        self,
        model: BaseRLModel,
        pipeline: BasePipeline,
        reward_fn: Callable,
        metric_fn: Callable = None,
        chunk_size: int = 512,
    ):
        self.pipeline = pipeline
        self.rl_model = model
        self.chunk_size = chunk_size

        self.pipeline_loader = self.pipeline.create_loader(
            self.chunk_size, shuffle=True
        )
        self.pipeline_loader = self.rl_model.accelerator.prepare(self.pipeline_loader)
        self.pipeline_iterator = iter(self.pipeline_loader)

        if not hasattr(self.rl_model.model, "frozen_head"):
            self.ref_model = self.rl_model.get_arch(self.rl_model.config)

        self.rl_model.orch = self
        self.rl_model.reward_fn = reward_fn
        self.rl_model.metric_fn = metric_fn

        self.running = RunningMoments()
        self.ref_mean = self.rl_model.config.method.ref_mean
        self.ref_std = self.rl_model.config.method.ref_std

    def score(self, samples):
        """
        Batched scoring function taking text and generating scalar
        """
        return self.rl_model.reward_fn(samples)

    def make_experience(self, num_rollouts: int = 1024, iter_count: int = 0):
        """
        Takes `num_rollouts` prompts from `pipeline`, samples model, computes KL againts a reference model appends PPOElements to model's `store`
        """
        ppo_rl_elements = []
        stats = {}
        clock = Clock()
        while len(ppo_rl_elements) < num_rollouts:
            # Get next batch in prompt dataset and refresh if exhausted
            try:
                batch: PromptBatch = next(self.pipeline_iterator)
            except StopIteration:
                self.pipeline_iterator = iter(self.pipeline_loader)
                batch = next(self.pipeline_iterator)

            exp_generate_time = time()
            samples = self.rl_model.generate(**batch)
            stats["time/exp_generate"] = time() - exp_generate_time

            query_tensors = batch.input_ids
            response_tensors = samples[:, query_tensors.shape[1] :]
            texts = self.rl_model.tokenizer.batch_decode(
                samples, skip_special_tokens=True
            )
            exp_score_time = time()
            scores = torch.tensor(
                self.score(texts), device=samples.device, dtype=torch.float
            )
            stats["time/exp_score"] = time() - exp_score_time

            # store statistics of the initial rollout as reference
            if self.ref_mean is None:
                self.ref_mean, self.ref_std = scores.mean(), scores.std()
            all_scores_mean, all_scores_std = self.running.update(scores)
            stats["exp_scores/mean"] = all_scores_mean
            stats["exp_scores/std"] = all_scores_std
            stats["exp_scores/running_mean"] = self.running.mean
            stats["exp_scores/running_std"] = self.running.std

            if self.rl_model.config.method.scale_reward == "running":
                scores /= self.running.std
            elif self.rl_model.config.method.scale_reward == "ref":
                scores /= self.ref_std

            clip_reward = self.rl_model.config.method.cliprange_reward
            if clip_reward:
                scores = torch.clip(scores, -clip_reward, clip_reward)

            # Precompute logprobs, values
            all_tokens, attention_mask, position_ids = self.rl_model.get_model_inputs(
                query_tensors.to(response_tensors.device), response_tensors
            )
            with torch.no_grad():
                logits, *_, values = self.rl_model.model(
                    all_tokens, attention_mask=attention_mask, position_ids=position_ids
                )
                # TODO(dahoas): When hydra model works need to also support generation on hydra head
                if hasattr(self.rl_model.model, "frozen_head"):
                    ref_logits = self.rl_model.model.forward_hydra(
                        all_tokens,
                        attention_mask=attention_mask,
                        position_ids=position_ids,
                        return_dict=False,
                    )
                else:
                    ref_logits, _, *_ = self.ref_model(
                        all_tokens.to(self.reference_device),
                        attention_mask=attention_mask.to(self.reference_device),
                        position_ids=position_ids.to(self.reference_device),
                    )
                    ref_logits = ref_logits.to(self.rl_model.accelerator.device)

            logprobs = logprobs_from_logits(logits[:, :-1, :], all_tokens[:, 1:])
            ref_logprobs = logprobs_from_logits(
                ref_logits[:, :-1, :], all_tokens[:, 1:]
            )

            n = samples.shape[0]
            values = values.cpu()[:, :-1]
            logprobs = logprobs.cpu()
            ref_logprobs = ref_logprobs.cpu()
            query_tensors = query_tensors.cpu()
            response_tensors = response_tensors.cpu()

            start = query_tensors.shape[1] - 1
            ends = start + attention_mask[:, start:].sum(1)
            all_values = [values[ix, start : ends[ix]] for ix in range(n)]
            all_logprobs = [logprobs[ix, start : ends[ix]] for ix in range(n)]

            # Compute rewards
            rewards = -self.rl_model.kl_ctl.value * (logprobs - ref_logprobs)
            all_rewards = [None] * n
            for ix in range(n):
                rs = rewards[ix][start : ends[ix]]
                rs[-1] = scores[ix]
                all_rewards[ix] = rs

            new_ppo_rl_elements = [
                PPORLElement(
                    query_tensor=query_tensors[i],
                    response_tensor=response_tensors[i],
                    logprobs=all_logprobs[i],
                    values=all_values[i],
                    rewards=all_rewards[i],
                )
                for i in range(n)
            ]

            ppo_rl_elements += new_ppo_rl_elements
            exp_time = clock.tick()

        stats["kl_ctl_value"] = self.rl_model.kl_ctl.value
        stats["time/exp"] = exp_time

        if not ray.is_initialized():
            self.rl_model.accelerator.log(stats, step=iter_count)

        # Push samples and rewards to model's rollout storage
        self.rl_model.push_to_store(ppo_rl_elements)

@register_orchestrator
class T5PPOOrchestrator(PPOOrchestrator):

    def __init__(
        self,
        model: T5AcceleratePPOModel,
        pipeline: BasePipeline,
        reward_fn: Callable,
        metric_fn: Callable = None,
        chunk_size: int = 512,
    ):
        super().__init__(model, pipeline, reward_fn, metric_fn, chunk_size)

        print(" ===== LOADING REFERENCE MODEL =====")
        self.ref_model = transformers.T5ForConditionalGeneration.from_pretrained(
            model.config.model.model_path
        )

        self.reference_device = torch.device(f"cuda:{GPU_REFERENCE_MODEL}")

                    #if GPU_REFERENCE_MODEL:
        print(" ===== MOVING REFERENCE MODEL TO GPU =====")
        self.ref_model = self.ref_model.to(torch.bfloat16).to(self.reference_device)


    def make_experience(self, num_rollouts: int = 1024, iter_count: int = 0):
        """
        Takes `num_rollouts` prompts from `pipeline`, samples model, computes KL againts a reference model appends PPOElements to model's `store`
        """
        ppo_rl_elements = []
        stats = {}
        clock = Clock()
        while len(ppo_rl_elements) < num_rollouts:
            # Get next batch in prompt dataset and refresh if exhausted
            try:
                batch: PromptBatch = next(self.pipeline_iterator)
            except StopIteration:
                self.pipeline_iterator = iter(self.pipeline_loader)
                batch = next(self.pipeline_iterator)

            exp_generate_time = time()
            samples = self.rl_model.generate(**batch, use_cache=True, max_new_tokens=50, do_sample=True)
            stats["time/exp_generate"] = time() - exp_generate_time

            query_tensors = batch.input_ids
            response_tensors = samples
            response_texts = self.rl_model.tokenizer.batch_decode(
                samples, skip_special_tokens=True
            )

            query_texts = self.rl_model.tokenizer.batch_decode(
                query_tensors, skip_special_tokens=True
            )

            texts = [f"{q} {r}" for q, r in zip(query_texts, response_texts)]

            exp_score_time = time()
            scores = torch.tensor(
                self.score(texts), device=samples.device, dtype=torch.float
            )
            stats["time/exp_score"] = time() - exp_score_time

            # store statistics of the initial rollout as reference
            if self.ref_mean is None:
                self.ref_mean, self.ref_std = scores.mean(), scores.std()
            all_scores_mean, all_scores_std = self.running.update(scores)
            stats["exp_scores/mean"] = all_scores_mean
            stats["exp_scores/std"] = all_scores_std
            stats["exp_scores/running_mean"] = self.running.mean
            stats["exp_scores/running_std"] = self.running.std

            if self.rl_model.config.method.scale_reward == "running":
                scores /= self.running.std
            elif self.rl_model.config.method.scale_reward == "ref":
                scores /= self.ref_std

            clip_reward = self.rl_model.config.method.cliprange_reward
            if clip_reward:
                scores = torch.clip(scores, -clip_reward, clip_reward)

            # Precompute logprobs, values
            input_ids, attention_mask, decoder_input_ids, decoder_attention_mask = self.rl_model.get_model_inputs(
                query_tensors.to(response_tensors.device), response_tensors
            )

            with torch.no_grad():
                outputs = self.rl_model.model(
                    input_ids, attention_mask, decoder_input_ids, decoder_attention_mask
                )
                logits, values = outputs.logits, outputs.value
                ref_logits = self.ref_model(
                    input_ids=input_ids.to(self.reference_device),
                    attention_mask=attention_mask.to(self.reference_device),
                    decoder_input_ids=decoder_input_ids.to(self.reference_device),
                    decoder_attention_mask=decoder_attention_mask.to(self.reference_device),
                ).logits

                ref_logits = ref_logits.to(self.rl_model.accelerator.device)

            logprobs = logprobs_from_logits(logits[:, :-1, :], decoder_input_ids[:, 1:])
            ref_logprobs = logprobs_from_logits(
                ref_logits[:, :-1, :], decoder_input_ids[:, 1:]
            )

            n = samples.shape[0]
            values = values.cpu()[:, :-1]
            logprobs = logprobs.cpu()
            ref_logprobs = ref_logprobs.cpu()
            query_tensors = query_tensors.cpu()
            response_tensors = response_tensors.cpu()

            all_values = [values[ix] for ix in range(n)]
            all_logprobs = [logprobs[ix] for ix in range(n)]

            # Compute rewards
            rewards = -self.rl_model.kl_ctl.value * (logprobs - ref_logprobs)
            all_rewards = [None] * n
            for ix in range(n):
                rs = rewards[ix]
                rs[-1] = scores[ix]
                all_rewards[ix] = rs

            new_ppo_rl_elements = [
                PPORLElement(
                    query_tensor=query_tensors[i],
                    response_tensor=response_tensors[i],
                    logprobs=all_logprobs[i],
                    values=all_values[i],
                    rewards=all_rewards[i],
                )
                for i in range(n)
            ]

            ppo_rl_elements += new_ppo_rl_elements
            exp_time = clock.tick()

        stats["kl_ctl_value"] = self.rl_model.kl_ctl.value
        stats["time/exp"] = exp_time

        if not ray.is_initialized():
            self.rl_model.accelerator.log(stats, step=iter_count)

        # Push samples and rewards to model's rollout storage
        self.rl_model.push_to_store(ppo_rl_elements)
