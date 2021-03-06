import os
import argparse

import tvm
from tvm import relay, autotvm
from tvm.autotvm.tuner import XGBTuner, GATuner, RandomTuner, GridSearchTuner
from tvm.autotvm.graph_tuner import DPTuner, PBQPTuner

from util import get_network


def autotvm_tune(network, target, input_name, log_prefix):
    graph_log = log_prefix + "_graph.log"
    kernel_log = log_prefix + "_kernel.log"
    if os.path.exists(kernel_log):
        os.remove(kernel_log)
    if os.path.exists(graph_log):
        os.remove(graph_log)
    mod, params, input_shape, output_shape = get_network(network)

    if network in ["bert"]:
        tuning_opt = autotvm_tuning_opt(target, kernel_log)
        tasks = autotvm.task.extract_from_program(
            mod["main"], target=target,
            params=params, ops=(relay.op.get("nn.batch_matmul"), relay.op.get("nn.dense")))
        tune_kernels(tasks, **tuning_opt)
    else:
        # covert to NCHW
        desired_layouts = {'nn.conv2d': ['NCHW', 'default']}
        seq = tvm.transform.Sequential([relay.transform.RemoveUnusedFunctions(),
                                        relay.transform.ConvertLayout(desired_layouts)])
        with tvm.transform.PassContext(opt_level=3):
            mod = seq(mod)

        tuning_opt = autotvm_tuning_opt(target, kernel_log)
        tasks = autotvm.task.extract_from_program(
                mod["main"], target=target,
                params=params, ops=(relay.op.get("nn.conv2d"),)
        )
        tune_kernels(tasks, **tuning_opt)
        if "cpu" in target.keys:
            tune_graph(mod["main"], input_shape, kernel_log,
                    graph_log, target, input_name)


def autotvm_tuning_opt(target, log_file, dtype = "float32"):
    if "cpu" in target.keys:
        print("enable cpu tuning options")
        measure_option = autotvm.measure_option(
            builder=autotvm.LocalBuilder(),
            runner=autotvm.LocalRunner(
                number=1, repeat=10, min_repeat_ms=0, enable_cpu_cache_flush=True
            ),
        )
    else:
        print("enable gpu tuning options")
        measure_option = autotvm.measure_option(
                builder=autotvm.LocalBuilder(timeout=10),
                runner=autotvm.LocalRunner(number=20, repeat=3, timeout=4, min_repeat_ms=150),
            )

    tuning_option = {
        "log_filename": log_file,
        "tuner": "xgb",
        "early_stopping": None,
        "measure_option": measure_option
    }
    return tuning_option

def tune_kernels(
    tasks,
    measure_option,
    tuner="xgb",
    n_trial=1000,
    early_stopping=None,
    log_filename="tuning.log"
):
    for i, tsk in enumerate(reversed(tasks)):
        prefix = "[Task %2d/%2d] " % (i + 1, len(tasks))
        # create tuner
        if tuner == "xgb" or tuner == "xgb-rank":
            tuner_obj = XGBTuner(tsk, loss_type="rank")
        elif tuner == "ga":
            tuner_obj = GATuner(tsk, pop_size=100)
        elif tuner == "random":
            tuner_obj = RandomTuner(tsk)
        elif tuner == "gridsearch":
            tuner_obj = GridSearchTuner(tsk)
        else:
            raise ValueError("Invalid tuner: " + tuner)

        # do tuning
        tsk_trial = min(n_trial, len(tsk.config_space))
        tuner_obj.tune(
            n_trial=tsk_trial,
            early_stopping=early_stopping,
            measure_option=measure_option,
            callbacks=[
                autotvm.callback.progress_bar(tsk_trial, prefix=prefix),
                autotvm.callback.log_to_file(log_filename),
            ],
        )

# Use graph tuner to achieve graph level optimal schedules
# Set use_DP=False if it takes too long to finish.
def tune_graph(graph, dshape, records, opt_sch_file, target, input_name, use_DP=True):
    target_op = [
        relay.op.get("nn.conv2d"),
    ]
    Tuner = DPTuner if use_DP else PBQPTuner
    executor = Tuner(graph, {input_name: dshape}, records, target_op, target)
    executor.benchmark_layout_transform(min_exec_num=2000)
    executor.run()
    executor.write_opt_sch2record_file(opt_sch_file)



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--network",
        type=str,
        choices=[
            "resnet-50",
            "mobilenet_v2",
            "bert",
            "all"
        ],
        help="The name of neural network",
    )
    parser.add_argument(
        "--target",
        type=str,
        default="llvm -model=e5-2670 -mcpu=core-avx2",
        help="The tvm compilation target",
    )
    parser.add_argument("--logdir", type=str, default="tuning_logs/", help="Log file directory.")
    parser.add_argument("--thread", type=int, default=1, help="The number of threads to be run.")
    parser.add_argument("--inputname", type=str, default="data",
                        help="Input name of the graph. For ONNX models, it is typically 0")

    args = parser.parse_args()
    dtype = "float32"

    if args.network is None or args.network == "all":
        networks = ["resnet-50", "mobilenet_v2", "bert"]
    else:
        networks = [args.network]

    if not os.path.exists(args.logdir):
        os.mkdir(args.logdir)

    target = tvm.target.Target(args.target)

    if "cpu" in target.keys:
        target_name = "cpu"
    else:
        target_name = "cuda"
    for network in networks:
        log_prefix = os.path.join(args.logdir, "autotvm_" + target_name + "_" + network)
        autotvm_tune(network, target, args.inputname, log_prefix)
