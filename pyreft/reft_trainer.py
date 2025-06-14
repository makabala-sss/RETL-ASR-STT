import pyvene as pv
import torch.nn as nn
from torch.utils.data.sampler import Sampler
from torch.utils.data import DataLoader, DistributedSampler
from transformers import (
    Trainer,
    TrainingArguments,
    DataCollator,
    DataCollatorForSeq2Seq,
    AutoTokenizer
)
from transformers.trainer_utils import (
    EvalPrediction,
    has_length,
    denumpify_detensorize
)
from datasets import Dataset
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Union, Iterable

from tqdm import tqdm
import os
import torch
import re

import numpy as np
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss
from transformers.utils import logging
import torch.distributed as dist

logger = logging.get_logger(__name__)

@dataclass
class ReftDataCollator(object):
    """Collate examples for ReFT."""

    data_collator: DataCollator

    def __call__(self, instances: Sequence[Dict]) -> Dict[str, torch.Tensor]:
        batch_inputs = self.data_collator(instances)
        max_seq_length = batch_inputs["input_ids"].shape[-1]
        batch_inputs["intervention_locations"] = batch_inputs["intervention_locations"][..., :max_seq_length]
        return batch_inputs


def make_data_collator(tokenizer, model) -> ReftDataCollator:
    data_collator_fn = DataCollatorForSeq2Seq(
        tokenizer=tokenizer,
        model=model,
        label_pad_token_id=-100,
        padding="longest",
        max_length=2048,
    )
    return ReftDataCollator(data_collator=data_collator_fn)


def make_dataloader(
    dataset: Dataset,
    batch_size: int,
    collate_fn: DataCollatorForSeq2Seq,
    shuffle: bool,
    sampler: Union[Sampler, Iterable, None]=None
) -> DataLoader:
    return DataLoader(dataset, shuffle=shuffle, batch_size=batch_size, sampler=sampler, collate_fn=collate_fn)



class ReftSrTrainer(Trainer):
    def save_model(self, output_dir, _internal_call=False):
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        self.model.save_intervention(
            save_directory=f"{output_dir}/intervenable_model", 
            include_model=True
        )

    def _load_best_model(self):
        logger.warning(f"Loading best model from {self.state.best_model_checkpoint} (score: {self.state.best_metric}).")
        self.model.load_intervention(
            f"{self.state.best_model_checkpoint}/intervenable_model", 
            include_model=True
        )
    def compute_loss(
            self,
            intervenable: pv.IntervenableModel,
            inputs,
            return_outputs=False
        ):
        
        
        unit_locations = None
        if "intervention_locations" in inputs:
            if inputs["intervention_locations"].dim() == 3:
                unit_locations = {
                    "sources->base": (
                        None,
                        inputs["intervention_locations"]
                            .permute(1, 0, 2)
                            .tolist()
                    )
                }
            else:
                unit_locations = {"sources->base": (None, 0)}

        if "input_ids" in inputs:
            data = {
                "input_ids": inputs["input_ids"],
                "attention_mask": inputs["attention_mask"],
            }
        elif "input_values" in inputs:
            data = {
                "input_values": inputs["input_values"],
                "attention_mask": inputs.get("attention_mask"),
            }
        elif "input_features" in inputs:
            data = {
                "input_features": inputs["input_features"],
                "attention_mask": inputs.get("attention_mask"),
            }
        else:
            raise ValueError("Unsupported input structure provided.")

        # 鈥斺€?2. 鍓嶅悜骞惰幏鍙?logits 鈥斺€?
        #    cf_outputs.logits: (batch_size, seq_len, vocab_size)
        _, cf_outputs = intervenable(
            data,
            unit_locations=unit_locations,
            labels=inputs["labels"],
            subspaces=(
                inputs["subspaces"]
                    .permute(1, 0, 2)
                    .tolist()
            ) if "subspaces" in inputs else None
        )
        logits = cf_outputs.logits
        labels = inputs["labels"]
        
        # flatten logits & labels
        vocab_size = logits.size(-1)
        loss_fct = nn.CrossEntropyLoss()
        loss = loss_fct(
            logits.view(-1, vocab_size),
            labels.view(-1),
        )
        
        return (loss, cf_outputs) if return_outputs else loss

class ReftTrainerForCausalLM(ReftTrainer):
    def get_train_dataloader(self) -> DataLoader:
        return make_dataloader(self.train_dataset, self._train_batch_size, self.data_collator, shuffle=True)

class ReftTrainerForCausalLMDistributed(ReftTrainer):
    def save_model(self, output_dir, _internal_call=False):
        if dist.get_rank() == 0:
            super().save_model(output_dir, _internal_call)

    def get_train_dataloader(self) -> DataLoader:
        return make_dataloader(
            self.train_dataset,
            self._train_batch_size,
            self.data_collator,
            shuffle=False,
            sampler=DistributedSampler(self.train_dataset, shuffle=True),
        )

def compute_loss(
        self,
        intervenable: pv.IntervenableModel,
        inputs,
        return_outputs=False
    ):
        # run intervened forward pass
        unit_locations = None
        if "intervention_locations" in inputs:
            if inputs["intervention_locations"].dim() == 3:
                unit_locations={"sources->base": (
                    None,
                    inputs["intervention_locations"].permute(1, 0, 2).tolist()
                )}
            else:
                # this is dummy for lora only baseline
                unit_locations={"sources->base": (None, 0)}

        if "input_ids" in inputs:
            data = {
                "input_ids": inputs["input_ids"],
                "attention_mask": inputs["attention_mask"]
            }
        elif "input_values" in inputs:
            data = {
                "input_values": inputs["input_values"],  # waveforms or spectrograms
                "attention_mask": inputs.get("attention_mask")  # Optional
            }
        elif "input_features" in inputs:
            data = {
                "input_features": inputs["input_features"],  # waveforms or spectrograms
                "attention_mask": inputs.get("attention_mask")  # Optional
            }
        else:
            raise ValueError("Unsupported input structure provided.")
        # base_outputs, cf_outputs = intervenable(
        #     data,
        #     unit_locations=unit_locations,
        #     labels=inputs["labels"],
        #     subspaces=inputs["subspaces"].permute(1, 0, 2).tolist() if "subspaces" in inputs else None
        # )
        _, cf_outputs = intervenable(
            data,
            unit_locations=unit_locations,
            labels=inputs["labels"],
            subspaces=inputs["subspaces"].permute(1, 0, 2).tolist() if "subspaces" in inputs else None
        )
        logits = cf_outputs.logits
        labels = inputs["labels"]
        
        # 需要在模型训练前指定 problem_type
        if self.model.model.config.problem_type == None:
            problem_type = "single_label_classification"
        else:
            problem_type = self.model.model.config.problem_type
        
        if problem_type == "ctc":
            # 假设 logits 的形状为 (batch_size, time_steps, num_classes)
            batch_size, time_steps, num_classes = logits.size()
            
            # 为每个样本构造输入长度，这里简单设置为 time_steps（如果有 padding，需要实际计算）
            input_lengths = torch.full(size=(batch_size,), fill_value=time_steps, dtype=torch.long)
            
            # 获取 pad_token_id，用于计算 target_lengths（去除填充部分）
            pad_token_id = self.model.model.config.pad_token_id
            target_lengths = (labels != pad_token_id).sum(dim=1)
            
            # CTCLoss 要求 logits 的形状为 (time_steps, batch_size, num_classes)
            logits = logits.transpose(0, 1)
            
            # 创建 CTCLoss，计算损失
            loss_fct = nn.CTCLoss(blank=pad_token_id, zero_infinity=True)
            loss = loss_fct(logits, labels, input_lengths, target_lengths)

        elif problem_type == "single_label_classification":
            logits = logits.view(-1, logits.size(-1))  # 变为 (batch_size * sequence_length, vocab_size)
            labels = labels.view(-1)  # 变为 (batch_size * sequence_length,)
            loss_fct = nn.CrossEntropyLoss()
            loss = loss_fct(logits, labels)
        elif problem_type == "multi_label_classification":
            logits = logits.view(-1, logits.size(-1))  # 变为 (batch_size * sequence_length, vocab_size)
            labels = labels.view(-1)  # 变为 (batch_size * sequence_length,)
            loss_fct = nn.BCEWithLogitsLoss()
            loss = loss_fct(logits, labels)
        else:
            raise ValueError(f"Unknown problem type: {problem_type}, please pass the problem_type before train!!")

        


        return (loss, cf_outputs) if return_outputs else loss


class ReftTrainerForSequenceClassification(ReftTrainer):
    def compute_loss(
        self,
        intervenable: pv.IntervenableModel,
        inputs,
        return_outputs=False
    ):
        # run intervened forward pass
        unit_locations = None
        if "intervention_locations" in inputs:
            unit_locations={"sources->base": (
                None,
                inputs["intervention_locations"].permute(1, 0, 2).tolist()
            )}
            
        _, cf_outputs = intervenable(
            {
                "input_ids": inputs["input_ids"],
                "attention_mask": inputs["attention_mask"]
            },
            unit_locations=unit_locations,
            labels=inputs["labels"],
            subspaces=inputs["subspaces"].permute(1, 0, 2).tolist() if "subspaces" in inputs else None
        )
        # classification loss on counterfactual labels
        logits = cf_outputs.logits
        labels = inputs["labels"]

        if self.model.model.config.problem_type is None:
            if self.model.model.num_labels == 1:
                problem_type = "regression"
            elif self.model.model.num_labels > 1 and (labels.dtype == torch.long or labels.dtype == torch.int):
                problem_type = "single_label_classification"
            else:
                problem_type = "multi_label_classification"
        else:
            problem_type = self.model.model.config.problem_type
            
        if problem_type == "regression":
            loss_fct = MSELoss()
            if self.model.model.num_labels == 1:
                loss = loss_fct(logits.squeeze(), labels.squeeze().to(torch.bfloat16))
            else:
                loss = loss_fct(logits, labels.to(torch.bfloat16))
        elif problem_type == "single_label_classification":
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(logits.view(-1, self.model.model.num_labels), labels.view(-1))
        elif problem_type == "multi_label_classification":
            loss_fct = BCEWithLogitsLoss()
            loss = loss_fct(logits, labels)

        # return
        return (loss, cf_outputs) if return_outputs else loss
    
    def evaluate(
        self, ignore_keys,
    ):

        # ensure everything is in eval mode
        self.model.model.eval()
        for k,v in  self.model.interventions.items():
            _ = v[0].eval()
        
        batch_size = self.args.eval_batch_size
        data_collator = self.data_collator
        eval_dataset = self.eval_dataset
        intervenable = self.model
        
        dataloader = make_dataloader(
            eval_dataset, batch_size, data_collator, shuffle=False)

        logger.info(f"***** Running In-Training Evaluation *****")
        if has_length(dataloader):
            logger.info(f"  Num examples = {self.num_examples(dataloader)}")
        else:
            logger.info("  Num examples: Unknown")
        logger.info(f"  Batch size = {batch_size}")

        eval_iterator = tqdm(dataloader, position=0, leave=True)
        all_preds = []
        all_labels = []
        with torch.no_grad():
            for step, inputs in enumerate(eval_iterator):
                for k, v in inputs.items():
                    if v is not None and isinstance(v, torch.Tensor):
                        inputs[k] = v.to(self.model.get_device())
                
                # [layers, batch_size, positions]
                intervention_locations = inputs["intervention_locations"].permute(1, 0, 2).tolist()
                _, cf_outputs = intervenable(
                    {"input_ids": inputs["input_ids"], "attention_mask": inputs["attention_mask"]},
                    unit_locations={"sources->base": (None, intervention_locations)})
            
                all_preds += [cf_outputs.logits]
                all_labels += [inputs["labels"]]
        all_preds = torch.cat(all_preds, dim=0).cpu().to(torch.float32)
        all_labels = torch.cat(all_labels, dim=0).cpu().to(torch.float32)
        metrics = self.compute_metrics(EvalPrediction(predictions=all_preds, label_ids=all_labels))
        metrics = denumpify_detensorize(metrics)
        
        metric_key_prefix = "eval"
        for key in list(metrics.keys()):
            if not key.startswith(f"{metric_key_prefix}_"):
                metrics[f"{metric_key_prefix}_{key}"] = metrics.pop(key)
        
        self.log(metrics)
        self.control = self.callback_handler.on_evaluate(self.args, self.state, self.control, metrics)
        self._memory_tracker.stop_and_update_metrics(metrics)
        
        return metrics
        
