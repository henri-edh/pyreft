import os
from typing import Dict, List, Literal, Optional, Union, Tuple
from trl import DPOTrainer
import torch
import torch.nn as nn

class DPOReftTrainer(DPOTrainer):
    def concatenated_forward(
        self, model: nn.Module, batch: Dict[str, Union[List, torch.LongTensor]], reference: bool = False
    ) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
        """Run the given model on the given batch of inputs, concatenating the chosen and rejected inputs together.

        We do this to avoid doing two forward passes, because it's faster for FSDP.
        """
        concatenated_batch = self.concatenated_inputs(
            batch,
            is_encoder_decoder=self.is_encoder_decoder,
            label_pad_token_id=self.label_pad_token_id,
            padding_value=self.padding_value,
            device=self.accelerator.device,
        )
        len_chosen = batch["chosen_labels"].shape[0]

        intervention_locations = 3

        model_kwargs = (
            {
                "labels": concatenated_batch["concatenated_labels"],
                "decoder_input_ids": concatenated_batch.pop("concatenated_decoder_input_ids", None),
            }
            if self.is_encoder_decoder
            else {}
        )
        if reference:
            all_outputs, _ = model(
                {
                    "input_ids": concatenated_batch["concatenated_input_ids"].to(model.get_device()),
                    "attention_mask": concatenated_batch["concatenated_attention_mask"].to(model.get_device()),
                },
                unit_locations={"sources->base": intervention_locations},
                use_cache=False,
                output_original_output=True,
                **model_kwargs,
            )
        else:
            _, all_outputs = model(
                {
                    "input_ids": concatenated_batch["concatenated_input_ids"].to(model.get_device()),
                    "attention_mask": concatenated_batch["concatenated_attention_mask"].to(model.get_device()),
                },
                unit_locations={"sources->base": intervention_locations},
                use_cache=False,
                **model_kwargs,
            )

        all_logits = all_outputs.logits

        all_logps = self.get_batch_logps(
            all_logits,
            concatenated_batch["concatenated_labels"],
            average_log_prob=self.loss_type == "ipo",
            is_encoder_decoder=self.is_encoder_decoder,
            label_pad_token_id=self.label_pad_token_id,
        )

        chosen_logps = all_logps[:len_chosen]
        rejected_logps = all_logps[len_chosen:]

        chosen_logits = all_logits[:len_chosen]
        rejected_logits = all_logits[len_chosen:]

        return (chosen_logps, rejected_logps, chosen_logits, rejected_logits)

    def get_batch_loss_metrics(
        self,
        model,
        batch: Dict[str, Union[List, torch.LongTensor]],
        train_eval: Literal["train", "eval"] = "train",
    ):
        """Compute the DPO loss and other metrics for the given batch of inputs for train or test."""
        metrics = {}

        (
            policy_chosen_logps,
            policy_rejected_logps,
            policy_chosen_logits,
            policy_rejected_logits,
        ) = self.concatenated_forward(model, batch, reference=False)

        # if reference_chosen_logps and reference_rejected_logps in batch use them, otherwise use the reference model
        if "reference_chosen_logps" in batch and "reference_rejected_logps" in batch:
            reference_chosen_logps = batch["reference_chosen_logps"]
            reference_rejected_logps = batch["reference_rejected_logps"]
        else:
            with torch.no_grad():
                (
                    reference_chosen_logps,
                    reference_rejected_logps,
                    _,
                    _,
                ) = self.concatenated_forward(self.model, batch, reference=True)

        losses, chosen_rewards, rejected_rewards = self.dpo_loss(
            policy_chosen_logps,
            policy_rejected_logps,
            reference_chosen_logps,
            reference_rejected_logps,
        )
        reward_accuracies = (chosen_rewards > rejected_rewards).float()

        prefix = "eval_" if train_eval == "eval" else ""
        metrics[f"{prefix}rewards/chosen"] = chosen_rewards.mean().cpu()
        metrics[f"{prefix}rewards/rejected"] = rejected_rewards.mean().cpu()
        metrics[f"{prefix}rewards/accuracies"] = reward_accuracies.mean().cpu()
        metrics[f"{prefix}rewards/margins"] = (chosen_rewards - rejected_rewards).mean().cpu()
        metrics[f"{prefix}logps/rejected"] = policy_rejected_logps.detach().mean().cpu()
        metrics[f"{prefix}logps/chosen"] = policy_chosen_logps.detach().mean().cpu()
        metrics[f"{prefix}logits/rejected"] = policy_rejected_logits.detach().mean().cpu()
        metrics[f"{prefix}logits/chosen"] = policy_chosen_logits.detach().mean().cpu()

        return losses.mean(), metrics

    def get_batch_loss_metrics_old(
        self,
        model,
        batch: Dict[str, Union[List, torch.LongTensor]],
        train_eval: Literal["train", "eval"] = "train",
    ):
        concatenated_batch = self.concatenated_inputs(
            batch,
            is_encoder_decoder=self.is_encoder_decoder,
            label_pad_token_id=self.label_pad_token_id,
            padding_value=self.padding_value,
            device=self.accelerator.device,
        )
        len_chosen = batch["chosen_labels"].shape[0]
        
        # unpack the concatenated batch
        chosen_batch = {
            k: v[:len_chosen] for k, v in concatenated_batch.items()
        }
        rejected_batch = {
            k: v[len_chosen:] for k, v in concatenated_batch.items()
        }

        intervention_locations = 3 # 10 # batch["intervention_locations"][0]

        # forward pass for chosen samples
        model_kwargs = (
            {
                "labels": chosen_batch["concatenated_labels"].to(model.get_device()),
                "decoder_input_ids": chosen_batch.pop("concatenated_decoder_input_ids", None),
            }
            if self.is_encoder_decoder
            else {}
        )

        with torch.no_grad():
            reference_chosen_outputs, _ = model(
                {
                    "input_ids": concatenated_batch["concatenated_input_ids"].to(model.get_device()),
                    "attention_mask": concatenated_batch["concatenated_attention_mask"].to(model.get_device()),
                },
                unit_locations={"sources->base": intervention_locations},
                use_cache=False,
                output_original_output=True,
                **model_kwargs,
            )

        _, policy_chosen_outputs = model(
            {
                "input_ids": concatenated_batch["concatenated_input_ids"].to(model.get_device()),
                "attention_mask": concatenated_batch["concatenated_attention_mask"].to(model.get_device()),
            },
            unit_locations={"sources->base": intervention_locations},
            use_cache=False,
            **model_kwargs,
        )

        reference_chosen_logits = reference_chosen_outputs.logits
        policy_chosen_logits = policy_chosen_outputs.logits

        # forward pass for rejected samples
        model_kwargs = (
            {
                "labels": rejected_batch["concatenated_labels"].to(model.get_device()),
                "decoder_input_ids": rejected_batch.pop("concatenated_decoder_input_ids", None),
            }
            if self.is_encoder_decoder
            else {}
        )

        reference_rejected_outputs, policy_rejected_outputs = model(
            {
                "input_ids": rejected_batch["concatenated_input_ids"].to(model.get_device()),
                "attention_mask": rejected_batch["concatenated_attention_mask"].to(model.get_device()),
            },
            unit_locations={"sources->base": intervention_locations},
            use_cache=False,
            output_original_output=True,
            **model_kwargs,
        )
        reference_rejected_logits = reference_rejected_outputs.logits
        policy_rejected_logits = policy_rejected_outputs.logits

        # compute log probabilities
        all_logits = torch.cat([policy_chosen_logits, policy_rejected_logits], dim=0)
        all_logps = self.get_batch_logps(
            all_logits,
            concatenated_batch["concatenated_labels"],
            average_log_prob=self.loss_type == "ipo",
            is_encoder_decoder=self.is_encoder_decoder,
            label_pad_token_id=self.label_pad_token_id,
        )

        policy_chosen_logps = all_logps[:len_chosen]
        policy_rejected_logps = all_logps[len_chosen:]

        all_reference_logits = torch.cat([reference_chosen_logits, reference_rejected_logits], dim=0)
        all_reference_logps = self.get_batch_logps(
            all_reference_logits,
            concatenated_batch["concatenated_labels"],
            average_log_prob=self.loss_type == "ipo",
            is_encoder_decoder=self.is_encoder_decoder,
            label_pad_token_id=self.label_pad_token_id,
        )

        reference_chosen_logps = all_reference_logps[:len_chosen]
        reference_rejected_logps = all_reference_logps[len_chosen:]

        losses, chosen_rewards, rejected_rewards = self.dpo_loss(
            policy_chosen_logps,
            policy_rejected_logps,
            reference_chosen_logps,
            reference_rejected_logps,
        )
        reward_accuracies = (chosen_rewards > rejected_rewards).float()

        metrics = {}
        prefix = "eval_" if train_eval == "eval" else ""
        metrics[f"{prefix}rewards/chosen"] = chosen_rewards.mean().cpu()
        metrics[f"{prefix}rewards/rejected"] = rejected_rewards.mean().cpu()
        metrics[f"{prefix}rewards/accuracies"] = reward_accuracies.mean().cpu()
        metrics[f"{prefix}rewards/margins"] = (chosen_rewards - rejected_rewards).mean().cpu()
        metrics[f"{prefix}logps/rejected"] = policy_rejected_logps.detach().mean().cpu()
        metrics[f"{prefix}logps/chosen"] = policy_chosen_logps.detach().mean().cpu()
        metrics[f"{prefix}logits/rejected"] = policy_rejected_logits.detach().mean().cpu()
        metrics[f"{prefix}logits/chosen"] = policy_chosen_logits.detach().mean().cpu()

        return losses.mean(), metrics
    
    def save_model(self, output_dir: Optional[str] = None, _internal_call: bool = False):
        os.makedirs(output_dir, exist_ok=True)