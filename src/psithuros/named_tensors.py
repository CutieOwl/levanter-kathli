import dataclasses
import functools
import inspect
import typing
from inspect import BoundArguments
from typing import List, Tuple, Dict, Sequence, Optional, Callable, Any, get_origin, get_args, Annotated, Union

import equinox as eqx
import jax
from equinox.custom_types import sentinel, PyTree
import jax.numpy as jnp
from jax._src.tree_util import flatten_one_level, all_leaves, _registry
from jax.experimental.maps import xmap, AxisName, ResourceSet


class Array(jax.numpy.ndarray):
    def __class_getitem__(cls, item):
        if not isinstance(item, tuple):
            item = (item,)
        return AxisNames(item)


@dataclasses.dataclass
class AxisNames:
    names: Tuple[AxisName, ...]

    def __call__(self, *args, **kwds):
        raise TypeError(f"Shouldn't call this. Just necessary to trick the type checker")

    def concat(self, other: Optional['AxisNames']) -> 'AxisNames':
        if other is None:
            return self
        return AxisNames(tuple(self.names) + tuple(other.names))

    def __hash__(self):
        return hash(self.names)

    def __eq__(self, other):
        return self.names == other.names

UnnamedAxes = AxisNames(names=(...,))

T = typing.TypeVar("T")
N = typing.TypeVar("N")

# Shaped = Annotated[T, Array[N]]


class Shaped(typing.Generic[T]):

    """Supports things like Shaped["shard", Linear] or Shaped[ ("shard", "x", ...), Linear]. The returned type is """
    def __class_getitem__(cls, item: Tuple[typing.Union[AxisName, Tuple[AxisName, ...]], typing.Type[T]])-> typing.Type[T]:
        if len(item) != 2:
            raise ValueError("Shaped[...] only supports two-tuples. If you want to use a tuple of axes, use Shaped[(...), ...]")
        shapes, tpe = item
        if not isinstance(shapes, tuple):
            shapes = (shapes,)

        return typing.Annotated[tpe, AxisNames(shapes)]


def infer_named_axes_from_module(mod: eqx.Module):
    """Automatically get a "pytree" of named axes for an equinox Module. This can be passed to xmap."""
    # first split into the pytree
    dynamic_values, aux = mod.tree_flatten()
    dynamic_field_names = aux[0]
    fields: Sequence[dataclasses.Field] = dataclasses.fields(mod)  # type:ignore
    fields = {f.name: f for f in fields}

    named_shapes: List[Tuple[AxisName, ...]] = []

    for name, value in zip(dynamic_field_names, dynamic_values):
        if name not in fields:
            raise ValueError(f"Could not find field {name} in {mod.__class__}")

        field = fields[name]
        shape = infer_named_axes(value=value, tpe=field.type)
        named_shapes.append(shape)

    return mod.__class__.tree_unflatten(aux, named_shapes)

def infer_leaf_axes(tpe: type)-> List[AxisNames]:
    origin = get_origin(tpe)
    if origin is Annotated:
        args = get_args(tpe)
        shapeses = [s for s in args[1:] if isinstance(s, AxisNames)]
        if len(shapeses) != 1:
            raise ValueError(f"We only support one Shaped[...] in a leaf type, but got {shapeses}")
        prefix_names = shapeses[0].names

        recursive_leaf_names = infer_leaf_axes(args[0])
        return [prefix_names + n for n in recursive_leaf_names]
    elif type(tpe) == eqx.module._ModuleMeta and issubclass(tpe, eqx.Module):
        # unfortunately need to replicate the logic in Module#tree_flatten
        shapes = []
        for field_ in dataclasses.fields(tpe):  # type: ignore
            if not field_.metadata.get("static", False):
                shapes += infer_leaf_axes(field_.type)
        return shapes
    elif isinstance(tpe, AxisNames):
        return tpe
    elif tpe is Array:
        return UnnamedAxes
    else:
        return UnnamedAxes

def infer_named_axes(value: Any, tpe: Optional[type])->Optional[Union[AxisNames, PyTree]]:
    "return value of None means the argument is static and unshaped"
    origin = get_origin(tpe)
    if origin is Annotated:
        args = get_args(tpe)
        shapeses = [s for s in args[1:] if isinstance(s, AxisNames)]
        if len(shapeses) != 1:
            raise ValueError(f"We only support one Shaped[...] in a leaf type, but got {shapeses}")
        prefix_names = shapeses[0]

        recursive_leaf_names = infer_named_axes(value, args[0])
        if recursive_leaf_names is None:
            return prefix_names
        else:
            return jax.tree_map(lambda x: prefix_names.concat(x), recursive_leaf_names)
    elif isinstance(value, eqx.Module):
        return infer_named_axes_from_module(value)
    elif isinstance(tpe, AxisNames):
        return tpe
    elif isinstance(value, jax.numpy.ndarray):
        return UnnamedAxes
    elif tpe is Array:
        return UnnamedAxes
    elif all_leaves([value]):
        return UnnamedAxes
    else:
        # TODO exploit tuple types: tuples, lists, dicts, etc.
        handler = _registry.get(type(value))
        if not handler:
            raise NotImplementedError("Don't know how to infer axes for type %s" % type(value))
        children, meta = handler.to_iter(value)
        leaf_axes = [infer_named_axes(leaf, None) for leaf in children]
        return handler.from_iter(meta, leaf_axes)


def auto_xmap(fun: Callable = sentinel,
              *,
              axis_sizes: Dict[AxisName, int] = None,
              axis_resources: Dict[AxisName, ResourceSet] = None,
              backend: Optional[str] = None):
    if fun is sentinel:
        return functools.partial(auto_xmap, axis_sizes=axis_sizes, axis_resources=axis_resources, backend=backend)
    """Wraps xmap to automatically infer tensor names from function signature and dataclass field declarations. This
    method knows about types annotated with NamedArray as well as equinox Module dataclasses."""

    # we want to make a function that, when it is called with a Module, will:
    # 1. infer the names of the axes from the Module's dataclass
    # 2. flatten the module into leaves and treedefs
    # 3. create a new function that will take the leaves as input and unflatten it into a Module
    # 4. call xmap with the new function
    # 5. apply the xmapped function to the flattened module (which will unflatten it)

    sig = inspect.signature(fun)

    @functools.wraps(fun)
    def axis_extracting_function(*args, **kwargs):
        # inspect the function signature for all args and such.
        # We need the signature for figuring out the names of axes for passed-in arrays as well as what type
        # we're expected to return
        # infer the axes of all arguments:
        arg_shapes = [infer_named_axes(arg, param.annotation) for (arg, param) in zip(args, sig.parameters.values())]
        if len(kwargs) > 0:
            raise NotImplementedError("kwargs not yet supported")
        kwarg_shapes = {k: infer_named_axes(v, None) for k, v in kwargs.items()}
        # flatten the arguments into pytrees
        args_leaves, args_treedefs = jax.tree_flatten(args)
        kwargs_leaves_defs = {k: jax.tree_flatten(v) for k,v in kwargs.items()}
        kwargs_leaves = {k: v[0] for k,v in kwargs_leaves_defs.items()}
        kwargs_treedefs = {k: v[1] for k,v in kwargs_leaves_defs.items()}

        # attempt to figure out the return type
        # TODO: want to handle type vars...
        return_axes = infer_leaf_axes(sig.return_annotation)

        results_treedefs = None

        @functools.wraps(fun)
        def function_to_xmap(*args, **kwargs):
            # unflatten the arguments into pytrees
            # args_unflattened = [jax.tree_unflatten(treedef, leaf) for treedef, leaf in zip(args_treedefs, args_leaves)]
            # kwargs_unflattened = {k: jax.tree_unflatten(kwargs_treedefs[k], kwargs_leaves[k]) for k in kwargs_leaves}
            # call the original function
            results = fun(*args, **kwargs)
            # flatten the results into pytrees
            nonlocal results_treedefs
            results_leaves, results_treedefs = jax.tree_flatten(results)
            return results_leaves

        # now we can call xmap
        # TODO: need to do a compile cache thing!
        # TODO: make this work with the signature for plain arrays
        # TODO: need to handle return type
        # TODO: figure out how to use kwargs shapes
        f = xmap(function_to_xmap, in_axes=arg_shapes, out_axes=return_axes)
        result_leaves = f(*args, **kwargs)
        result_unflattened = jax.tree_unflatten(results_treedefs, result_leaves)
        return result_unflattened

    return axis_extracting_function

def _ensure_tuple(x):
    if x is None:
        return ()
    elif isinstance(x, typing.Iterable):
        return tuple(x)
    else:
        return (x,)



def xmapped_init(cls: typing.Type[eqx.Module],
                 *,
                 static_argnums: Optional[Sequence[int]]=None,
                 static_kwarg_names: Optional[Sequence[str]] = None,
                 axis_sizes=None, axis_resources=None, backend=None
                 ):

    axis_sizes = axis_sizes or {}
    axis_resources = axis_resources or {}


    # this is pretty tricky to get right.
    # It shares a lot in common with equinox's filter_jit etc, though it's a bit less fancy (for now), using
    # static argnums etc for now. We also don't bother with making sure caching works, since we're typically
    # only doing this once per run

    static_argnums = _ensure_tuple(static_argnums)
    static_kwarg_names = _ensure_tuple(static_kwarg_names)

    sig = inspect.signature(cls.__init__)

    @functools.wraps(cls.__new__)
    def wrapper_function(*args, **kwargs):

        bound_args: BoundArguments = sig.bind_partial(*((None, ) + args), **kwargs)

        dynamic_args = []
        dynamic_arg_shapes = []
        dynamic_arg_names = []

        for i, (name, param) in enumerate(sig.parameters.items()):
            if i == 0:
                assert name == "self"
                continue

            i = i - 1 # drop one for "self"
            arg = bound_args.arguments[name]
            if name not in static_kwarg_names and i not in static_argnums:
                dynamic_args.append(arg)
                dynamic_arg_shapes.append(infer_named_axes(arg, param.annotation))
                dynamic_arg_names.append(name)


        # these have to be tuples for xmap, but they break tree_map
        dynamic_arg_shapes_as_lists = jax.tree_map(lambda x: x.names if isinstance(x, AxisNames) else x, dynamic_arg_shapes)
        dynamic_arg_shapes = tuple(dynamic_arg_shapes)

        # we have to call the ctor twice under xmap.
        # The first time we get the axis names for the whole thing,
        # the second time we actually xmap

        # helper function we'll use to get the instance
        def construct_object(*dynamic_args):
            # update the signature
            bound_args.arguments.update(dict(zip(dynamic_arg_names, dynamic_args)))
            bound_args.apply_defaults()
            # call the original function, again dropping the first argument which is the dummy self
            inst = cls(*bound_args.args[1:], **bound_args.kwargs)
            return inst

        # first pass: get the axis names
        out_axes = None
        @functools.wraps(cls.__new__)
        def initial_xmap(*dynamic_args):
            # in the first pass we have to remove the names axes from the dynamic args
            def remove_named_axes(value, axis_spec: AxisNames):
                axis_spec = [axis for axis in axis_spec.names if axis is not Ellipsis]
                for _ in axis_spec:
                    value = value[0]
                return value
            # dynamic_args = jax.tree_map(remove_named_axes, dynamic_args, dynamic_arg_shapes)
            inst = construct_object(*dynamic_args)
            nonlocal out_axes
            out_axes = infer_named_axes(inst, cls)
            return None

        xmap(initial_xmap, in_axes=dynamic_arg_shapes_as_lists, out_axes=[...], axis_sizes=axis_sizes, axis_resources=axis_resources)(*dynamic_args)

        out_axes = jax.tree_map(lambda x: x.names if isinstance(x, AxisNames) else x, out_axes)

        # second pass: we actually have our axes.

        # so we can recreate the tree at the end
        result_tree_shape = None

        # now we make the function that we will xmap
        def function_to_xmap(*dynamic_args):
            inst = construct_object(*dynamic_args)
            nonlocal result_tree_shape
            return inst
            # leaves, result_tree_shape = jax.tree_flatten(inst)
            # return leaves

        # now we can call xmap
        f = xmap(function_to_xmap, in_axes=dynamic_arg_shapes_as_lists, out_axes=out_axes,
                 axis_resources=axis_resources, axis_sizes=axis_sizes, backend=backend)
        inst = f(*dynamic_args)
        # result_unflattened = jax.tree_unflatten(result_tree_shape, result_leaves)
        return inst

    return wrapper_function




__all__ = ["xmapped_init", "auto_xmap", "infer_leaf_axes", "infer_named_axes", "Array", "AxisNames",
           "infer_named_axes_from_module", "Shaped"]