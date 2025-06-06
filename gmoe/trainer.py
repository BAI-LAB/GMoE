from .tasks import CasualTask, MultiTask, task_dict
from .dispatcher import TrainTask, Dispatcher
from .common import LoraConfig, DataClass, LoraBatchDataConfig, MultiLoraBatchData, MixConfig
from .model import LLMModel

from transformers import get_scheduler
from dataclasses import dataclass
from typing import Dict, List, Union
import logging
import torch
import json
import os
import time
import math
import copy

from .tasks import (
    BasicTask,
    BasicMetric,
    CommonSenseTask,
    task_dict,
)
from .tokenizer import Tokenizer

@dataclass
class ValidationConfig:
    adapter_name: str = None
    task_name: str = None
    batch_size: int = 16
    router_profile: bool = False
    # Do not set these manually
    task_: BasicTask = None
    data_: List[DataClass] = None
    #metric_: BasicMetric = None
    rollback_start_idx_: int = 0
    batch_start_idx_: int = 0
    batch_end_idx_: int = 0
    start_step: int = -1

    def prepare(self, tokenizer: Tokenizer, device: str):
        self.rollback_start_idx_: int = 0
        self.batch_start_idx_: int = 0
        self.batch_end_idx_: int = 0
        if self.data_ == None:
            # if self.task_ != task_dict[self.task_name]:
            self.task_ = task_dict[self.task_name]
            #self.metric_ = self.task_.loading_metric()
            with torch.no_grad():
                self.data_ori = self.task_.loading_data(tokenizer, is_train = False, val = True)
                self.data_ = copy.deepcopy(self.data_ori)
        else:
            with torch.no_grad():
                self.data_ = copy.deepcopy(self.data_ori)
        if isinstance(self.task_, CommonSenseTask):
            labels = self.task_.label_list()
            label_indices = [0] * len(labels)
            for idx, label in enumerate(labels):
                ids = tokenizer.encode(" " + label)
                label_indices[idx] = ids[-1]
            self.label_indices_ = torch.tensor(
                label_indices, dtype=torch.int64, device=device)
        else:
            self.label_indices_ = None



class TrainConfig:
    def __init__(self,
                 train_config: Dict[str, any],
                 lora_config: LoraConfig):
        self.adapter_name_ = lora_config.adapter_name
        self.batch_size_ = train_config["batch_size"]
        self.micro_batch_size_ = train_config.get(
            "micro_batch_size", self.batch_size_)
        self.optimizer_name_ = train_config.get("optim", "adamw")
        self.learning_rate_ = train_config["lr"]
        # loraplus learning rate ratio lr_B / lr_A
        self.loraplus_lr_ratio_ = train_config.get("loraplus_lr_ratio", 1.0)
        self.momentum_ = train_config.get("momentum", 0)
        self.weight_decay_ = train_config.get("weight_decay", 0.01)
        # Scheduler Types
        #   constant, linear, cosine, cosine_with_restarts, polynomial
        #   constant_with_warmup, inverse_sqrt, reduce_lr_on_plateau
        self.scheduler_type_: str = train_config.get(
            "scheduler_type", "constant")
        self.warmup_ratio_: Union[int, float] = train_config.get(
            "warmup_ratio", 0)
        self.lr_scheduler_: torch.optim.lr_scheduler.LRScheduler = None
        self.accumulation_step_: int = None
        self.accumulation_step_cnt_: int = 0
        self.optimizer_: torch.optim.Optimizer = None
        task_name = train_config.get("task_name", "casual")
        if task_name == "casual":
            self.task_ = CasualTask(
                data_path=train_config["data"],
                prompt_template=train_config.get("prompt", None),
                validation_size=train_config.get("val_set_size", None))
        elif ';' in task_name:
            self.task_ = MultiTask(task_name)
        else:
            self.task_ = task_dict[task_name]
        train_config["dataloader"] = self.task_.loading_data

    def _optimizer_grouped_parameters(self, train_paramas: Dict[str, torch.Tensor]):
        assert self.loraplus_lr_ratio_ >= 1.0
        if self.loraplus_lr_ratio_ == 1.0:
            return [{
                'params': list(params for params in train_paramas.values() if torch.is_tensor(params) and params.requires_grad),
                'lr': self.learning_rate_,
            }]
        logging.info(f"Initializing {self.adapter_name_} for LoRA+")
        param_groupA = []
        param_groupB = []
        for name, param in train_paramas.items():
            if not param.requires_grad:
                continue
            if "lora_B" in name or param.ndim == 1:
                param_groupB.append(param)
            else:
                param_groupA.append(param)

        return [{'params': param_groupA,
                 'lr': self.learning_rate_,
                 },
                {'params': param_groupB,
                 'lr': self.learning_rate_ * self.loraplus_lr_ratio_,
                 }]

    def prepare(self, train_params: Dict[str, torch.Tensor]):
        # preparing batch size and gradient accumulation
        if self.batch_size_ < self.micro_batch_size_ or self.batch_size_ % self.micro_batch_size_ != 0:
            raise ValueError(
                f"error batch_size {self.batch_size_} and micro batch size {self.micro_batch_size_}")
        self.accumulation_step_ = self.batch_size_ / self.micro_batch_size_
        self.accumulation_step_cnt_ = 0
        # preparing optimizer
        paramas_count = sum(t.numel()
                            for t in train_params.values() if torch.is_tensor(t) and t.requires_grad)
        logging.info(
            f"{self.adapter_name_} total trainable params: {paramas_count}")
        grouped_parameters = self._optimizer_grouped_parameters(train_params)
        if self.optimizer_name_ == "sgd":
            self.optimizer_ = torch.optim.SGD(
                grouped_parameters, momentum=self.momentum_, weight_decay=self.weight_decay_)
        elif self.optimizer_name_ == "adamw":
            self.optimizer_ = torch.optim.AdamW(
                grouped_parameters, weight_decay=self.weight_decay_)
        else:
            raise ValueError(f"unkown optimizer {self.optimizer_name_}")

    def prepare_lr_scheduler(self, total_epoch, len_dataset):
        if self.lr_scheduler_ is None:
            total_steps = (len_dataset // self.batch_size_) * total_epoch if len_dataset % self.batch_size_ == 0 else (
                len_dataset // self.batch_size_ + 1) * total_epoch
            self.lr_scheduler_ = get_scheduler(
                self.scheduler_type_, self.optimizer_, self.warmup_ratio_ * total_steps, total_steps)

    def step(self):
        self.accumulation_step_cnt_ += 1
        if self.accumulation_step_cnt_ % self.accumulation_step_ == 0:
            self.optimizer_.step()
            self.lr_scheduler_.step()
            self.optimizer_.zero_grad()

    def finish(self):
        self.optimizer_.step()
        self.optimizer_.zero_grad()


def save_adapter_weight(model: LLMModel, config: TrainConfig, path: str, dir_suffix="", acc = None):
    lora_output_dir = path + os.sep + config.adapter_name_
    cnt = dir_suffix
    if dir_suffix != "":
        if "best" in dir_suffix:
            dir_suffix = "best"
        lora_output_dir += os.sep + \
            config.adapter_name_ + "_" + dir_suffix

    if not os.path.exists(lora_output_dir):
        os.makedirs(lora_output_dir)

    lora_weight_dict = model.get_lora_weight_dict(config.adapter_name_)
    lora_config_dict = model.adapter_configs_[config.adapter_name_].export()
    lora_config_dict["base_model_name_or_path"] = model.name_or_path_
    lora_config_dict["task_type"] = config.task_.peft_task_type
    if "best" in cnt:
        lora_config_dict["cnt"] = cnt
        lora_config_dict["best_acc"] = acc.item()

    torch.save(lora_weight_dict, lora_output_dir +
               os.sep + "adapter_model.bin")

    with open(lora_output_dir + os.sep + "adapter_config.json", "w") as f:
        json.dump(lora_config_dict, f, indent=4)


def _dispatch_task_in(tokenizer, config, max_seq_len):
    batch_data_config = []
    sequence_lengths = []
    current_configs = []
    batch_tokens = []
    batch_labels = []
    atten_masks = []
    max_tokens_len = 0

    if config.batch_start_idx_ < len(config.data_):
        config.batch_end_idx_ = min(
            config.batch_start_idx_ + config.batch_size, len(config.data_))
        batch_start_idx = len(batch_tokens)
        for idx in range(config.batch_start_idx_, config.batch_end_idx_):
            if idx >= len(config.data_):
                break
            tokens = config.data_[idx].tokens_
            labels = config.data_[idx].labels_
            if len(tokens) > max_seq_len:
                tokens = tokens[:max_seq_len]
            max_tokens_len = max(len(tokens), max_tokens_len)
            # sequence_lengths.append(len(tokens))
            # while len(tokens) < max_seq_len:
            #     tokens.append(tokenizer.pad_id_)
            batch_tokens.append(tokens)
            # atten_masks.append(tokenizer.mask_from(tokens))
            batch_labels.append(labels.copy())

        config.batch_start_idx_ = config.batch_end_idx_
        current_configs.append(config)
        batch_data_config.append(LoraBatchDataConfig(adapter_name_=config.adapter_name,
                                                    batch_start_idx_=batch_start_idx, batch_end_idx_=len(batch_tokens)))


    if max_tokens_len < max_seq_len:
        max_seq_len = math.ceil(max_tokens_len / 8) * 8

    for tokens in batch_tokens:
        sequence_lengths.append(len(tokens) - 1)
        while len(tokens) < max_seq_len:
            tokens.append(tokenizer.pad_id_)
        atten_masks.append(tokenizer.mask_from(tokens))

    return (current_configs,
            sequence_lengths,
            batch_labels,
            MultiLoraBatchData(
                lora_batch_data_config_=batch_data_config,
                batch_tokens_=batch_tokens,
                attention_masks_=atten_masks,
                inference_mode_=True))


def _compute_metrcis(current_configs, sequence_lengths, batch_labels, outputs):
    right = 0 
    tot = 0 
    for idx, output in enumerate(outputs):
        config = current_configs[idx]
        task: BasicTask = config.task_
        #metric: BasicMetric = config.metric_
        start_idx = output.batch_start_idx_
        end_idx = output.batch_end_idx_
        logits = output.logits

        batch_size = logits.shape[0]
        pooled_logits = logits[torch.arange(
            batch_size, device=logits.device), sequence_lengths[start_idx:end_idx]]
        labels = torch.tensor(batch_labels[start_idx:end_idx],
                              dtype=task.label_dtype_, device=logits.device)
        if task.task_type_ == "common_sense":
            pooled_logits = pooled_logits[:, config.label_indices_]
            pooled_logits = pooled_logits.softmax(-1).argmax(-1)
        elif task.task_type_ == "single_label_classification":
            pooled_logits = pooled_logits.softmax(-1).argmax(-1)
            pooled_logits = pooled_logits.to(task.label_dtype_)
        elif task.task_type_ != "multi_label_classification":
            raise ValueError(f"unknown task type {task.task_type_}")

        tot += pooled_logits.shape[0] #新加的
        right += torch.sum(labels.squeeze() == pooled_logits) #新加的

        #metric.add_batch(predictions=pooled_logits.detach().cpu(),
        #                 references=labels.detach().cpu())
        logging.info(
            f"{config.adapter_name}, {config.task_name}")
        logging.info(
            f"    step: {config.batch_start_idx_}/{len(config.data_)}")
    return right / tot 


def run_validation(model: LLMModel, valconfig: ValidationConfig, tokenizer: Tokenizer,
             retrying_steps: int = 20,
             max_seq_len: int = 4096,
             save_file: str = None):
    print("validation")
    valconfig.prepare(tokenizer, model.device_)
    while True:
        current_configs, sequence_lengths, batch_labels, input_args = _dispatch_task_in(
            tokenizer, valconfig, max_seq_len)
        if len(current_configs) == 0:
            break
        """_compute_metrcis(current_configs,
                             sequence_lengths, batch_labels,
                             model.forward(input_args))"""
        
        ans = _compute_metrcis(current_configs,#新加的
                             sequence_lengths, batch_labels,#新加的
                             model.forward(input_args))        #新加的

        for config in current_configs:
            config.rollback_start_idx_ = config.batch_start_idx_
    # return config.metric_.compute()['accuracy']
    return ans 

def train(dispatcher: Dispatcher,
          model: LLMModel,
          configs: List[TrainConfig],
          valconfigs: ValidationConfig,
          save_dir: str = ".",
          save_step: int = 2000,
          ) -> None:
    config_dict = {}
    val_dict = {}
    best_val = {}
    for config in configs:
        config_dict[config.adapter_name_] = config
    for val in valconfigs:
        val_dict[val.adapter_name] = val
        best_val[val.adapter_name] = 0

    def task_in_callback(task: TrainTask):
        adapter_name = task.adapter_name_
        logging.info(f"Loading training task {adapter_name}")
        config = config_dict[adapter_name]
        config.prepare(model.get_lora_weight_dict(adapter_name))
        config.prepare_lr_scheduler(
            task.total_epoch_num_, len(task.train_token_data_))

    dispatcher.train_task_in_event_.register(task_in_callback)

    step_cnt = 0
    while not dispatcher.check_task_done():
        input_args = dispatcher.get_train_data()
        input_args.gradient_checkpoint_ = "none"
        step_cnt += 1

        outputs = model.forward(input_args)

        total_loss = None
        for output in outputs:
            adapter_name = output.adapter_name
            loss = output.loss / config_dict[adapter_name].accumulation_step_
            logging.info(
                f"    adapter: {adapter_name} loss: {loss}")
            if output.aux_loss:
                aux_loss = output.aux_loss / \
                    config_dict[adapter_name].accumulation_step_
                logging.info(
                    f"    adapter: {adapter_name}  aux: {aux_loss}")
                loss += aux_loss
            if total_loss is None:
                total_loss = loss
            else:
                total_loss += loss

        total_loss.backward()

        for output in outputs:
            config = config_dict[output.adapter_name]
            valconf = val_dict[output.adapter_name]
            if ';' in valconf.task_name:
                valconf = ValidationConfig(
                        adapter_name=valconf.adapter_name,
                        task_name='arc-c',
                        batch_size = valconf.batch_size)
                if valconf.start_step == -1:
                    valconf.start_step = step_cnt
                config.step()

                if config.accumulation_step_cnt_ % save_step == 0:
                    model.eval()
                    with torch.no_grad():
                        acc = run_validation(model, valconf, dispatcher.tokenizer_)
                        logging.info(f"adapter:{valconf.adapter_name}: validation acc:{acc}")
                    model.train()
                    if acc > best_val[output.adapter_name]:
                        best_val[output.adapter_name] = acc
                        logging.info(f"New best validation acc:{acc}")
                        save_adapter_weight(model, config, save_dir, f"best{step_cnt - valconf.start_step + 1}")
            else:
                if valconf.start_step == -1:
                    valconf.start_step = step_cnt
                config.step()

                if config.accumulation_step_cnt_ % save_step == 0:
                    model.eval()

                    with torch.no_grad():
                        acc = run_validation(model, valconf, dispatcher.tokenizer_)
                        logging.info(f"adapter:{valconf.adapter_name}: validation acc:{acc}")
                    model.train()
                    if acc >= best_val[output.adapter_name]:
                        best_val[output.adapter_name] = acc
                        logging.info(f"New best validation acc:{acc}")
                        save_adapter_weight(model, config, save_dir, f"best{step_cnt - valconf.start_step + 1}", acc)

    for config in configs:
        config.finish()
        save_adapter_weight(model, config, save_dir)
