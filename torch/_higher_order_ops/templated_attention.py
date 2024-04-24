from typing import Callable, Tuple, Union

import torch
import torch.utils._pytree as pytree
from torch._C import DispatchKey
from torch._higher_order_ops.utils import (
    _has_potential_branch_input_mutation,
    autograd_not_implemented,
    UnsupportedAliasMutationException,
)
from torch._ops import HigherOrderOperator
from torch._subclasses import FakeTensorMode
from torch.fx.experimental.proxy_tensor import (
    make_fx,
    ProxyTorchDispatchMode,
    track_tensor_tree,
)
from torch.fx.graph_module import GraphModule


class TemplatedAttentionHOP(HigherOrderOperator):
    def __init__(self):
        super().__init__("templated_attention")

    def __call__(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        score_mod: Callable,
        *other_buffers: torch.Tensor,
    ):
        if not all(isinstance(buf, torch.Tensor) for buf in other_buffers):
            raise RuntimeError("Other buffers must be tensors.")
        return super().__call__(query, key, value, score_mod, *other_buffers)


templated_attention = TemplatedAttentionHOP()
templated_attention.__module__ = "torch.ops.higher_order"


class TemplatedAttentionBackwardHOP(HigherOrderOperator):
    def __init__(self):
        super().__init__("templated_attention_backward")

    def __call__(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        out: torch.Tensor,
        logsumexp: torch.Tensor,
        grad_out: torch.Tensor,
        fw_graph: Union[Callable, GraphModule],
        joint_graph: GraphModule,
        *other_buffers: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        if not all(isinstance(buf, torch.Tensor) for buf in other_buffers):
            raise RuntimeError("Other buffers must be tensors.")
        return super().__call__(
            query,
            key,
            value,
            out,
            logsumexp,
            grad_out,
            fw_graph,
            joint_graph,
            *other_buffers,
        )


templated_attention_backward = TemplatedAttentionBackwardHOP()
templated_attention_backward.__module__ = "torch.ops.higher_order"


def math_attention(
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    score_mod: Callable,
    *other_buffers: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Eager implementation

    This implementation uses vmap to vectorize the score_mod function over the batch, head, m, and n dimensions.
    We then apply the vectorized score_mod function to the scores matrix. Each wrap of vmap applies one of the
    batch, head, m, or n dimensions. We need to apply vmap 4 times to vectorized over all 4 dimensions.

    Args:
        query: The query tensor
        key: The key tensor
        value: The value tensor
        score_mod: The score_mod function
        other_buffers: Other buffers that are passed to the score_mod function
    """
    working_precision = torch.float64 if query.dtype == torch.float64 else torch.float32

    scores = (query @ key.transpose(-2, -1)).to(dtype=working_precision)

    b = torch.arange(0, scores.size(0), device=scores.device)
    h = torch.arange(0, scores.size(1), device=scores.device)
    m = torch.arange(0, scores.size(2), device=scores.device)
    n = torch.arange(0, scores.size(3), device=scores.device)

    in_dim_buffers = (None,) * len(other_buffers)
    score_mod = torch.vmap(score_mod, in_dims=(0, None, None, None, 0) + in_dim_buffers)
    score_mod = torch.vmap(score_mod, in_dims=(0, None, None, 0, None) + in_dim_buffers)
    score_mod = torch.vmap(score_mod, in_dims=(0, None, 0, None, None) + in_dim_buffers)
    score_mod = torch.vmap(score_mod, in_dims=(0, 0, None, None, None) + in_dim_buffers)

    scores = score_mod(scores, b, h, m, n, *other_buffers).to(working_precision)

    # TODO Unconditionally return logsumexp for backwards
    # if any(t.requires_grad for t in (query, key, value)):
    logsumexp = scores.logsumexp(dim=-1)

    scores = scores.softmax(dim=-1)

    return scores.to(query.dtype) @ value, logsumexp


@templated_attention.py_impl(DispatchKey.CompositeExplicitAutograd)
def sdpa_dense(
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    score_mod: Callable,
    *other_buffers: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor]:
    out, lse = math_attention(query, key, value, score_mod, *other_buffers)
    out = out.contiguous()
    return out, lse


def trace_templated_attention(
    proxy_mode: ProxyTorchDispatchMode,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    score_mod: Callable,
    *other_buffers: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Traces the templated_attention operator with the given score_mod function and other_buffers.

    Trace SDPA will call make_fx with "fake" example vals and then trace the score_mod function
    This will produce a GraphModule that will be stored on the root tracer as "sdpa_score". We
    access this graph module in inductor to inline the score_mod function to the triton template.
    """
    example_out = templated_attention(query, key, value, score_mod, *other_buffers)
    example_vals = [
        torch.zeros((), dtype=query.dtype, requires_grad=query.requires_grad)
    ] + [torch.zeros((), dtype=torch.int) for _ in range(4)]
    score_graph = make_fx(score_mod)(*example_vals, *other_buffers)
    proxy_mode.tracer.root.register_module("sdpa_score", score_graph)
    node_args = (query, key, value, score_graph, *other_buffers)
    proxy_args = pytree.tree_map(proxy_mode.tracer.unwrap_proxy, node_args)
    out_proxy = proxy_mode.tracer.create_proxy(
        "call_function", templated_attention, proxy_args, {}, name="templated_attention"
    )
    return track_tensor_tree(
        example_out, out_proxy, constant=None, tracer=proxy_mode.tracer
    )


@templated_attention.py_impl(ProxyTorchDispatchMode)
def templated_attention_proxy_torch_dispatch_mode(
    mode: ProxyTorchDispatchMode,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    score_mod: Callable,
    *other_buffers: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor]:
    assert mode is not None, "Mode should always be enabled for python fallback key"
    if mode.enable_tracing:
        return trace_templated_attention(
            mode, query, key, value, score_mod, *other_buffers
        )
    else:
        return templated_attention(query, key, value, score_mod, *other_buffers)


@templated_attention.py_functionalize_impl
def templated_attention_functionalize(
    ctx: torch._subclasses.functional_tensor.BaseFunctionalizeAPI,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    score_mod: Callable,
    *other_buffers: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Defines the functionalization rules for the templated_attention operator.

    Write now we are unwrapping each tensor and then redispatching to the next, however we want to
    guard against any mutations in the score_mod function, to the other_buffers since those
    are free variables.
    """
    query_unwrapped = ctx.unwrap_tensors(query)
    key_unwrapped = ctx.unwrap_tensors(key)
    value_unwrapped = ctx.unwrap_tensors(value)
    other_buffers_unwrapped = ctx.unwrap_tensors(other_buffers)

    # Appease the mypy overlords
    assert isinstance(query_unwrapped, torch.Tensor)
    assert isinstance(key_unwrapped, torch.Tensor)
    assert isinstance(value_unwrapped, torch.Tensor)
    assert isinstance(other_buffers_unwrapped, tuple)
    assert all(isinstance(item, torch.Tensor) for item in other_buffers_unwrapped)

    example_vals = (
        [torch.zeros((), dtype=query.dtype)]
        + [torch.zeros((), dtype=torch.int) for _ in range(4)]
        + list(other_buffers_unwrapped)
    )
    with ctx.redispatch_to_next() as m:
        functional_score_mod = ctx.functionalize(score_mod)
        pre_dispatch = hasattr(ctx, "mode") and ctx.mode.pre_dispatch
        mutates = _has_potential_branch_input_mutation(
            functional_score_mod, example_vals, pre_dispatch
        )
        # The only care about mutations of existing buffers since we can't replay these.
        # However, we can just error if anything is detected
        if mutates:
            raise UnsupportedAliasMutationException("Mutations detected in score_mod")

        out = templated_attention(
            query_unwrapped,
            key_unwrapped,
            value_unwrapped,
            functional_score_mod,
            *other_buffers_unwrapped,
        )
    return ctx.wrap_tensors(out)  # type: ignore[return-value]


@templated_attention.py_impl(FakeTensorMode)
def templated_attention_fake_tensor_mode(
    mode: FakeTensorMode,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    score_mod: Callable,
    *other_buffers: Tuple[torch.Tensor, ...],
) -> Tuple[torch.Tensor, torch.Tensor]:
    with mode:
        batch_size, num_heads, seq_len_q, _ = query.shape
        logsumexp = query.new_empty(
            batch_size, num_heads, seq_len_q, dtype=torch.float32
        )
        return torch.empty_like(query, memory_format=torch.contiguous_format), logsumexp


# ---------------------------- Autograd Implementation ----------------------------
def create_fw_bw_graph(score_mod, index_values, other_buffers):
    # See Note:[HOP create fw_bw graph]

    # All of these imports need to be here in order to avoid circular dependencies
    from torch._dispatch.python import suspend_functionalization
    from torch._functorch.aot_autograd import AOTConfig, create_joint, from_fun

    from torch._subclasses.functional_tensor import (
        disable_functional_mode,
        FunctionalTensor,
    )
    from torch.fx.experimental.proxy_tensor import disable_proxy_modes_tracing

    dummy_aot_config = AOTConfig(
        fw_compiler=None,  # type: ignore[arg-type]
        bw_compiler=None,  # type: ignore[arg-type]
        partition_fn=None,  # type: ignore[arg-type]
        decompositions={},
        num_params_buffers=0,
        aot_id=0,
        keep_inference_input_mutations=False,
    )
    with suspend_functionalization(), disable_functional_mode():
        with disable_proxy_modes_tracing():

            def _from_fun(t):
                if isinstance(t, torch.Tensor):
                    if t.dtype != torch.bool:
                        # New empty is causing some bogus values to be attempted to index
                        return torch.zeros(
                            t.size(), dtype=t.dtype, requires_grad=t.requires_grad
                        )
                        return torch.empty_strided(
                            t.size(),
                            t.stride(),
                            dtype=t.dtype,
                            requires_grad=t.requires_grad,
                        )
                    else:
                        # clone of a functional tensor produces a functional tensor
                        # but we want to avoid it so we clone a non-functional version
                        maybe_unfunc_t = t
                        if isinstance(t, FunctionalTensor):
                            torch._sync(t)
                            maybe_unfunc_t = from_fun(t)
                        elif torch._is_functional_tensor(t):
                            # need to handle both types of functionalization here:
                            # these are the tensors that came from the user,
                            # which could be either FunctionalTensorWrapper or FunctionalTensor
                            torch._sync(t)
                            maybe_unfunc_t = torch._from_functional_tensor(t)
                        return maybe_unfunc_t.clone()
                return t

            unwrapped_score_mod_indexes = pytree.tree_map(_from_fun, index_values)
            unwrapped_other_buffers = pytree.tree_map(_from_fun, other_buffers)
            example_flat_out = pytree.tree_map(
                _from_fun,
                score_mod(*unwrapped_score_mod_indexes, *unwrapped_other_buffers),
            )
            if not isinstance(example_flat_out, torch.Tensor):
                raise RuntimeError(
                    "Expected output of score_mod to be a tensor."
                    f"Got type {type(example_flat_out)}."
                )
            example_grad = _from_fun(example_flat_out)

        def joint_f(score, b, h, m, n, example_grad, *other_buffers):
            def fw_with_masks(*args):
                fw_out = score_mod(*args)
                out_requires_grad = fw_out.requires_grad
                return ((fw_out,), (out_requires_grad,))

            joint = create_joint(fw_with_masks, aot_config=dummy_aot_config)
            args = [score, b, h, m, n] + list(other_buffers)
            optional_grad = [example_grad] if example_grad.requires_grad else []
            _, grads = joint(args, optional_grad)

            return grads

        joint_graph = make_fx(joint_f)(
            *unwrapped_score_mod_indexes, example_grad, *unwrapped_other_buffers
        )
        return score_mod, joint_graph


class TemplatedAttentionAutogradOp(torch.autograd.Function):
    @staticmethod
    def forward(
        ctx, query, key, value, fw_graph, joint_graph, *other_buffers
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        any_buffer_requires_grad = any(buffer.requires_grad for buffer in other_buffers)
        assert (
            not any_buffer_requires_grad
        ), "Captured buffers that require grad are not yet supported."
        ctx._fw_graph = fw_graph
        ctx._joint_graph = joint_graph
        with torch._C._AutoDispatchBelowAutograd():
            out, logsumexp = templated_attention(
                query, key, value, fw_graph, *other_buffers
            )

        ctx.save_for_backward(query, key, value, out, logsumexp, *other_buffers)
        return out, logsumexp

    @staticmethod
    def backward(ctx, grad_out, logsumexp_grad):
        fw_args = ctx.saved_tensors
        query, key, value, out, logsumexp, *other_buffers = fw_args
        fw_graph = ctx._fw_graph
        joint_graph = ctx._joint_graph
        # We have asserted that other_buffers do not require grad in the forward
        none_grads = [None] * (2 + len(other_buffers))
        grad_query, grad_key, grad_value = templated_attention_backward(
            query,
            key,
            value,
            out,
            logsumexp,
            grad_out,
            fw_graph,
            joint_graph,
            *other_buffers,
        )
        return grad_query, grad_key, grad_value, *none_grads


@templated_attention.py_impl(DispatchKey.Autograd)
def templated_attention_autograd(
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    score_mod: Callable,
    *other_buffers: Tuple[torch.Tensor, ...],
) -> Tuple[torch.Tensor, torch.Tensor]:
    input_requires_grad = any(t.requires_grad for t in (query, key, value))
    example_vals = [
        torch.zeros((), dtype=query.dtype, requires_grad=input_requires_grad)
    ] + [torch.zeros((), dtype=torch.int) for _ in range(4)]
    fw_graph, bw_graph = create_fw_bw_graph(score_mod, example_vals, other_buffers)
    out, logsumexp = TemplatedAttentionAutogradOp.apply(
        query, key, value, fw_graph, bw_graph, *other_buffers
    )
    return out, logsumexp


# ---------------------------- Backward HOP Implementation ----------------------------


@templated_attention_backward.py_impl(DispatchKey.CompositeExplicitAutograd)
def sdpa_dense_backward(
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    out: torch.Tensor,
    logsumexp: torch.Tensor,
    grad_out: torch.Tensor,
    fw_graph: Callable,  # GraphModule type hint?
    joint_graph: Callable,
    *other_buffers: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    working_precision = torch.float64 if query.dtype == torch.float64 else torch.float32
    scores = (query @ key.transpose(-2, -1)).to(working_precision)

    b = torch.arange(0, scores.size(0), device=scores.device)
    h = torch.arange(0, scores.size(1), device=scores.device)
    m = torch.arange(0, scores.size(2), device=scores.device)
    n = torch.arange(0, scores.size(3), device=scores.device)

    in_dim_buffers = (None,) * len(other_buffers)
    score_mod = torch.vmap(fw_graph, in_dims=(0, None, None, None, 0) + in_dim_buffers)
    score_mod = torch.vmap(score_mod, in_dims=(0, None, None, 0, None) + in_dim_buffers)
    score_mod = torch.vmap(score_mod, in_dims=(0, None, 0, None, None) + in_dim_buffers)
    score_mod = torch.vmap(score_mod, in_dims=(0, 0, None, None, None) + in_dim_buffers)

    scores = score_mod(scores, b, h, m, n, *other_buffers).to(working_precision)

    softmax_scores = torch.exp(scores - logsumexp.unsqueeze(-1))

    grad_value = softmax_scores.to(query.dtype).transpose(-2, -1) @ grad_out

    grad_softmax_scores = grad_out @ value.transpose(-2, -1)

    sum_scores = torch.sum(out * grad_out, -1, keepdim=True)
    grad_score_mod = softmax_scores * (grad_softmax_scores - sum_scores)

    # Gradient of the inline score_mod function, with respect to the scores
    in_dim_buffers = (None,) * len(other_buffers)
    out_dims = [0, None, None, None, None]
    joint_score_mod = torch.vmap(
        joint_graph,
        in_dims=(0, None, None, None, 0, 0) + in_dim_buffers,
        out_dims=out_dims,
    )
    joint_score_mod = torch.vmap(
        joint_score_mod,
        in_dims=(0, None, None, 0, None, 0) + in_dim_buffers,
        out_dims=out_dims,
    )
    joint_score_mod = torch.vmap(
        joint_score_mod,
        in_dims=(0, None, 0, None, None, 0) + in_dim_buffers,
        out_dims=out_dims,
    )
    joint_score_mod = torch.vmap(
        joint_score_mod,
        in_dims=(0, 0, None, None, None, 0) + in_dim_buffers,
        out_dims=out_dims,
    )

    grad_scores = joint_score_mod(scores, b, h, m, n, grad_score_mod, *other_buffers)[
        0
    ].to(query.dtype)

    grad_query = grad_scores @ key
    grad_key = grad_scores.transpose(-2, -1) @ query
    return grad_query.contiguous(), grad_key.contiguous(), grad_value.contiguous()


def trace_templated_attention_backward(
    proxy_mode: ProxyTorchDispatchMode,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    out: torch.Tensor,
    logsumexp: torch.Tensor,
    grad_out: torch.Tensor,
    fw_graph: Union[Callable, GraphModule],
    joint_graph: GraphModule,
    *other_buffers: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """We already have the forward graph and joint graph from the forward pass, so we create a proxy attach both graphs"""
    example_out = templated_attention_backward(
        query,
        key,
        value,
        out,
        logsumexp,
        grad_out,
        fw_graph,
        joint_graph,
        *other_buffers,
    )
    # TODO If I dont wrap the graph modules in functionalize I already have my graph modules..
    # Ask Brian
    fw_example_vals = [
        torch.zeros((), dtype=query.dtype, requires_grad=query.requires_grad)
    ] + [torch.zeros((), dtype=torch.int) for _ in range(4)]
    bw_example_vals = fw_example_vals + [torch.zeros((), dtype=query.dtype)]
    fw_graph = make_fx(fw_graph)(*fw_example_vals, *other_buffers)
    joint_graph = make_fx(joint_graph)(*bw_example_vals, *other_buffers)
    proxy_mode.tracer.root.register_module("fw_graph", fw_graph)
    proxy_mode.tracer.root.register_module("joint_graph", joint_graph)
    node_args = (
        query,
        key,
        value,
        out,
        logsumexp,
        grad_out,
        fw_graph,
        joint_graph,
        *other_buffers,
    )
    proxy_args = pytree.tree_map(proxy_mode.tracer.unwrap_proxy, node_args)
    out_proxy = proxy_mode.tracer.create_proxy(
        "call_function",
        templated_attention_backward,
        proxy_args,
        {},
        name="templated_attention_backward",
    )
    return track_tensor_tree(
        example_out, out_proxy, constant=None, tracer=proxy_mode.tracer
    )


@templated_attention_backward.py_impl(ProxyTorchDispatchMode)
def templated_attention_backward_proxy_torch_dispatch_mode(
    mode: ProxyTorchDispatchMode,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    out: torch.Tensor,
    logsumexp: torch.Tensor,
    grad_out: torch.Tensor,
    fw_graph: Union[Callable, GraphModule],
    joint_graph: GraphModule,
    *other_buffers: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    assert mode is not None, "Mode should always be enabled for python fallback key"
    if mode.enable_tracing:
        return trace_templated_attention_backward(
            mode,
            query,
            key,
            value,
            out,
            logsumexp,
            grad_out,
            fw_graph,
            joint_graph,
            *other_buffers,
        )
    else:
        return templated_attention_backward(
            query,
            key,
            value,
            out,
            logsumexp,
            grad_out,
            fw_graph,
            joint_graph,
            *other_buffers,
        )


@templated_attention_backward.py_functionalize_impl
def templated_attention_backward_functionalize(
    ctx: torch._subclasses.functional_tensor.BaseFunctionalizeAPI,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    out: torch.Tensor,
    logsumexp: torch.Tensor,
    grad_out: torch.Tensor,
    fw_graph: Union[Callable, GraphModule],
    joint_graph: GraphModule,
    *other_buffers: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Defines the functionalization rules for the templated_attention operator.

    Write now we are unwrapping each tensor and then redispatching to the next,
    since we know that the forward score mod function is assured to be free of mutations
    to the other_buffers, we skip that mutate check and go straight to redispatching.
    """
    query_unwrapped = ctx.unwrap_tensors(query)
    key_unwrapped = ctx.unwrap_tensors(key)
    value_unwrapped = ctx.unwrap_tensors(value)
    out_unwrapped = ctx.unwrap_tensors(out)
    logsumexp_unwrapped = ctx.unwrap_tensors(logsumexp)
    grad_out_unwrapped = ctx.unwrap_tensors(grad_out)
    other_buffers_unwrapped = ctx.unwrap_tensors(other_buffers)

    # Appease the mypy overlords
    assert isinstance(query_unwrapped, torch.Tensor)
    assert isinstance(key_unwrapped, torch.Tensor)
    assert isinstance(value_unwrapped, torch.Tensor)
    assert isinstance(out_unwrapped, torch.Tensor)
    assert isinstance(logsumexp_unwrapped, torch.Tensor)
    assert isinstance(grad_out_unwrapped, torch.Tensor)
    assert isinstance(other_buffers_unwrapped, tuple)
    assert all(isinstance(item, torch.Tensor) for item in other_buffers_unwrapped)

    with ctx.redispatch_to_next() as m:
        functional_fw_graph = ctx.functionalize(fw_graph)
        functional_joint_graph = ctx.functionalize(joint_graph)

        grad_query, grad_key, grad_value = templated_attention_backward(
            query_unwrapped,
            key_unwrapped,
            value_unwrapped,
            out_unwrapped,
            logsumexp_unwrapped,
            grad_out_unwrapped,
            functional_fw_graph,  # type: ignore[arg-type]
            functional_joint_graph,  # type: ignore[arg-type]
            *other_buffers_unwrapped,
        )

    return ctx.wrap_tensors((grad_query, grad_key, grad_value))  # type: ignore[return-value,arg-type]


@templated_attention_backward.py_impl(FakeTensorMode)
def templated_attention_backward_fake_tensor_mode(
    mode: FakeTensorMode,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    out: torch.Tensor,
    logsumexp: torch.Tensor,
    grad_out: torch.Tensor,
    fw_graph: Union[Callable, GraphModule],
    joint_graph: GraphModule,
    *other_buffers: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    with mode:
        grad_query = torch.empty_like(query, memory_format=torch.contiguous_format)
        grad_key = torch.empty_like(key, memory_format=torch.contiguous_format)
        grad_value = torch.empty_like(value, memory_format=torch.contiguous_format)
        return grad_query, grad_key, grad_value


templated_attention_backward.py_impl(DispatchKey.Autograd)(
    autograd_not_implemented(templated_attention_backward, deferred_error=True)
)
