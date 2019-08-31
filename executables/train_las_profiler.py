import logging
import click
import torch

from sonosco.models.seq2seq_las import Seq2Seq
from sonosco.common.constants import SONOSCO
from sonosco.common.utils import setup_logging
from sonosco.common.path_utils import parse_yaml
from sonosco.training import Experiment, ModelTrainer
from sonosco.datasets import create_data_loaders
from sonosco.decoders import GreedyDecoder
from sonosco.training.word_error_rate import word_error_rate
from sonosco.training.character_error_rate import character_error_rate
from sonosco.training.losses import cross_entropy_loss
from sonosco.training.disable_soft_window_attention import DisableSoftWindowAttention
from sonosco.training.tb_teacher_forcing_text_comparison_callback import TbTeacherForcingTextComparisonCallback
from sonosco.config.global_settings import CUDA_ENABLED
from sonosco.training.las_text_comparison_callback import LasTextComparisonCallback

LOGGER = logging.getLogger(SONOSCO)

EOS = '$'
SOS = '#'
PADDING_VALUE = '%'

cprofile_sortby = 'tottime'
cprofile_topk = 15
autograd_prof_sortby = 'cpu_time_total'
autograd_prof_topk = 15

import argparse
import cProfile
import pstats
import sys
import os

import torch
from torch.autograd import profiler
from torch.utils.collect_env import get_env_info


def redirect_argv(new_argv):
    sys.argv[:] = new_argv[:]


def compiled_with_cuda(sysinfo):
    if sysinfo.cuda_compiled_version:
        return 'compiled w/ CUDA {}'.format(sysinfo.cuda_compiled_version)
    return 'not compiled w/ CUDA'


env_summary = """
--------------------------------------------------------------------------------
  Environment Summary
--------------------------------------------------------------------------------
PyTorch {pytorch_version}{debug_str} {cuda_compiled}
Running with Python {py_version} and {cuda_runtime}

`{pip_version} list` truncated output:
{pip_list_output}
""".strip()


def run_env_analysis():
    print('Running environment analysis...')
    info = get_env_info()

    result = []

    debug_str = ''
    if info.is_debug_build:
        debug_str = ' DEBUG'

    cuda_avail = ''
    if info.is_cuda_available:
        cuda = info.cuda_runtime_version
        if cuda is not None:
            cuda_avail = 'CUDA ' + cuda
    else:
        cuda = 'CUDA unavailable'

    pip_version = info.pip_version
    pip_list_output = info.pip_packages
    if pip_list_output is None:
        pip_list_output = 'Unable to fetch'

    result = {
        'debug_str': debug_str,
        'pytorch_version': info.torch_version,
        'cuda_compiled': compiled_with_cuda(info),
        'py_version': '{}.{}'.format(sys.version_info[0], sys.version_info[1]),
        'cuda_runtime': cuda_avail,
        'pip_version': pip_version,
        'pip_list_output': pip_list_output,
    }

    return env_summary.format(**result)


def run_cprofile(code, launch_blocking=False):
    print('Running your script with cProfile')
    prof = cProfile.Profile()
    prof.enable()
    code()
    prof.disable()
    return prof


cprof_summary = """
--------------------------------------------------------------------------------
  cProfile output
--------------------------------------------------------------------------------
""".strip()


def print_cprofile_summary(prof, sortby='tottime', topk=15):
    result = {}

    print(cprof_summary.format(**result))

    cprofile_stats = pstats.Stats(prof).sort_stats(sortby)
    cprofile_stats.print_stats(topk)


def run_autograd_prof(code):
    def run_prof(use_cuda=False):
        with profiler.profile(use_cuda=use_cuda) as prof:
            code()
        return prof

    print('Running your script with the autograd profiler...')
    result = [run_prof(use_cuda=False)]
    if torch.cuda.is_available():
        result.append(run_prof(use_cuda=True))
    else:
        result.append(None)

    return result


autograd_prof_summary = """
--------------------------------------------------------------------------------
  autograd profiler output ({mode} mode)
--------------------------------------------------------------------------------
        {description}
{cuda_warning}
{output}
""".strip()


def print_autograd_prof_summary(prof, mode, sortby='cpu_time', topk=15):
    valid_sortby = ['cpu_time', 'cuda_time', 'cpu_time_total', 'cuda_time_total', 'count']
    if sortby not in valid_sortby:
        warn = ('WARNING: invalid sorting option for autograd profiler results: {}\n'
                'Expected `cpu_time`, `cpu_time_total`, or `count`. '
                'Defaulting to `cpu_time`.')
        print(warn.format(autograd_prof_sortby))
        sortby = 'cpu_time'

    if mode == 'CUDA':
        cuda_warning = ('\n\tBecause the autograd profiler uses the CUDA event API,\n'
                        '\tthe CUDA time column reports approximately max(cuda_time, cpu_time).\n'
                        '\tPlease ignore this output if your code does not use CUDA.\n')
    else:
        cuda_warning = ''

    sorted_events = sorted(prof.function_events,
                           key=lambda x: getattr(x, sortby), reverse=True)
    topk_events = sorted_events[:topk]

    result = {
        'mode': mode,
        'description': 'top {} events sorted by {}'.format(topk, sortby),
        'output': torch.autograd.profiler.build_table(topk_events),
        'cuda_warning': cuda_warning
    }

    print(autograd_prof_summary.format(**result))


descript = """
`bottleneck` is a tool that can be used as an initial step for debugging
bottlenecks in your program.

It summarizes runs of your script with the Python profiler and PyTorch\'s
autograd profiler. Because your script will be profiled, please ensure that it
exits in a finite amount of time.

For more complicated uses of the profilers, please see
https://docs.python.org/3/library/profile.html and
https://pytorch.org/docs/master/autograd.html#profiler for more information.
""".strip()


def parse_args():
    parser = argparse.ArgumentParser(description=descript)
    parser.add_argument('scriptfile', type=str,
                        help='Path to the script to be run. '
                             'Usually run with `python path/to/script`.')
    parser.add_argument('args', type=str, nargs=argparse.REMAINDER,
                        help='Command-line arguments to be passed to the script.')
    return parser.parse_args()


def cpu_time_total(autograd_prof):
    return sum([event.cpu_time_total for event in autograd_prof.function_events])


def train(config_path="../sonosco/config/train_seq2seq_las.yaml"):
    config = parse_yaml(config_path)["train"]
    experiment = Experiment.create(config, LOGGER)

    device = torch.device("cuda" if CUDA_ENABLED else "cpu")

    char_list = config["labels"] + EOS + SOS

    config["decoder"]["vocab_size"] = len(char_list)
    config["decoder"]["sos_id"] = char_list.index(SOS)
    config["decoder"]["eos_id"] = char_list.index(EOS)

    # Create model
    model = Seq2Seq(config["encoder"], config["decoder"])
    model.to(device)

    # Create data loaders
    train_loader, val_loader, test_loader = create_data_loaders(**config)

    # Create model trainer
    trainer = ModelTrainer(model, loss=cross_entropy_loss, epochs=config["max_epochs"],
                           train_data_loader=train_loader, val_data_loader=val_loader,
                           test_data_loader=test_loader,
                           lr=config["learning_rate"], weight_decay=config['weight_decay'],
                           metrics=[word_error_rate, character_error_rate],
                           decoder=GreedyDecoder(config['labels']),
                           device=device, test_step=config["test_step"], custom_model_eval=True)

    trainer.add_callback(LasTextComparisonCallback(labels=char_list,
                                                   log_dir=experiment.plots_path,
                                                   args=config['recognizer']))
    trainer.add_callback(TbTeacherForcingTextComparisonCallback(log_dir=experiment.plots_path))
    trainer.add_callback(DisableSoftWindowAttention())

    # Setup experiment with a model trainer
    experiment.setup_model_trainer(trainer, checkpoints=True, tensorboard=True)

    # try:
    experiment.start()
    # except KeyboardInterrupt:
    #     experiment.stop()


def main():
    # Customizable constants.
    cprofile_sortby = 'tottime'
    cprofile_topk = 15
    autograd_prof_sortby = 'cpu_time_total'
    autograd_prof_topk = 15

    code = train
    #
    # globs = {
    #     '__name__': '__main__',
    #     '__package__': None,
    #     '__cached__': None,
    # }

    print(descript)

    env_summary = run_env_analysis()

    if torch.cuda.is_available():
        torch.cuda.init()
    cprofile_prof = run_cprofile(code)
    autograd_prof_cpu, autograd_prof_cuda = run_autograd_prof(code)

    print(env_summary)
    print_cprofile_summary(cprofile_prof, cprofile_sortby, cprofile_topk)

    if not torch.cuda.is_available():
        print_autograd_prof_summary(autograd_prof_cpu, 'CPU', autograd_prof_sortby, autograd_prof_topk)
        return

    # Print both the result of the CPU-mode and CUDA-mode autograd profilers
    # if their execution times are very different.
    cuda_prof_exec_time = cpu_time_total(autograd_prof_cuda)
    if len(autograd_prof_cpu.function_events) > 0:
        cpu_prof_exec_time = cpu_time_total(autograd_prof_cpu)
        pct_diff = (cuda_prof_exec_time - cpu_prof_exec_time) / cuda_prof_exec_time
        if abs(pct_diff) > 0.05:
            print_autograd_prof_summary(autograd_prof_cpu, 'CPU', autograd_prof_sortby, autograd_prof_topk)

    print_autograd_prof_summary(autograd_prof_cuda, 'CUDA', autograd_prof_sortby, autograd_prof_topk)


if __name__ == '__main__':
    main()