import sys
from typing import Callable, List, Optional, Tuple, cast
from typing_extensions import Literal
from typing import Type as typing_Type
from mypy.plugin import Plugin, FunctionContext, MethodContext, CheckerPluginInterface
from mypy.nodes import (
    ARG_POS,
    ARG_STAR,
    TypeInfo,
    Context,
    FuncDef,
    StrExpr,
    IntExpr,
    Expression,
)
from mypy.types import (
    Type,
    CallableType,
    NoneTyp,
    Overloaded,
    TypeVarDef,
    TypeVarType,
    Instance,
    UnionType,
    UninhabitedType,
    AnyType,
    TypeOfAny,
)
from mypy.checker import TypeChecker


class TrioPlugin(Plugin):
    def get_function_hook(
        self, fullname: str
    ) -> Optional[Callable[[FunctionContext], Type]]:
        if fullname in (
            "contextlib.asynccontextmanager",
            "async_generator.asynccontextmanager",
        ):
            return args_invariant_decorator_callback
        if fullname == "trio.open_file":
            return open_file_callback
        if fullname == "trio_typing.takes_callable_and_args":
            return takes_callable_and_args_callback
        if fullname == "async_generator.async_generator":
            return async_generator_callback
        if fullname == "async_generator.yield_":
            return yield_callback
        if fullname == "async_generator.yield_from_":
            return yield_from_callback
        return None

    def get_method_hook(
        self, fullname: str
    ) -> Optional[Callable[[MethodContext], Type]]:
        if fullname == "trio_typing.TaskStatus.started":
            return started_callback
        if fullname == "trio.Path.open":
            return open_method_callback
        return None


def args_invariant_decorator_callback(ctx: FunctionContext) -> Type:
    """Infer a better return type for @asynccontextmanager,
    @async_generator, and other decorators that affect the return
    type but not the argument types of the function they decorate.
    """
    # (adapted from the @contextmanager support in mypy's builtin plugin)
    if ctx.arg_types and len(ctx.arg_types[0]) == 1:
        arg_type = ctx.arg_types[0][0]
        if isinstance(arg_type, CallableType) and isinstance(
            ctx.default_return_type, CallableType
        ):
            return ctx.default_return_type.copy_modified(
                arg_types=arg_type.arg_types,
                arg_kinds=arg_type.arg_kinds,
                arg_names=arg_type.arg_names,
                variables=arg_type.variables,
                is_ellipsis_args=arg_type.is_ellipsis_args,
            )
    return ctx.default_return_type


def open_return_type(
    api: CheckerPluginInterface, args: List[List[Expression]]
) -> Optional[Type]:
    def return_type(word: Literal["Text", "Buffered", "Raw"]) -> Type:
        return api.named_generic_type(
            "typing.Awaitable",
            [api.named_generic_type("trio._Async{}IOBase".format(word), [])],
        )

    if len(args) < 2 or len(args[1]) == 0:
        # If mode is unspecified, the default is text
        return return_type("Text")

    if len(args[1]) == 1:
        # Mode was specified
        mode_arg = args[1][0]
        if isinstance(mode_arg, StrExpr):
            # Mode is a string constant
            if "b" not in mode_arg.value:
                # If there's no "b" in it, it's a text mode
                return return_type("Text")
            # Otherwise it's binary -- determine whether buffered or not
            if len(args) >= 3 and len(args[2]) == 1:
                # Buffering was specified
                buffering_arg = args[2][0]
                if isinstance(buffering_arg, IntExpr):
                    # Buffering is a constant -- zero means
                    # unbuffered, otherwise buffered
                    if buffering_arg.value == 0:
                        return return_type("Raw")
                    return return_type("Buffered")
                # Not a constant, so we're not sure which it is.
                options = [
                    api.named_generic_type("trio._AsyncRawIOBase", []),
                    api.named_generic_type("trio._AsyncBufferedIOBase", []),
                ]  # type: List[Type]
                return api.named_generic_type(
                    "typing.Awaitable", [UnionType.make_simplified_union(options)]
                )
            else:
                # Buffering is default if not specified
                return return_type("Buffered")

    # Mode wasn't a constant or we couldn't make sense of it
    return None


def open_file_callback(ctx: FunctionContext) -> Type:
    """Infer a better return type for trio.open_file()."""
    return open_return_type(ctx.api, ctx.args) or ctx.default_return_type


def open_method_callback(ctx: MethodContext) -> Type:
    """Infer a better return type for trio.Path.open()."""

    # Path.open() doesn't take the first (filename) argument of open_file(),
    # so we need to shift by one.
    args_with_path = cast(List[List[Expression]], [[]]) + ctx.args
    return open_return_type(ctx.api, args_with_path) or ctx.default_return_type


def decode_agen_types_from_return_type(
    ctx: FunctionContext, original_async_return_type: Type
) -> Tuple[Type, Type, Type]:
    """Return the yield type, send type, and return type of
    an @async_generator decorated function that was
    originally declared to return ``original_async_return_type``.

    This tries to interpret ``original_async_return_type`` as a union
    between the async generator return type (i.e., the thing actually
    returned by the decorated function, which becomes the value
    associated with a ``StopAsyncIteration`` exception), an optional
    ``trio_typing.YieldType[X]`` where ``X`` is the type of values
    that the async generator yields, and an optional
    ``trio_typing.SendType[Y]`` where ``Y`` is the type of values that
    the async generator expects to be sent. If one of ``YieldType``
    and ``SendType`` is specified, the other is assumed to be None;
    if neither is specified, both are assumed to be Any.
    If ``original_async_return_type`` includes a ``YieldType``
    and/or a ``SendType`` but no actual return type, the return
    is inferred as ``NoReturn``.
    """

    if isinstance(original_async_return_type, UnionType):
        arms = original_async_return_type.items
    else:
        arms = [original_async_return_type]
    yield_type = None  # type: Optional[Type]
    send_type = None  # type: Optional[Type]
    other_arms = []  # type: List[Type]
    try:
        for arm in arms:
            if isinstance(arm, Instance):
                if arm.type.fullname() == "trio_typing.YieldType":
                    if len(arm.args) != 1:
                        raise ValueError("YieldType must take one argument")
                    if yield_type is not None:
                        raise ValueError("YieldType specified multiple times")
                    yield_type = arm.args[0]
                elif arm.type.fullname() == "trio_typing.SendType":
                    if len(arm.args) != 1:
                        raise ValueError("SendType must take one argument")
                    if send_type is not None:
                        raise ValueError("SendType specified multiple times")
                    send_type = arm.args[0]
                else:
                    other_arms.append(arm)
            else:
                other_arms.append(arm)
    except ValueError as ex:
        ctx.api.fail("invalid @async_generator return type: {}".format(ex), ctx.context)
        return (
            AnyType(TypeOfAny.from_error),
            AnyType(TypeOfAny.from_error),
            original_async_return_type,
        )

    if yield_type is None and send_type is None:
        return (
            AnyType(TypeOfAny.unannotated),
            AnyType(TypeOfAny.unannotated),
            original_async_return_type,
        )

    if yield_type is None:
        yield_type = NoneTyp(ctx.context.line, ctx.context.column)
    if send_type is None:
        send_type = NoneTyp(ctx.context.line, ctx.context.column)
    if not other_arms:
        return (
            yield_type,
            send_type,
            UninhabitedType(
                is_noreturn=True, line=ctx.context.line, column=ctx.context.column
            ),
        )
    else:
        return (
            yield_type,
            send_type,
            UnionType.make_simplified_union(
                other_arms, ctx.context.line, ctx.context.column
            ),
        )


def async_generator_callback(ctx: FunctionContext) -> Type:
    """Handle @async_generator.

    This moves the yield type and send type declarations from
    the return type of the decorated function to the appropriate
    type parameters of ``trio_typing.CompatAsyncGenerator``.
    That is, if you say::

        @async_generator
        async def example() -> Union[str, YieldType[bool], SendType[int]]:
            ...

    then the decorated ``example()`` will return values of type
    ``CompatAsyncGenerator[bool, int, str]``, as opposed to
    ``CompatAsyncGenerator[Any, Any, Union[str,
    YieldType[bool], SendType[int]]`` without the plugin.
    """

    # Apply the common logic to not change the arguments of the
    # decorated function
    new_return_type = args_invariant_decorator_callback(ctx)
    if (
        isinstance(new_return_type, CallableType)
        and isinstance(new_return_type.ret_type, Instance)
        and new_return_type.ret_type.type.fullname()
        == ("trio_typing.CompatAsyncGenerator")
        and len(new_return_type.ret_type.args) == 3
    ):
        return new_return_type.copy_modified(
            ret_type=new_return_type.ret_type.copy_modified(
                args=list(
                    decode_agen_types_from_return_type(
                        ctx, new_return_type.ret_type.args[2]
                    )
                )
            )
        )
    return new_return_type


def decode_enclosing_agen_types(ctx: FunctionContext) -> Tuple[Type, Type]:
    """Return the yield and send types that would be returned by
    decode_agen_types_from_return_type() for the function that's
    currently being typechecked, i.e., the function that contains the
    call described in ``ctx``.
    """
    private_api = cast(TypeChecker, ctx.api)
    enclosing_func = private_api.scope.top_function()
    if (
        enclosing_func is None
        or not isinstance(enclosing_func, FuncDef)
        or not enclosing_func.is_coroutine
        or not enclosing_func.is_decorated
    ):
        # we can't actually detect the @async_generator decorator but
        # we'll at least notice if it couldn't possibly be present
        ctx.api.fail(
            "async_generator.yield_() outside an @async_generator func", ctx.context
        )
        return AnyType(TypeOfAny.from_error), AnyType(TypeOfAny.from_error)

    if (
        isinstance(enclosing_func.type, CallableType)
        and isinstance(enclosing_func.type.ret_type, Instance)
        and enclosing_func.type.ret_type.type.fullname() == "typing.Coroutine"
        and len(enclosing_func.type.ret_type.args) == 3
    ):
        yield_type, send_type, _ = decode_agen_types_from_return_type(
            ctx, enclosing_func.type.ret_type.args[2]
        )
        return yield_type, send_type

    return (
        AnyType(TypeOfAny.implementation_artifact),
        AnyType(TypeOfAny.implementation_artifact),
    )


def yield_callback(ctx: FunctionContext) -> Type:
    """Provide a more specific argument and return type for yield_()
    inside an @async_generator.
    """
    if len(ctx.arg_types) == 0:
        arg_type = NoneTyp(ctx.context.line, ctx.context.column)  # type: Type
    elif ctx.arg_types and len(ctx.arg_types[0]) == 1:
        arg_type = ctx.arg_types[0][0]
    else:
        return ctx.default_return_type

    private_api = cast(TypeChecker, ctx.api)
    yield_type, send_type = decode_enclosing_agen_types(ctx)
    if yield_type is not None and send_type is not None:
        private_api.check_subtype(
            subtype=arg_type,
            supertype=yield_type,
            context=ctx.context,
            subtype_label="yield_ argument",
            supertype_label="declared YieldType",
        )
        return ctx.api.named_generic_type("typing.Awaitable", [send_type])

    return ctx.default_return_type


def yield_from_callback(ctx: FunctionContext) -> Type:
    """Provide a better typecheck for yield_from_()."""
    if ctx.arg_types and len(ctx.arg_types[0]) == 1:
        arg_type = ctx.arg_types[0][0]
    else:
        return ctx.default_return_type

    private_api = cast(TypeChecker, ctx.api)
    our_yield_type, our_send_type = decode_enclosing_agen_types(ctx)
    if our_yield_type is None or our_send_type is None:
        return ctx.default_return_type

    if (
        isinstance(arg_type, Instance)
        and arg_type.type.fullname()
        in (
            "trio_typing.CompatAsyncGenerator",
            "trio_typing.AsyncGenerator",
            "typing.AsyncGenerator",
        )
        and len(arg_type.args) >= 2
    ):
        their_yield_type, their_send_type = arg_type.args[:2]
        private_api.check_subtype(
            subtype=their_yield_type,
            supertype=our_yield_type,
            context=ctx.context,
            subtype_label="yield_from_ argument YieldType",
            supertype_label="local declared YieldType",
        )
        private_api.check_subtype(
            subtype=our_send_type,
            supertype=their_send_type,
            context=ctx.context,
            subtype_label="local declared SendType",
            supertype_label="yield_from_ argument SendType",
        )
    elif isinstance(arg_type, Instance):
        private_api.check_subtype(
            subtype=arg_type,
            supertype=ctx.api.named_generic_type(
                "typing.AsyncIterable", [our_yield_type]
            ),
            context=ctx.context,
            subtype_label="yield_from_ argument type",
            supertype_label="expected iterable type",
        )

    return ctx.default_return_type


def started_callback(ctx: MethodContext) -> Type:
    """Raise an error if task_status.started() is called without an argument
    and the TaskStatus is not declared to accept a result of type None.
    """
    if (
        (not ctx.arg_types or not ctx.arg_types[0])
        and isinstance(ctx.type, Instance)
        and ctx.type.args
        and not isinstance(ctx.type.args[0], NoneTyp)
    ):
        ctx.api.fail(
            "TaskStatus.started() requires an argument for types other than "
            "TaskStatus[None]",
            ctx.context,
        )
    return ctx.default_return_type


def takes_callable_and_args_callback(ctx: FunctionContext) -> Type:
    """Automate the boilerplate for writing functions that accept
    arbitrary positional arguments of the same type accepted by
    a callable.

    For example, this supports writing::

        @trio_typing.takes_callable_and_args
        def start_soon(
            self,
            async_fn: Callable[[trio_typing.ArgsForCallable], None],
            *args: trio_typing.ArgsForCallable,
        ) -> None: ...

    instead of::

        T1 = TypeVar("T1")
        T2 = TypeVar("T2")
        T3 = TypeVar("T3")
        T4 = TypeVar("T4")

        @overload
        def start_soon(
            self,
            async_fn: Callable[[], None],
        ) -> None: ...

        @overload
        def start_soon(
            self,
            async_fn: Callable[[T1], None],
            __arg1: T1,
        ) -> None: ...

        @overload
        def start_soon(
            self,
            async_fn: Callable[[T1, T2], None],
            __arg1: T1,
            __arg2: T2
        ) -> None: ...

        # etc

    """
    try:
        if (
            not ctx.arg_types
            or len(ctx.arg_types[0]) != 1
            or not isinstance(ctx.arg_types[0][0], CallableType)
            or not isinstance(ctx.default_return_type, CallableType)
        ):
            raise ValueError("must be used as a decorator")

        fn_type = ctx.arg_types[0][0]  # type: CallableType
        callable_idx = -1  # index in function arguments of the callable
        callable_args_idx = -1  # index in callable arguments of the StarArgs
        args_idx = -1  # index in function arguments of the StarArgs

        for idx, (kind, ty) in enumerate(zip(fn_type.arg_kinds, fn_type.arg_types)):
            if (
                isinstance(ty, Instance)
                and ty.type.fullname() == "trio_typing.ArgsForCallable"
            ):
                if kind != ARG_STAR:
                    raise ValueError(
                        "ArgsForCallable must be used with a *args argument "
                        "in the decorated function"
                    )
                assert args_idx == -1
                args_idx = idx
            elif isinstance(ty, CallableType) and kind == ARG_POS:
                for idx_, (kind_, ty_) in enumerate(zip(ty.arg_kinds, ty.arg_types)):
                    if (
                        isinstance(ty_, Instance)
                        and ty_.type.fullname() == "trio_typing.ArgsForCallable"
                    ):
                        if kind != ARG_POS:
                            raise ValueError(
                                "ArgsForCallable must be used with a positional "
                                "argument in the callable type that the decorated "
                                "function takes"
                            )
                        if callable_idx != -1:
                            raise ValueError(
                                "ArgsForCallable may only be used once as the type "
                                "of an argument to a callable type that the "
                                "decorated function takes"
                            )
                        callable_idx = idx
                        callable_args_idx = idx_
        if args_idx == -1:
            raise ValueError(
                "decorated function must take *args with type "
                "trio_typing.ArgsForCallable"
            )
        if callable_idx == -1:
            raise ValueError(
                "decorated function must take a callable that has an "
                "argument of type trio_typing.ArgsForCallable"
            )

        expanded_fns = []  # type: List[CallableType]
        type_var_defs = []  # type: List[TypeVarDef]
        type_var_types = []  # type: List[Type]
        for arg_idx in range(1, 5):
            callable_ty = cast(CallableType, fn_type.arg_types[callable_idx])
            arg_types = list(fn_type.arg_types)
            arg_types[callable_idx] = callable_ty.copy_modified(
                arg_types=(
                    callable_ty.arg_types[:callable_args_idx]
                    + type_var_types
                    + callable_ty.arg_types[callable_args_idx + 1 :]
                ),
                arg_kinds=(
                    callable_ty.arg_kinds[:callable_args_idx]
                    + ([ARG_POS] * len(type_var_types))
                    + callable_ty.arg_kinds[callable_args_idx + 1 :]
                ),
                arg_names=(
                    callable_ty.arg_names[:callable_args_idx]
                    + ([None] * len(type_var_types))
                    + callable_ty.arg_names[callable_args_idx + 1 :]
                ),
                variables=(callable_ty.variables + type_var_defs),
            )
            expanded_fns.append(
                fn_type.copy_modified(
                    arg_types=(
                        arg_types[:args_idx]
                        + type_var_types
                        + arg_types[args_idx + 1 :]
                    ),
                    arg_kinds=(
                        fn_type.arg_kinds[:args_idx]
                        + ([ARG_POS] * len(type_var_types))
                        + fn_type.arg_kinds[args_idx + 1 :]
                    ),
                    arg_names=(
                        fn_type.arg_names[:args_idx]
                        + ([None] * len(type_var_types))
                        + fn_type.arg_names[args_idx + 1 :]
                    ),
                    variables=(fn_type.variables + type_var_defs),
                )
            )
            type_var_defs.append(
                TypeVarDef(
                    "__T{}".format(arg_idx),
                    "__T{}".format(arg_idx),
                    -len(fn_type.variables) - arg_idx - 1,
                    [],
                    ctx.api.named_generic_type("builtins.object", []),
                )
            )
            type_var_types.append(
                TypeVarType(type_var_defs[-1], ctx.context.line, ctx.context.column)
            )
        return Overloaded(expanded_fns)

    except ValueError as ex:
        ctx.api.fail(
            "invalid use of @takes_callable_and_args: {}".format(ex), ctx.context
        )
        return ctx.default_return_type


def plugin(version: str) -> typing_Type[Plugin]:
    return TrioPlugin
