# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import operator
from typing import Callable, Optional, Sequence

import jax
from jax import core
from jax import linear_util as lu
from jax.interpreters import ad
from jax.interpreters import batching
from jax.interpreters.batching import not_mapped
from jax.interpreters import partial_eval as pe
from jax.tree_util import (tree_flatten, tree_map, tree_structure,
                           tree_unflatten, treedef_tuple)
from jax._src import source_info_util
from jax._src import traceback_util
from jax._src import util
from jax._src.api_util import flatten_fun_nokwargs


source_info_util.register_exclusion(__file__)
traceback_util.register_exclusion(__file__)


map, unsafe_map = util.safe_map, map
zip, unsafe_zip = util.safe_zip, zip


class custom_vmap:
  fun: Callable
  vmap_rule: Optional[Callable]

  def __init__(self, fun: Callable) -> None:
    self.fun = fun  # type: ignore[assignment]
    self.vmap_rule = None

  def def_vmap(self, vmap_rule: Callable) -> None:
    self.vmap_rule = vmap_rule

  @traceback_util.api_boundary
  def __call__(self, *args, **kwargs):
    assert not kwargs
    args_flat, in_tree = tree_flatten(args)
    flat_fun, out_tree = flatten_fun_nokwargs(lu.wrap_init(self.fun), in_tree)
    in_avals = [core.raise_to_shaped(core.get_aval(x)) for x in args_flat]
    debug = pe.debug_info(self.fun, in_tree, False, "custom_vmap")
    jaxpr, _, consts = pe.trace_to_jaxpr_dynamic(flat_fun, in_avals, debug)
    assert not len(consts)
    out_flat = custom_vmap_p.bind(*consts, *args_flat,
                                  call=pe.convert_constvars_jaxpr(jaxpr),
                                  rule=self.vmap_rule,
                                  in_tree=in_tree)
    return tree_unflatten(out_tree(), out_flat)


### utils


def ensure_list(xs):
  return xs if type(xs) is list else list(xs)

def rule_name(rule):
  return getattr(rule, '__name__', '<unnamed rule>')

def call_rule(rule, axis_size, in_batched, args):
  outs, out_batched = rule(axis_size, ensure_list(in_batched), *args)
  if not isinstance(outs, Sequence):
    raise TypeError(
        'custom vmap rule output values must be a sequence, '
        f'rule ({rule_name(rule)}) returned {type(outs)}')
  if not isinstance(out_batched, Sequence):
    raise TypeError(
        'custom vmap rule output batching specification must be a sequence, '
        f'rule ({rule_name(rule)}) returned {type(out_batched)}')
  return ensure_list(outs), ensure_list(out_batched)

def check_vmap_rule_trees(rule, out_tree, out_batched_tree):
  if out_tree != out_batched_tree:
    raise ValueError(
        'structure of output values and output batching specification returned '
        f'by custom vmap rule ({rule_name(rule)}) do not match.\n'
        f'Output values: {out_tree}\n'
        f'Batching spec: {out_batched_tree}')

# Like batching.bdim_at_front, but doesn't broadcast if not mapped
def maybe_bdim_at_front(x, bdim):
  if core.get_aval(x) is core.abstract_unit:
    return core.unit
  if bdim is not_mapped:
    return x
  else:
    return util.moveaxis(x, bdim, 0)

# Like batching.batch except (a) not curried and (b) returns inferred output
# axes instead of accepting and matching a given spec of output axes. Assumes
# `f` is pytree-flattened
def vmap_unrestricted(f: lu.WrappedFun, *args, in_axes, axis_name, axis_size):
  f, out_axes = batching.batch_subtrace(f)
  f = batching._batch_outer(f, axis_name, axis_size, in_axes,
                            batching.BatchTrace)
  outs = f.call_wrapped(*args)
  return outs, out_axes()


### custom_vmap_p rules


def custom_vmap_impl(*args, call, rule, in_tree):
  del rule, in_tree
  return core.eval_jaxpr(call, (), *args)


def custom_vmap_batching(args_flat, dims, *, call, rule, in_tree):
  del call
  axis_size, = {x.shape[d] for x, d in zip(args_flat, dims) if d is not None}
  args_flat = map(maybe_bdim_at_front, args_flat, dims)
  flat_in_batched = [d is not not_mapped for d in dims]

  args = tree_unflatten(in_tree, args_flat)
  in_batched = tree_unflatten(in_tree, flat_in_batched)
  outs, out_batched = call_rule(rule, axis_size, in_batched, args)
  flat_outs, tree1 = tree_flatten(outs)
  flat_out_batched, tree2 = tree_flatten(out_batched,
                                         is_leaf=lambda x: x is None)
  check_vmap_rule_trees(rule, tree1, tree2)
  flat_out_dims = [0 if b else not_mapped for b in flat_out_batched]
  return flat_outs, flat_out_dims


def custom_vmap_jvp(primals, tangents, *, call, rule, in_tree):
  def jvp_of_rule_rule(axis_size, in_batched, primals, tangents):
    in_batched_ps, in_batched_ts = in_batched

    mutually_batched = tree_map(operator.and_, in_batched_ps, in_batched_ts)
    extra_batched_ps = tree_map(lambda pb, tb: 0 if pb and not tb else None,
                                in_batched_ps, in_batched_ts)
    extra_batched_ts = tree_map(lambda pb, tb: 0 if tb and not pb else None,
                                in_batched_ps, in_batched_ts)

    out_mutually_batched = lu.Store()
    flat_ps_ts, tree_ps_ts = tree_flatten((primals, tangents))
    flat_extra_batched_ps_ts, tree_ps_ts2 = tree_flatten(
        (extra_batched_ps, extra_batched_ts),
        is_leaf=lambda x: x is None)

    # TODO(frostig): assert these also equal:
    #   treedef_tuple((in_tree, in_tree))
    # once https://github.com/google/jax/issues/9066 is fixed
    assert tree_ps_ts == tree_ps_ts2
    del tree_ps_ts2

    def to_jvp(*primals):
      outs, out_batched = call_rule(rule, axis_size, mutually_batched, primals)
      check_vmap_rule_trees(
          rule, tree_structure(outs), tree_structure(out_batched))
      out_mutually_batched.store(out_batched)
      return outs

    def to_vmap_over_extra_batched_dims(primals, tangents):
      return jax.jvp(to_jvp, primals, tangents)

    to_vmap_over_extra_batched_dims_flat, out_tree = flatten_fun_nokwargs(
        lu.wrap_init(to_vmap_over_extra_batched_dims),
        tree_ps_ts)

    flat_out_ps_ts, flat_out_axes = vmap_unrestricted(
        to_vmap_over_extra_batched_dims_flat, *flat_ps_ts,
        in_axes=flat_extra_batched_ps_ts,
        axis_name=core.no_axis_name, axis_size=axis_size)

    n, ragged = divmod(len(flat_out_ps_ts), 2)
    assert not ragged
    flat_out_ps, flat_out_ts = flat_out_ps_ts[:n], flat_out_ps_ts[n:]
    flat_out_axes_p, flat_out_axes_t = flat_out_axes[:n], flat_out_axes[n:]
    flat_out_ps = map(maybe_bdim_at_front, flat_out_ps, flat_out_axes_p)
    flat_out_extra_batched_ps = [d is not not_mapped for d in flat_out_axes_p]
    flat_out_ts = map(maybe_bdim_at_front, flat_out_ts, flat_out_axes_t)
    flat_out_extra_batched_ts = [d is not not_mapped for d in flat_out_axes_t]

    out_ps, out_ts = tree_unflatten(
        out_tree(), [*flat_out_ps, *flat_out_ts])
    out_extra_batched_ps, out_extra_batched_ts = tree_unflatten(
        out_tree(), [*flat_out_extra_batched_ps, *flat_out_extra_batched_ts])

    out_batched_ps = tree_map(
        operator.or_, out_mutually_batched.val, out_extra_batched_ps)
    out_batched_ts = tree_map(
        operator.or_, out_mutually_batched.val, out_extra_batched_ts)

    return (out_ps, out_ts), (out_batched_ps, out_batched_ts)

  closed_call = core.ClosedJaxpr(call, ())
  tangents = map(ad.instantiate_zeros, tangents)
  jvp_call, _ = ad.jvp_jaxpr(closed_call, [True] * len(primals), True)
  jvp_in_tree = treedef_tuple((in_tree, in_tree))
  outs = custom_vmap_p.bind(
      *primals, *tangents,
      call=jvp_call.jaxpr, rule=jvp_of_rule_rule, in_tree=jvp_in_tree)
  assert len(outs) % 2 == 0, len(outs)
  out_primals, out_tangents = util.split_list(outs, [len(outs) // 2])
  return out_primals, out_tangents


custom_vmap_p = core.Primitive('custom_vmap_call')
custom_vmap_p.multiple_results = True
custom_vmap_p.def_impl(custom_vmap_impl)
batching.primitive_batchers[custom_vmap_p] = custom_vmap_batching
ad.primitive_jvps[custom_vmap_p] = custom_vmap_jvp
