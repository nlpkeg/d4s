import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from ..rome.layer_stats import layer_stats
from ...util import nethook
from ...util.generate import generate_fast
from ...util.globals import *

from .compute_ks import compute_ks
from .compute_z import compute_z, get_module_input_output_at_words, find_fact_lookup_idx, get_cov
from .memit_hparams import MEMITHyperParams

# Cache variable(s)
CONTEXT_TEMPLATES_CACHE = None
cache_kvs = {}


def get_cache(name):
    global cache_kvs

    return cache_kvs[name]


def upd_cache(name, w):
    global cache_kvs

    if name in cache_kvs:
        with torch.no_grad():
            cache_kvs[name] = cache_kvs[name] + w.to(cache_kvs[name].device)
    else:
        with torch.no_grad():
            cache_kvs[name] = w


def apply_memit_to_model(
        model: AutoModelForCausalLM,
        tok: AutoTokenizer,
        requests: List[Dict],
        hparams: MEMITHyperParams,
        copy=False,
        return_orig_weights=False,
        cache_template: Optional[str] = None,
        keep_original_weight=False,
        **kwargs
):
    """
    Returns a model with the desired changes.
    :param copy: If true, will preserve the original model while creating a new one to edit.
        Note that you are responsible for deallocating the new model's memory to avoid leaks.
    :return: (1) the updated model, (2) an original copy of the weights that changed
    """

    weights_copy = {}
    if copy:
        model = deepcopy(model)

    deltas, prob_list = execute_memit(model, tok, requests, hparams, cache_template=cache_template)
    delta_nom = {}
    with torch.no_grad():
        for w_name, upd_matrix in deltas.items():
            w = nethook.get_parameter(model, w_name)
            upd_matrix = upd_matrix_match_shape(upd_matrix, w.shape)

            delta_nom[w_name] = torch.norm(upd_matrix, p=1).item() / upd_matrix.numel()
            if return_orig_weights and w_name not in weights_copy:
                weights_copy[w_name] = w.detach().clone()
            w[...] += upd_matrix.to(w.device).float()

    print(f"New weights successfully inserted into {list(deltas.keys())}")

    if not keep_original_weight:
        weights_copy = {}

    return model, weights_copy, delta_nom, prob_list


def execute_memit(
        model: AutoModelForCausalLM,
        tok: AutoTokenizer,
        requests: List[Dict],
        hparams: MEMITHyperParams,
        cache_template: Optional[str] = None,
):
    """
    Executes the MEMIT update algorithm for the specified update at the specified layer
    Invariant: model at beginning of function == model at end of function
    """

    deltas = {}

    # Update target and print info
    requests = deepcopy(requests)
    for i, request in enumerate(requests):
        if request["target_new"][0] != " ":
            # Space required for correct tokenization
            requests[i]["target_new"] = " " + request["target_new"]

        if '{}' not in request['prompt']:
            assert request['subject'] in request['prompt'] or \
                   print(f"Subject:{request['subject']} do not exist in prompt: {request['prompt']}")

            requests[i]['prompt'] = requests[i]['prompt'].replace(requests[i]['subject'], '{}')

    for request in requests[:10]:
        print(
            f"MEMIT request sample: "
            f"[{request['prompt'].format(request['subject'])}] -> [{request['target_new']}]"
        )

    # Retrieve weights that user desires to change
    weights = {
        f"{hparams.rewrite_module_tmp.format(layer)}.weight": nethook.get_parameter(
            model, f"{hparams.rewrite_module_tmp.format(layer)}.weight"
        )
        for layer in hparams.layers
    }
    # Save old weights for future restoration
    weights_copy = {k: v.detach().clone() for k, v in weights.items()}

    # Compute z for final layer
    context_templates = get_context_templates(model, tok)
    z_layer = hparams.layers[-1]
    z_list = []
    prob_list = []
    for request in requests:
        # Retrieve k/v pair if already stored in cache
        cache_fname = (
            Path(
                str(cache_template).format(
                    z_layer, hparams.clamp_norm_factor, request["case_id"]
                )
            )
            if cache_template is not None
            else None
        )
        data_loaded = False
        if (
                cache_fname is not None  # Require cache template
                and cache_fname.exists()  # Cache file must exist
        ):
            try:
                data = np.load(cache_fname)
                z_list.append(torch.from_numpy(data["v_star"]).to(f"cuda:{hparams.device}"))
                data_loaded = True
            except Exception as e:
                print(f"Error reading cache file due to {e}. Recomputing...")

        # Compute k/v pair if not loaded from cache
        if not data_loaded:
            cur_z, prob = compute_z(
                model,
                tok,
                request,
                hparams,
                z_layer,
                context_templates,
            )
            prob_list.append(prob)
            z_list.append(cur_z)

            if cache_fname is not None:
                cache_fname.parent.mkdir(exist_ok=True, parents=True)
                np.savez(
                    cache_fname,
                    **{
                        "v_star": cur_z.detach().cpu().numpy(),
                    },
                )
                print(f"Cached k/v pair at {cache_fname}")
    zs = torch.stack(z_list, dim=1)
    # hidden_dim * batch_size

    # Compute residual error
    cur_zs = get_module_input_output_at_words(
        model,
        tok,
        z_layer,
        context_templates=[request["prompt"] for request in requests],
        words=[request["subject"] for request in requests],
        module_template=hparams.layer_module_tmp,
        fact_token_strategy=hparams.fact_token,
        track='out'
    ).T
    targets = zs - cur_zs.to(zs.device)

    # Insert
    for i, layer in enumerate(hparams.layers):
        weight_name = f"{hparams.rewrite_module_tmp.format(layer)}.weight"
        print(f"\n\nLAYER {layer}\n")

        # Get current model activations
        layer_ks = compute_ks(model, tok, requests, hparams, layer, context_templates).T
        # hidden_dim * batch_size
        print(f"Writing {layer_ks.size(1)} key/value pair(s) into layer {layer}")

        print("z error", torch.linalg.norm(targets, dim=0).mean())

        repeat_factor = (layer_ks.size(1) // targets.size(1))
        layer_ks, targets = (
            layer_ks.double(),
            targets.double(),
        )
        targets = targets.repeat_interleave(repeat_factor, dim=1)
        r_cache = targets @ layer_ks.T.to(targets.device)
        upd_cache(name=weight_name + "r_cache", w=r_cache)

        # Load covariance matrix
        force_recompute = False
        # force_recompute = layer != hparams.layers[0]
        cov = get_cov(
            model,
            tok,
            hparams.rewrite_module_tmp.format(layer),
            hparams.mom2_dataset,
            hparams.mom2_n_samples
            if not force_recompute
            else hparams.mom2_n_samples // 10,
            hparams.mom2_dtype,
            force_recompute=force_recompute,
            hparams=hparams
        )

        right_cache = layer_ks.to(cov.device) @ layer_ks.to(cov.device).T
        upd_cache(name=weight_name + "right_cache", w=right_cache)

        r = get_cache(name=weight_name + "r_cache").detach().clone()
        right = get_cache(name=weight_name + "right_cache").detach().clone()
        right = right + hparams.mom2_update_weight * cov.double().to(right.device)
        resid = r / (len(hparams.layers) - i)  # Distribute residual across layers
        with torch.no_grad():
            upd_matrix = torch.linalg.solve(right, resid, left=False)

        # Adjust update matrix shape
        upd_matrix = upd_matrix_match_shape(upd_matrix, weights[weight_name].shape)

        print("orig norm", torch.linalg.norm(weights[weight_name]))
        print("upd norm", torch.linalg.norm(upd_matrix))

        # Update model weights and record desired changes in `delta` variable
        with torch.no_grad():
            weights[weight_name][...] = weights_copy[weight_name] + upd_matrix.to(
                weights_copy[weight_name].device).float()
            deltas[weight_name] = upd_matrix.detach().cpu()

        # Clear GPU memory
        cov.cpu()
        for x in [layer_ks, cur_zs, targets]:
            x.cpu()
            del x
        torch.cuda.empty_cache()

    # Restore state of original model
    with torch.no_grad():
        for k, v in weights.items():
            v[...] = weights_copy[k]

    print(f"Deltas successfully computed for {list(weights.keys())}")

    return deltas, prob_list


def upd_matrix_match_shape(matrix: torch.Tensor, shape: torch.Size) -> torch.Tensor:
    """
    GPT-2 and GPT-J have transposed weight representations.
    Returns a matrix that matches the desired shape, else raises a ValueError
    """

    if matrix.shape == shape:
        return matrix
    elif matrix.T.shape == shape:
        return matrix.T
    else:
        raise ValueError(
            "Update matrix computed by MEMIT does not match original weight shape. "
            "Check for bugs in the code?"
        )


def get_context_templates(model, tok):
    global CONTEXT_TEMPLATES_CACHE

    if CONTEXT_TEMPLATES_CACHE is None:
        CONTEXT_TEMPLATES_CACHE = [["{}"]] + [
            [
                f.replace("{", " ").replace("}", " ") + ". {}"
                for f in generate_fast(
                model,
                tok,
                ["The", "Therefore", "Because", "I", "You"],
                n_gen_per_prompt=n_gen // 5,
                max_out_len=length,
            )
            ]
            for length, n_gen in [(10, 5)]  # Be careful about changing this.
        ]
        print(f"Cached context templates {CONTEXT_TEMPLATES_CACHE}")

    return CONTEXT_TEMPLATES_CACHE
