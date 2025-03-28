# SPDX-License-Identifier: Apache-2.0
import torch

from vllm.config import VllmConfig
from vllm.forward_context import set_forward_context
from vllm.v1.attention.backends.flash_attn import FlashAttentionMetadata
from vllm.v1.sample.metadata import SamplingMetadata


class EagleProposer:

    def __init__(
        self,
        vllm_config: VllmConfig,
        num_speculative_tokens: int,
    ):
        self.model = ...
        self.vllm_config = vllm_config
        self.num_speculative_tokens = num_speculative_tokens
        self.block_size = vllm_config.cache_config.block_size

    def propose(
        self,
        # [num_tokens]
        target_token_ids: torch.Tensor,
        # [num_tokens]
        target_positions: torch.Tensor,
        # [num_tokens, hidden_size]
        target_hidden_states: torch.Tensor,
        # [batch_size]
        next_token_ids: torch.Tensor,
        # [batch_size + 1]
        cu_num_tokens: torch.Tensor,
        max_num_tokens: int,
        # [batch_size, max_num_blocks_per_req]
        block_table: torch.Tensor,
        sampling_metadata: SamplingMetadata,
    ) -> torch.Tensor:  # [batch_size, num_speculative_tokens]
        num_tokens = target_token_ids.shape[0]
        batch_size = next_token_ids.shape[0]

        last_token_indices = cu_num_tokens[1:] - 1
        input_ids = target_token_ids
        input_ids[last_token_indices] = next_token_ids

        seq_lens = target_positions[last_token_indices] + 1
        max_seq_len = seq_lens.max().item()  # FIXME: Avoid synchronization.
        slot_mapping = compute_slot_mapping(
            positions=target_positions,
            block_table=block_table,
            block_size=self.block_size,
        )
        attn_metadata = FlashAttentionMetadata(
            num_actual_tokens=num_tokens,
            max_query_len=max_num_tokens,
            query_start_loc=cu_num_tokens,
            max_seq_len=max_seq_len,
            seq_lens=seq_lens,
            block_table=block_table,
            slot_mapping=slot_mapping,
            # TODO(woosuk): Add cascade attention.
            use_cascade=False,
            common_prefix_len=0,
            cu_prefix_query_lens=None,
            prefix_kv_lens=None,
            suffix_kv_lens=None,
        )

        with set_forward_context(attn_metadata, self.vllm_config):
            hidden_states = self.model(
                input_ids=input_ids,
                hidden_states=target_hidden_states,
                positions=target_positions,
            )
        sample_hidden_states = hidden_states[last_token_indices]
        logits = self.model.compute_logits(sample_hidden_states, None)

        # Sample the next token.
        draft_token_ids = sample_token_ids(logits, sampling_metadata)

        # Early exit if there is only one draft token to be generated.
        if self.num_speculative_tokens == 1:
            return draft_token_ids.view(-1, 1)

        # Generate the remaining draft tokens.
        draft_token_ids_list: list[torch.Tensor] = [draft_token_ids]
        positions = target_positions[last_token_indices]
        hidden_states = sample_hidden_states
        attn_metadata.num_actual_tokens = batch_size
        attn_metadata.query_start_loc = torch.arange(batch_size,
                                                     device=positions.device)
        for _ in range(self.num_speculative_tokens - 1):
            # Update the inputs.
            input_ids = draft_token_ids_list[-1]
            positions += 1
            attn_metadata.max_query_len = 1
            attn_metadata.max_seq_len += 1
            attn_metadata.seq_lens += 1
            attn_metadata.slot_mapping = compute_slot_mapping(
                positions=positions,
                block_table=block_table,
                block_size=self.block_size,
            )

            # Run the model.
            hidden_states = self.model(
                input_ids=input_ids,
                hidden_states=hidden_states,
                positions=positions,
            )
            logits = self.model.compute_logits(hidden_states, None)
            draft_token_ids = sample_token_ids(logits, sampling_metadata)
            draft_token_ids_list.append(draft_token_ids)

        # [batch_size, num_speculative_tokens]
        return torch.stack(draft_token_ids_list, dim=1)


def sample_token_ids(
    logits: torch.Tensor,  # [batch_size, vocab_size]
    sampling_metadata: SamplingMetadata,
) -> torch.Tensor:  # [batch_size]
    # NOTE(woosuk): We don't need to apply all the sampling parameters
    # for generating the draft tokens.
    if sampling_metadata.all_greedy:
        # All greedy.
        next_token_ids = logits.argmax(dim=-1)
    else:
        logits.div_(sampling_metadata.temperature)
        probs = logits.softmax(dim=-1, dtype=torch.float32)

        # TODO(woosuk): Consider seeds?
        q = torch.empty_like(logits)
        q.exponential_()
        next_token_ids = probs.div_(q).argmax(dim=-1).view(-1)

        if not sampling_metadata.all_random:
            greedy_token_ids = logits.argmax(dim=-1)
            next_token_ids = torch.where(
                sampling_metadata.temperature == -1,
                greedy_token_ids,
                next_token_ids,
            )
    return next_token_ids


def compute_slot_mapping(
    # [num_tokens]
    positions: torch.Tensor,
    # [batch_size, max_num_blocks_per_req]
    block_table: torch.Tensor,
    block_size: int,
) -> torch.Tensor:  # [num_tokens]
    # [num_tokens]
    block_numbers = positions // block_size
    block_ids = block_table.gather(dim=1, index=block_numbers)
    slot_mapping = block_ids * block_size + positions % block_size
    return slot_mapping
