import logging
import torch
import torch.nn
import torch.optim.optimizer
import click
import math
import os
import json

from dataclasses import field, dataclass
from torch.utils.data import RandomSampler
from collections import defaultdict


from typing import Callable, Union, Tuple, List, Any

from sonosco.decoders.decoder import Decoder
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

LOGGER = logging.getLogger(__name__)


@click.command()
@click.option("-c", "--config_path", default="../sonosco/config/train_seq2seq_tds.yaml",
              type=click.STRING, help="Path to train configurations.")


@dataclass
class ModelEvaluator:
    model: torch.nn.Module
    data_loader: DataLoader
    bootstrap_size: int
    num_bootstraps: int
    decoder: Decoder = None
    metrics: List[Callable[[torch.Tensor, Any], Union[float, torch.Tensor]]] = field(default_factory=list)
    _current_bootstrap_step: int = None
    _eval_dict: dict = field(default_factory=dict)

    def __post_init__(self):
        self._setup_replacement_sampler_in_dataloader()
        self._evaluation_done = False

    def _setup_replacement_sampler_in_dataloader(self):
        self.data_loader.batch_sampler = None
        random_sampler = RandomSampler(data_source=self.data_loader.dataset, replacement=True,
                                       num_samples=self.bootstrap_size)
        self.data_loader.sampler = random_sampler

    def _bootstrap_step(self, mean_dict):
        torch.no_grad()
        running_metrics = {metric.__name__: [] for metric in self.metrics}

        for sample_step in range(self.bootstrap_size):
            batch_x, batch_y, input_lengths, target_lengths = next(iter(self.data_loader))
            batch = (batch_x, batch_y, input_lengths, target_lengths)
            batch = self._recursive_to_cuda(batch)  # move to GPU
            loss, model_output, grad_norm = self._train_on_batch(batch)
            self._compute_running_metrics(model_output, batch, running_metrics)

        self._fill_mean_dict(running_metrics, mean_dict)

    def _compute_running_metrics(self,
                                 model_output: torch.Tensor,
                                 batch: Tuple[torch.Tensor, torch.Tensor],
                                 running_metrics: dict):
        """
        Computes all metrics based on predictions and batches and adds them to the metrics
        dictionary. Allows to prepend a prefix to the metric names in the dictionary.
        """
        for metric in self.metrics:
            if metric.__name__ == 'word_error_rate' or metric.__name__ == 'character_error_rate':
                metric_result = metric(model_output, batch, self.decoder)
            else:
                metric_result = metric(model_output, batch)
            if type(metric_result) == torch.Tensor:
                metric_result = metric_result.item()

            running_metrics[metric.__name__].append(metric_result)

    def _fill_mean_dict(self, running_metrics, mean_dict):
        for key, value in running_metrics.items():
            mean = sum(value) / len(value)
            mean_dict[key].append(mean)

    def _compute_mean_variance(self):
        self.eval_dict = defaultdict()
        for key, value in self._mean_dict:
            tmp_mean = sum(value)/len(value)
            tmp_variance = math.sqrt(sum([(mean - tmp_mean)**2 for mean in value])/ len(value)-1)
            self.eval_dict[key + '_mean'] = tmp_mean
            self.eval_dict[key + '_variance'] = tmp_variance

    def set_metrics(self, metrics):
        """
        Set metric functions that receive y_pred and y_true. These metrics are used to
        create a statistical evaluation of the model provided
        """
        self.metrics = metrics

    def add_metric(self, metric):
        self.metrics.append(metric)

    def start_evaluation(self):
        self.model.eval() #evaluation mode
        mean_dict = {metric.__name__: [] for metric in self.metrics}

        for bootstrap_step in range(self.num_bootstraps):
            self._current_bootstrap_step = bootstrap_step
            self._bootstrap_step(mean_dict)
        self._compute_mean_variance(mean_dict)
        self._evaluation_done = True

    def dump_evaluation(self, output_path):
        if self._evaluation_done == False:
            LOGGER.info(f'Evaluation was not done yet. Starting evaluation')
            self.start_evaluation()
            file_to_dump = os.path.join(output_path, 'evaluation.json')
            with open(file_to_dump, 'w') as fp:
                json.dump(self.eval_dict, fp)

    def dump_to_tensorboard(self, log_path):
        writer = SummaryWriter(log_dir=log_path)
        for key, value in self.eval_dict:
            writer.add_scalar(key, value)




