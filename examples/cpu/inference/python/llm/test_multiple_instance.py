import torch
import time
import json
import pathlib
import argparse
import psutil
import re
import numpy as np 

from torch.nn.functional import pad
from datasets import load_dataset
from torch.utils.data import DataLoader

from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    LlamaForCausalLM,
    AutoTokenizer,
    LlamaTokenizer,
)


# supported models
MODEL_CLASSES = {
    "gpt-j": (AutoModelForCausalLM, AutoTokenizer),
    "gpt-neox": (AutoModelForCausalLM, AutoTokenizer),
    "llama": (AutoModelForCausalLM, LlamaTokenizer),
    "opt": (AutoModelForCausalLM, AutoTokenizer),
    "falcon": (AutoModelForCausalLM, AutoTokenizer),
    "bloom": (AutoModelForCausalLM, AutoTokenizer),
    "chatglm": (AutoModelForCausalLM, AutoTokenizer),
    "codegen": (AutoModelForCausalLM, AutoTokenizer),
    "gptbigcode": (AutoModelForCausalLM, AutoTokenizer),
    "auto": (AutoModelForCausalLM, AutoTokenizer),
}

# args
parser = argparse.ArgumentParser("Generation script (fp32/bf16 path)", add_help=False)
parser.add_argument(
    "-m",
    "--model-id",
    type=str,
    default="EleutherAI/gpt-j-6B",
    help="the huggingface mdoel id",
)
parser.add_argument(
    "--device",
    type=str,
    choices=["cpu"],
    default="cpu",
    help="cpu",
)
parser.add_argument(
    "--dtype",
    type=str,
    choices=["float32", "bfloat16"],
    default="bfloat16",
    help="bfloat16, float32",
)
parser.add_argument(
    "--input-tokens",
    default="32",
    type=str,
    help="input tokens length if needed from prompt.json",
)
parser.add_argument(
    "--max-new-tokens", default=32, type=int, help="output max new tokens"
)
parser.add_argument(
    "--prompt", default=None, type=str, help="input prompt for self-defined if needed"
)
parser.add_argument(
    "--config-file", default=None, type=str, help="specific configuration file"
)
parser.add_argument("--greedy", action="store_true")
parser.add_argument("--ipex", action="store_true")
parser.add_argument("--jit", action="store_true")
parser.add_argument("--profile", action="store_true")
parser.add_argument("--benchmark", action="store_true")
parser.add_argument("--lambada", action="store_true")
parser.add_argument("--dataset", default="lambada", type=str)
parser.add_argument("--accuracy-only", action="store_true")
parser.add_argument("--num-iter", default=100, type=int, help="num iter")
parser.add_argument("--num-warmup", default=10, type=int, help="num warmup")
parser.add_argument("--batch-size", default=1, type=int, help="batch size")
parser.add_argument(
    "--token-latency", action="store_true", help="get token latency breakdown"
)
parser.add_argument(
    "--instance-number", default=1, type=int, help="The number of instance keep in DDR/HBM memory. Default is 1."
)
parser.add_argument(
    "--kv-cache-dtype",
    type=str,
    choices=["float8_e5m2", "None"],
    default="None",
    help="Specify the kv_cache data type, you can use float8_e5m2 to reduce kv_cache memory footprint but may slightly drop the accuracy.",
)
parser.add_argument(
    "--request-number", default=10, type=int, help="The number of instance keep in DDR/HBM memory. Default is 1."
)
parser.add_argument("--print-memory-usage", action="store_true")
args = parser.parse_args()
print(args)

# device
device = torch.device(args.device)


def print_memory_usage(message):
    process = psutil.Process()
    print(
        "{} Memory usage with {} model instances: {:.3f} GB".format(
            message, args.instance_number, process.memory_info().rss / 1024 / 1024 / 1024
        )
    )


if not args.ipex or not args.jit:
    print("Please use --ipex and --jit to re-run this script, aborting...")
    exit(0)

# import ipex
if args.ipex:
    import intel_extension_for_pytorch as ipex

    try:
        ipex._C.disable_jit_linear_repack()
    except Exception:
        pass

if args.jit:
    torch._C._jit_set_texpr_fuser_enabled(False)

# dtype
amp_enabled = True if args.dtype != "float32" else False
amp_dtype = getattr(torch, args.dtype)

model_list = []
tokenizer_list = []
# load model
model_type = next(
    (x for x in MODEL_CLASSES.keys() if x in args.model_id.lower()), "auto"
)
model_class = MODEL_CLASSES[model_type]
if args.config_file is None:
    config = AutoConfig.from_pretrained(
        args.model_id, torchscript=args.jit, trust_remote_code=True
    )
else:
    config = AutoConfig.from_pretrained(
        args.config_file, torchscript=args.jit, trust_remote_code=True
    )

if args.kv_cache_dtype != "None":
    args.kv_cache_dtype = getattr(torch, args.kv_cache_dtype)
    print("kv_cache_dtype:", args.kv_cache_dtype)
    config.kv_cache_dtype = args.kv_cache_dtype

if not hasattr(config, "text_max_length") and args.prompt is None:
    config.text_max_length = int(args.input_tokens) + int(args.max_new_tokens)

model = None 
model = model_class[0].from_pretrained(
    args.model_id,
    torch_dtype=amp_dtype,
    config=config,
    low_cpu_mem_usage=True,
    trust_remote_code=True,
)
tokenizer = model_class[1].from_pretrained(args.model_id, trust_remote_code=True)
model = model.eval().to(device)
model = model.to(memory_format=torch.channels_last)    
# to ipex
if args.ipex:
    model = ipex._optimize_transformers(model.eval(), dtype=amp_dtype, inplace=True)
            
if re.search("gptbigcode", model.config.architectures[0], re.IGNORECASE):
    model_type = "gptbigcode"
num_beams = 1 if args.greedy else 4
# generate args
generate_kwargs = dict(
    do_sample=False,
    temperature=0.9,
    num_beams=num_beams,
    max_new_tokens=args.max_new_tokens,
    min_new_tokens=args.max_new_tokens,
)

# dummy past key values
past_key_values = None

if (
    re.search("GPTJ", model.config.architectures[0])
    or re.search("codegen", model.config.architectures[0], re.IGNORECASE)
    or re.search("gptbigcode", model.config.architectures[0], re.IGNORECASE)
):
    beam_idx_tmp = torch.zeros(
        (2048, int(args.batch_size * num_beams)), dtype=torch.long
    ).contiguous()
    past_key_values = tuple(
        [
            (
                torch.zeros([1, 1, 1, 1]).contiguous()
                if args.kv_cache_dtype == "None"
                else torch.zeros([1, 1, 1, 1], dtype=args.kv_cache_dtype).contiguous(),
                torch.zeros([1, 1, 1, 1]).contiguous()
                if args.kv_cache_dtype == "None"
                else torch.zeros([1, 1, 1, 1], dtype=args.kv_cache_dtype).contiguous(),
                beam_idx_tmp,
                torch.zeros(1, dtype=torch.long).contiguous(),
            )
            for i in range(model.config.n_layer)
        ]
    )
elif re.search("llama", model.config.architectures[0], re.IGNORECASE):
    beam_idx_tmp = torch.zeros(
        (2048, int(args.batch_size * num_beams)), dtype=torch.long
    ).contiguous()
    past_key_values = tuple(
        [
            (
                torch.zeros([1, 1, 1, 1]).contiguous()
                if args.kv_cache_dtype == "None"
                else torch.zeros([1, 1, 1, 1], dtype=args.kv_cache_dtype).contiguous(),
                torch.zeros([1, 1, 1, 1]).contiguous()
                if args.kv_cache_dtype == "None"
                else torch.zeros([1, 1, 1, 1], dtype=args.kv_cache_dtype).contiguous(),
                beam_idx_tmp,
                torch.zeros(1, dtype=torch.long).contiguous(),
            )
            for i in range(model.config.num_hidden_layers)
        ]
    )
elif re.search("gptneox", model.config.architectures[0], re.IGNORECASE):
    beam_idx_tmp = torch.zeros(
        (2048, int(args.batch_size * num_beams)), dtype=torch.long
    ).contiguous()
    past_key_values = tuple(
        [
            (
                torch.zeros([1, 1, 1, 1]).contiguous()
                if args.kv_cache_dtype == "None"
                else torch.zeros([1, 1, 1, 1], dtype=args.kv_cache_dtype).contiguous(),
                torch.zeros([1, 1, 1, 1]).contiguous()
                if args.kv_cache_dtype == "None"
                else torch.zeros([1, 1, 1, 1], dtype=args.kv_cache_dtype).contiguous(),
                beam_idx_tmp,
                torch.zeros(1, dtype=torch.long).contiguous(),
            )
            for i in range(model.config.num_hidden_layers)
        ]
    )
elif re.search("opt", model.config.architectures[0], re.IGNORECASE):
    beam_idx_tmp = torch.zeros(
        (2048, int(args.batch_size * num_beams)), dtype=torch.long
    ).contiguous()
    past_key_values = tuple(
        [
            (
                torch.zeros([1, 1, 1, 1]).contiguous()
                if args.kv_cache_dtype == "None"
                else torch.zeros([1, 1, 1, 1], dtype=args.kv_cache_dtype).contiguous(),
                torch.zeros([1, 1, 1, 1]).contiguous()
                if args.kv_cache_dtype == "None"
                else torch.zeros([1, 1, 1, 1], dtype=args.kv_cache_dtype).contiguous(),
                beam_idx_tmp,
                torch.zeros(1, dtype=torch.long).contiguous(),
            )
            for i in range(model.config.num_hidden_layers)
        ]
    )
    target_max_position_embeddings = int(args.input_tokens) + int(args.max_new_tokens)
    if target_max_position_embeddings >= 2048:
        orig_embed_weight = model.model.decoder.embed_positions.weight
        num_embeddings, embedding_dim = orig_embed_weight.shape
        padding_weight = torch.rand(
            target_max_position_embeddings - 2048, embedding_dim
        ).to(orig_embed_weight.dtype)
        new_embed_weight = torch.cat((orig_embed_weight, padding_weight), 0)
        model.model.decoder.embed_positions.weight = torch.nn.Parameter(
            new_embed_weight
        )
elif re.search("falcon", model.config.architectures[0], re.IGNORECASE) or re.search(
    "rw", model.config.architectures[0], re.IGNORECASE
):
    beam_idx_tmp = torch.zeros(
        (2048, int(args.batch_size * num_beams)), dtype=torch.long
    ).contiguous()
    past_key_values = tuple(
        [
            (
                torch.zeros([1, 1, 1, 1]).contiguous()
                if args.kv_cache_dtype == "None"
                else torch.zeros([1, 1, 1, 1], dtype=args.kv_cache_dtype).contiguous(),
                torch.zeros([1, 1, 1, 1]).contiguous()
                if args.kv_cache_dtype == "None"
                else torch.zeros([1, 1, 1, 1], dtype=args.kv_cache_dtype).contiguous(),
                beam_idx_tmp,
                torch.zeros(1, dtype=torch.long).contiguous(),
            )
            for i in range(model.config.num_hidden_layers)
        ]
    )
elif re.search("bloom", model.config.architectures[0], re.IGNORECASE):
    beam_idx_tmp = torch.zeros(
        (2048, int(args.batch_size * num_beams)), dtype=torch.long
    ).contiguous()
    past_key_values = tuple(
        [
            (
                torch.zeros([1, 1, 1, 1]).contiguous()
                if args.kv_cache_dtype == "None"
                else torch.zeros([1, 1, 1, 1], dtype=args.kv_cache_dtype).contiguous(),
                torch.zeros([1, 1, 1, 1]).contiguous()
                if args.kv_cache_dtype == "None"
                else torch.zeros([1, 1, 1, 1], dtype=args.kv_cache_dtype).contiguous(),
                beam_idx_tmp,
                torch.zeros(1, dtype=torch.long).contiguous(),
            )
            for i in range(model.config.n_layer)
        ]
    )
elif re.search("chatglm", model.config.architectures[0], re.IGNORECASE):
    beam_idx_tmp = torch.zeros(
        (2048, int(args.batch_size * num_beams)), dtype=torch.long
    ).contiguous()
    past_key_values = tuple(
        [
            (
                torch.zeros([1, 1, 1, 1]).contiguous()
                if args.kv_cache_dtype == "None"
                else torch.zeros([1, 1, 1, 1], dtype=args.kv_cache_dtype).contiguous(),
                torch.zeros([1, 1, 1, 1]).contiguous()
                if args.kv_cache_dtype == "None"
                else torch.zeros([1, 1, 1, 1], dtype=args.kv_cache_dtype).contiguous(),
                beam_idx_tmp,
                torch.zeros(1, dtype=torch.long).contiguous(),
            )
            for i in range(model.config.num_layers)
        ]
    )
else:
    print(
        "Currently we only support jit path on GPTJ, llama, gpt_neox, OPT, falcon, Bloom, chatGLM and Codegen models for IPEX new API ipex._optimize_transformers(), please re-run without jit "
    )
    exit(0)


if not hasattr(model, "trace_graph") and args.jit and args.benchmark and args.ipex:
    example_inputs = None
    input_ids = torch.ones(32).to(torch.long)
    attention_mask = torch.ones(len(input_ids))
    position_ids = torch.arange(len(input_ids))
    if (
        re.search("opt", model.config.architectures[0], re.IGNORECASE)
        or re.search("falcon", model.config.architectures[0], re.IGNORECASE)
        or re.search("rw", model.config.architectures[0], re.IGNORECASE)
        or re.search("bloom", model.config.architectures[0], re.IGNORECASE)
    ):
        example_inputs = {
            "input_ids": input_ids.unsqueeze(0),
            "attention_mask": attention_mask.unsqueeze(0),
            "past_key_values": past_key_values,
        }
    else:
        example_inputs = {
            "input_ids": input_ids.unsqueeze(0),
            "attention_mask": attention_mask.unsqueeze(0),
            "position_ids": position_ids.unsqueeze(0),
            "past_key_values": past_key_values,
        }
    if re.search("chatglm", model.config.architectures[0], re.IGNORECASE):
        example_inputs["return_last_logit"] = torch.tensor(True)

    with torch.inference_mode(), torch.no_grad(), torch.autocast(
        device_type=args.device,
        enabled=amp_enabled,
        dtype=amp_dtype if amp_enabled else None,
    ):
        trace_model = torch.jit.trace(
            model, example_kwarg_inputs=example_inputs, strict=False, check_trace=False
        )
        trace_model = torch.jit.freeze(trace_model)
        setattr(model, "trace_graph", trace_model)

import copy 
model_list.append(model)
tokenizer_list.append(tokenizer)
for i in range(args.instance_number - 1):
    model_list.append(copy.deepcopy(model))
    tokenizer_list.append(copy.deepcopy(tokenizer))

if args.print_memory_usage:
    print_memory_usage("There are {} model instances in memory. Berfore Benchmark".format(args.instance_number))
    
for i in range(args.request_number):
        model_id = np.random.randint(0, args.instance_number)
        model = model_list[model_id]
        if args.benchmark:
            if args.token_latency:
                if not hasattr(model.config, "token_latency"):
                    model.config.token_latency = True
            # input prompt
            current_path = pathlib.Path(__file__).parent.resolve()
            with open(str(current_path) + "/prompt.json") as f:
                prompt_pool = json.load(f)
            if args.prompt is not None:
                prompt = args.prompt
            elif model_type == "auto":
                raise SystemExit(
                    "[ERROR] model prompt is not supported, please use --prompt for this model: "
                    + args.model_id
                )
            elif int(args.input_tokens) > 8192:
                prompt = prompt_pool[model_type]["8192"] * int(int(args.input_tokens) / 8192)
            elif args.input_tokens in prompt_pool[model_type]:
                prompt = prompt_pool[model_type][args.input_tokens]
            else:
                raise SystemExit("[ERROR] Plese use --prompt if want to use custom input.")

            input_size = tokenizer(prompt, return_tensors="pt").input_ids.size(dim=1)
            print("---- Prompt size:", input_size)

            # start
            total_time = 0.0
            num_iter = args.num_iter
            num_warmup = args.num_warmup
            prompt = [prompt] * args.batch_size
            total_list = []
            with torch.inference_mode(), torch.no_grad(), torch.autocast(
                device_type=args.device,
                enabled=amp_enabled,
                dtype=amp_dtype if amp_enabled else None,
            ):
                for i in range(num_iter):
                    tic = time.time()
                    input_ids = tokenizer(prompt, return_tensors="pt").input_ids.to(device)
                    output = model.generate(input_ids, **generate_kwargs)
                    if args.print_memory_usage:
                        print_memory_usage("Model_{}".format(model_id) + " After iteration {} ".format(i))
                    gen_ids = output[0] if args.token_latency else output
                    gen_text = tokenizer.batch_decode(gen_ids, skip_special_tokens=True)
                    toc = time.time()
                    input_tokens_lengths = [x.shape[0] for x in input_ids]
                    output_tokens_lengths = [x.shape[0] for x in gen_ids]
                    total_new_tokens = [
                        o - i if model.config.model_type != "t5" else o
                        for i, o in zip(input_tokens_lengths, output_tokens_lengths)
                    ]
                    #print(gen_text, total_new_tokens, flush=True)
                    print("Model_{}".format(model_id), "Iteration: %d, Time: %.6f sec" % (i, toc - tic), flush=True)
                    if i >= num_warmup:
                        total_time += toc - tic
                        if args.token_latency:
                            total_list.append(output[1])

            print("\n", "-" * 10, "Model_{}".format(model_id), "Summary:", "-" * 10)
            latency = total_time / (num_iter - num_warmup)
            print("Model_{}".format(model_id), " Inference latency: %.3f sec." % latency)

            if args.token_latency:
                import numpy as np
                from itertools import chain

                first_latency = np.mean([x[0] for x in total_list])
                average_2n = list(chain(*[x[1:] for x in total_list]))
                average_2n.sort()
                average_2n_latency = np.mean(average_2n)
                p90_latency = average_2n[int(len(average_2n) * 0.9)]
                p99_latency = average_2n[int(len(average_2n) * 0.99)]
                print("Model_{} ".format(model_id), "First token average latency: %.4f sec." % first_latency)
                print("Model_{} ".format(model_id), "Average 2... latency: %.4f sec." % average_2n_latency)
                print("Model_{} ".format(model_id), "P90 2... latency: %.4f sec." % p90_latency)
                print("Model_{} ".format(model_id), "P99 2... latency: %.4f sec." % p99_latency)
