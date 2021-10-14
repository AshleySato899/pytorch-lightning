# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import contextlib
import importlib
import inspect
import threading
from contextlib import contextmanager
from itertools import chain
from typing import Callable, Dict, Iterator, Optional

import torch
from torch import nn, Tensor
from torch._C import _DisableTorchDispatch  # type: ignore[attr-defined]
from torch.nn import Module


# Context manager that causes all pytorch operators to dispatch to the passed-in
# type's __torch_dispatch__ function.
# operation that accepts no tensors but returns a tensor.
#
# enable_python_mode is affected by torch._C._DisableTorchDispatch.
#
# NB: Calling an operator inside __torch_dispatch__ does go through
# __torch_dispatch__ again. Please use _DisableTorchDispatch inside
# __torch_dispatch__ to prevent infinite recursion.
#
# TODO: Limitations and things about enable_python_mode we should fix before exposing it:
# - it currently cannot be nested. This should be simple to implement; we need a
#   stack of TorchDispatchTypeObjects and the next bullet point.
# - We need a better user-facing api for torch._C._DisableTorchDispatch that
#   is able to selectively disable __torch_dispatch__ of a particular class.
# - It doesn't work with the tensor constructors (torch.tensor, torch.Tensor)
# - Better name (see https://github.com/pytorch/pytorch/pull/63496#discussion_r694091694)
@contextlib.contextmanager
def enable_python_mode(cls) -> Iterator[None]:
    if not hasattr(cls, "__torch_dispatch__"):
        raise ValueError("The class passed to enable_python_mode " "must have a __torch_dispatch__ classmethod")
    if not isinstance(cls, type) or not issubclass(cls, (torch.Tensor,)):
        raise ValueError("The argument passed to enable_python_mode " "must be the type of a Tensor subclass")
    torch._C._enter_python_mode(cls)
    try:
        yield
    finally:
        torch._C._exit_python_mode()


_tls = threading.local()

# Used to check nested `init_meta()` calls.
_tls.in_call = False


@contextmanager
def _no_dispatch() -> Iterator[None]:
    """Temporarily disables the Python dispatch mode."""
    guard = _DisableTorchDispatch()
    try:
        yield
    finally:
        del guard


def _handle_arange(func, args, kwargs):
    kwargs["device"] = torch.device("cpu")

    # Although not ideal we first instantiate a CPU tensor and then immediately
    # convert it to a meta tensor. Since `arange()` is typically used for small
    # auxiliary vectors, in most cases this isn't a problem.
    return torch.empty_like(func(*args, **kwargs), device="meta")


def _handle_tril(func, args, kwargs):
    if args and isinstance(args[0], Tensor):
        return torch.empty_like(args[0], device="meta")

    return NotImplemented


class _MetaContext(Tensor):
    """Represents the Python dispatcher to be used with `enable_python_mode()`.

    Note:
        A known limitation of the Python dispatch API is that it requires the
        dispatcher to derive from `Tensor` or from one of its subclassses. In
        our implementation `_MetaContext` is a subclass of `Tensor` solely to
        fulfill this requirement.
    """

    _op_handlers: Dict[Callable, Callable] = {}

    @classmethod
    def _ensure_handlers_initialized(cls) -> None:
        # The `torch.ops` module is only available once the `torch` module is
        # fully imported; therefore, we lazy initialize our handlers.
        if cls._op_handlers:
            return

        cls._op_handlers.update(
            {
                torch.ops.aten.arange: _handle_arange,
                torch.ops.aten.tril: _handle_tril,
            }
        )

    @classmethod
    def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
        cls._ensure_handlers_initialized()

        op_handler: Optional[Callable]

        # Some operators such as `tril()` do not support the meta backend. For
        # such operators we use special handlers.
        try:
            op_handler = cls._op_handlers[func]
        except KeyError:
            op_handler = None

        with _no_dispatch():
            if op_handler:
                result = op_handler(func, args, kwargs)
                # If for some reason the operator handler could not handle the
                # dispatch (e.g. due to unexpected arguments), we fall back to
                # our common handler below; otherwise, we return the result of
                # the handler.
                if result is not NotImplemented:
                    return result

            # We use a simple heuristic. If the function call has a device
            # argument, we simply override it with the meta device.
            if "device" in kwargs:
                kwargs["device"] = torch.device("meta")

            return func(*args, **kwargs)


def init_meta(module_fn: Callable[..., Module], *args, **kwargs) -> Module:
    """Constructs a module on the meta device for inspection purposes.
    This function is meant to be used if the size of a module is too big to fit
    on a single machine and you want to inspect it without instantiating.
    Internally ``init_meta()`` forces all parameters, buffers, and other tensors
    within the scope of the module and its sub-modules to use the meta device
    regardless of the actual device of the tensor, even if that device was
    explicitly passed to the constructor of the tensor. The returned module can
    be used for inspection purposes and afterwards its ``materialize()`` method
    can be called to construct a fully-instantiated module (see the example
    below).
    However note that ``init_meta()`` uses a "best effort" algorithm and is not
    guaranteed to succeed if the underlying module's implementation cannot be
    mapped to the meta device. If you are the module author, you can use the
    ``is_meta_init()`` function described below to find out whether your module
    is being instantiated in the scope of an ``init_meta()`` call and use an
    alternate logic if required:
    ::
        class MyModule(Module):
            def __init__(self):
                if torch.distributed.nn.utils.is_meta_init():
                    self.myparam = torch.empty([10,10], device="meta")
                else:
                    self.myparam = load_myparam()
    Args:
        module_fn (Callable[..., Module]):
            The constructor or the factory function of the module.
        args:
            The positional arguments to pass to ``module_fn()``.
        kwargs:
            The keyword arguments to pass to ``module_fn()``.
    Returns:
        A ``torch.nn.Module`` instance on the meta device.
    :Example:
        >>> import torch
        >>> import torch.distributed.nn
        >>> m = torch.distributed.nn.utils.init_meta(torch.nn.Linear, 5, 1)
        >>> m.weight
        Parameter containing:
        tensor(..., device='meta', requires_grad=True)
        >>> m = m.materialize()
        >>> m.weight
        Parameter containing:
        tensor([[-1.4677e+24,  4.5915e-41,  1.4013e-45,  0.0000e+00,
                 -1.4677e+24, 4.5915e-41]], requires_grad=True)
        >>>
        >>> # `init_meta()` overrides even explicitly passed device arguments.
        >>> class MyModule(torch.nn.Module):
        ...     def __init__(self):
        ...         super().__init__()
        ...         self.param = torch.ones([10, 10], device="cpu")
        ...
        >>> m = torch.distributed.nn.utils.init_meta(MyModule)
        >>> m.param
        tensors(..., device='meta', size=(10, 10))
    Note:
        The ``args`` and ``kwargs`` arguments must be treated as immutable by
        the module constructor since they might be used a second time if
        ``materialize()`` is called.
    """

    def create_instance() -> Module:
        return module_fn(*args, **kwargs)

    if _tls.in_call:
        module = create_instance()
    else:
        _tls.in_call = True
        try:
            with enable_python_mode(_MetaContext):
                module = create_instance()
        finally:
            _tls.in_call = False

    module.materialize = create_instance  # type: ignore[assignment]

    return module


def is_meta_init() -> bool:
    """Indicates whether the module is being instantiated by ``init_meta()``."""
    return _tls.in_call


# https://stackoverflow.com/a/63851681/9201239
def get_all_subclasses(cls):
    subclass_list = []

    def recurse(cl):
        for subclass in cl.__subclasses__():
            subclass_list.append(subclass)
            recurse(subclass)

    recurse(cls)

    return set(subclass_list)


def recursively_setattr(root_module: nn.Module, prefix: str, materialized_module: nn.Module):
    *path, name = prefix.split(".")
    for p in path:
        root_module = getattr(root_module, p)

    try:
        index = int(name)
        root_module[index] = materialized_module
    except ValueError:
        setattr(root_module, name, materialized_module)


def materialize_module(root_module: torch.nn.Module):
    """This utility enables to recursively materialize a module and its children."""
    modules = list(root_module.named_modules())[::-1]
    for prefix, mod in modules:
        materialize_fn = getattr(mod, "materialize", None)
        if materialize_fn:
            materialized_module = materialize_fn()
            recursively_setattr(root_module, prefix, materialized_module)


__STORAGE_META__ = {}


def unset_meta_device():
    for mods, subclass, _ in __STORAGE_META__.values():
        for mod in mods:
            setattr(mod, subclass.__name__, subclass)


def set_meta_device():
    """Replace all torch.nn.Module by their meta replacement."""
    for subclass in get_all_subclasses(torch.nn.modules.module.Module):

        if str(subclass) in __STORAGE_META__:
            mods, subclass, meta_class = __STORAGE_META__[str(subclass)]
            for mod in mods:
                setattr(mod, subclass.__name__, meta_class)
            continue

        class _MetaClass(subclass):
            def __new__(cls, *args, **kwargs):
                subclass = cls.__bases__[0]
                submodules = subclass.__module__.split(".")
                mod = importlib.import_module(submodules[0])
                for name in submodules[1:]:
                    mod = getattr(mod, name)
                setattr(mod, subclass.__name__, subclass)
                obj = init_meta(subclass, *args, **kwargs)

                obj._materialize = obj.materialize

                def materialize():
                    nonlocal obj
                    setattr(mod, subclass.__name__, subclass)
                    obj = obj._materialize()
                    setattr(mod, subclass.__name__, cls)
                    return obj

                obj.materialize = materialize
                setattr(mod, subclass.__name__, cls)
                return obj

        def search(mod):
            out = []
            for _, obj in inspect.getmembers(mod):
                if obj == subclass:
                    out.append(mod)
            return out

        submodules = subclass.__module__.split(".")
        mod = importlib.import_module(submodules[0])

        out = []
        out.append(search(mod))
        for name in submodules[1:]:
            mod = getattr(mod, name)
            out.append(search(mod))

        mods = [mod for mod in chain(*out) if mod]

        __STORAGE_META__[str(subclass)] = (mods, subclass, _MetaClass)

        for mod in mods:
            setattr(mod, subclass.__name__, _MetaClass)


def set_device(device: torch.DeviceObjType):
    if device.type == "meta":
        set_meta_device()
    else:
        unset_meta_device()
        if device.type == "cuda":
            torch.cuda.set_device(device)