from __future__ import absolute_import, division, print_function

import string
# Note: this requires adding `future` to the test requirements!
from builtins import int as py3int
from collections import defaultdict

import numpy as np
import numpy.lib.function_base as npfb

import hypothesis.extra.gufunc as gu
from hypothesis import given
from hypothesis.extra.numpy import from_dtype, scalar_dtypes
from hypothesis.strategies import (
    booleans,
    composite,
    data,
    dictionaries,
    from_regex,
    integers,
    just,
    lists,
    sampled_from,
    tuples,
)

NP_BROADCASTABLE = ((np.matmul, "(n,m),(m,p)->(n,p)"),
                    (np.add, "(),()->()"),
                    (np.multiply, "(),()->()"))


# Also include if function can handle axis=None
NP_AXIS = ((np.sum, "(n)->()", True),
           (np.cumsum, "(n)->(n)", True),
           (np.percentile, "(n),()->()", True),
           (np.diff, "(n)->(m)", False),
           (np.diff, "(n),()->(m)", False))

# The spec for a dimension name in numpy.lib.function_base is r'\A\w+\Z' but
# this creates too many weird corner cases on Python3 unicode. Also make sure
# doesn't start with digits because if it is parsed as number we could end up
# with very large dimensions that blow out memory.
VALID_DIM_NAMES = r"\A[a-zA-Z_][a-zA-Z0-9_]*\Z"

_st_shape = lists(integers(min_value=0, max_value=5),
                  min_size=0, max_size=3).map(tuple)


def identity(x):
    return x


def check_int(x):
    """Use subroutine for this so in Py3 we can remove the `long`."""
    # Could also do ``type(x) in (int, long)``, but only on Py2.
    assert isinstance(x, py3int)


def pad_left(L, size, padding):
    L = (padding,) * max(0, size - len(L)) + L
    return L


def validate_elements(L, dtype, unique=False, choices=None):
    for drawn in L:
        assert drawn.dtype == np.dtype(dtype)

        if unique:
            assert len(set(drawn.ravel())) == drawn.size

        if choices is not None:
            assert drawn.dtype == choices.dtype
            vals = set(drawn.ravel())
            assert vals.issubset(choices)


def validate_shapes(L, parsed_sig, min_side, max_side):
    assert type(L) == list
    assert len(parsed_sig) == len(L)
    size_lookup = {}
    for spec, drawn in zip(parsed_sig, L):
        assert type(drawn) == tuple
        assert len(spec) == len(drawn)
        for ss, dd in zip(spec, drawn):
            check_int(dd)
            if ss.isdigit():
                assert int(ss) == dd
            else:
                mm = min_side.get(ss, 0) if isinstance(min_side, dict) \
                    else min_side
                assert mm <= dd
                mm = max_side.get(ss, gu.DEFAULT_MAX_SIDE) \
                    if isinstance(max_side, dict) else max_side
                assert dd <= mm
                var_size = size_lookup.setdefault(ss, dd)
                assert var_size == dd


def validate_bcast_shapes(shapes, parsed_sig,
                          min_side, max_side, max_dims_extra):
    assert all(len(ss) <= gu.GLOBAL_DIMS_MAX for ss in shapes)

    # chop off extra dims then same as gufunc_shape
    core_dims = [tt[len(tt) - len(pp):] for tt, pp in zip(shapes, parsed_sig)]
    validate_shapes(core_dims, parsed_sig, min_side, max_side)

    # check max_dims_extra
    b_dims = [tt[:len(tt) - len(pp)] for tt, pp in zip(shapes, parsed_sig)]
    assert all(len(tt) <= max_dims_extra for tt in b_dims)

    # Convert dims to matrix form
    b_dims2 = np.array([pad_left(bb, max_dims_extra, 1) for bb in b_dims],
                       dtype=int)
    # The extra broadcast dims be set to one regardless of min, max sides
    mm = min_side.get(gu.BCAST_DIM, 0) \
        if isinstance(min_side, dict) else min_side
    assert np.all((b_dims2 == 1) | (mm <= b_dims2))
    mm = max_side.get(gu.BCAST_DIM, gu.DEFAULT_MAX_SIDE) \
        if isinstance(max_side, dict) else max_side
    assert np.all((b_dims2 == 1) | (b_dims2 <= mm))
    # make sure 1 or same
    for ii in range(max_dims_extra):
        vals = set(b_dims2[:, ii])
        # Must all be 1 or a const size
        assert len(vals) <= 2
        assert len(vals) < 2 or (1 in vals)


def unparse(parsed_sig):
    assert len(parsed_sig) > 0, "gufunc sig does not support no argument funcs"

    sig = [",".join(vv) for vv in parsed_sig]
    sig = "(" + "),(".join(sig) + ")"
    return sig


def real_scalar_dtypes():
    def to_native(dtype):
        tt = dtype.type
        # Only keep if invertible
        tt = tt if np.dtype(tt) == dtype else dtype
        return tt

    def cast_it(args):
        return args[0](args[1])

    S = tuples(sampled_from((str, identity, to_native)), scalar_dtypes())
    S = S.map(cast_it)
    return S


def real_from_dtype(dtype, N=10):
    dtype = np.dtype(dtype)

    def clean_up(x):
        x = np.nan_to_num(x).astype(dtype)
        assert x.dtype == dtype  # hard to always get this it seems
        return x
    S = lists(from_dtype(dtype), min_size=N, max_size=N).map(clean_up)
    return S


def parsed_sigs(max_dims=3, max_args=5):
    """Strategy to generate a parsed gufunc signature.

    Note that in general functions can take no-args, but the function signature
    formalism is for >= 1 args. So, there is always at least 1 arg here.
    """
    # Use | to sample from digits since we would like (small) pure numbers too
    shapes = lists(from_regex(VALID_DIM_NAMES) | sampled_from(string.digits),
                   min_size=0, max_size=max_dims).map(tuple)
    S = lists(shapes, min_size=1, max_size=max_args)
    return S


@composite
def parsed_sigs_and_sizes(draw, min_min_side=0, max_max_side=5, **kwargs):
    parsed_sig = draw(parsed_sigs(**kwargs))
    # list of all labels used in sig, includes ints which is ok to include in
    # dict as distractors.
    labels = list(set([k for arg in parsed_sig for k in arg]))
    # Also sometimes put the broadcast flag in as label
    labels.append(gu.BCAST_DIM)

    # Using a split to decide which numbers we use for min sides and which
    # numbers we use for max side, to avoid min > max. This strategy does not
    # cover whole search space, but should should be good enough.
    split = draw(integers(min_min_side, gu.DEFAULT_MAX_SIDE))

    if draw(booleans()):
        min_side = draw(dictionaries(sampled_from(labels),
                                     integers(min_min_side, split)))
    else:
        min_side = draw(integers(min_min_side, split))

    if draw(booleans()):
        max_side = draw(dictionaries(sampled_from(labels),
                                     integers(split, max_max_side)))
    else:
        max_side = draw(integers(split, max_max_side))

    return parsed_sig, min_side, max_side


@given(dictionaries(from_regex(VALID_DIM_NAMES), integers()),
       integers(), integers())
def test_ddict_int_or_dict(D, default_val, default_val2):
    DD = defaultdict(lambda: default_val, D)

    DD2 = gu._int_or_dict(DD, default_val2)

    # just pass thru
    assert DD is DD2
    # default_val2 is ignored
    assert DD2.default_factory() == default_val


@given(dictionaries(from_regex(VALID_DIM_NAMES), integers()), integers())
def test_dict_int_or_dict(D, default_val):
    DD = gu._int_or_dict(D, default_val)

    assert DD == D
    assert DD['---'] == default_val


@given(integers(), integers())
def test_int_int_or_dict(default_val, default_val2):
    DD = gu._int_or_dict(default_val, default_val2)

    assert len(DD) == 0
    assert DD['---'] == default_val


@given(real_scalar_dtypes(), _st_shape, data())
def test_arrays(dtype, shape, data):
    # unique argument to arrays gets tested in the tuple of arrays tests
    choices = data.draw(real_from_dtype(dtype))

    elements = sampled_from(choices)
    S = gu._arrays(dtype, shape, elements)
    X = data.draw(S)

    assert np.shape(X) == shape
    validate_elements([X], dtype=dtype, choices=choices)

    assert type(X) == np.ndarray


# hypothesis.extra.numpy.array_shapes does not support 0 min_size so we roll
# our own in this case.
@given(lists(_st_shape, min_size=0, max_size=5),
       real_scalar_dtypes(), booleans(), data())
def test_shapes_tuple_of_arrays(shapes, dtype, unique, data):
    elements = from_dtype(np.dtype(dtype))

    S = gu._tuple_of_arrays(shapes, dtype, elements=elements, unique=unique)
    X = data.draw(S)

    validate_elements(X, dtype=dtype, unique=unique)

    assert len(shapes) == len(X)
    for spec, drawn in zip(shapes, X):
        assert tuple(spec) == np.shape(drawn)


@given(lists(_st_shape, min_size=0, max_size=5), real_scalar_dtypes(), data())
def test_elements_tuple_of_arrays(shapes, dtype, data):
    choices = data.draw(real_from_dtype(dtype))

    elements = sampled_from(choices)
    S = gu._tuple_of_arrays(shapes, dtype, elements=elements)
    X = data.draw(S)

    validate_elements(X, choices=choices, dtype=dtype)


@given(gu.gufunc_broadcast('(1),(1),(1),()->()',
                           dtype=['object', 'object', 'object', 'bool'],
                           elements=[_st_shape,
                                     scalar_dtypes(), just(None), booleans()],
                           min_side=1, max_dims_extra=1), data())
def test_bcast_tuple_of_arrays(args, data):
    '''Now testing broadcasting of tuple_of_arrays, kind of crazy since it uses
    gufuncs to test itself. Some awkwardness here since there are a lot of
    corner cases when dealing with object types in the numpy extension.

    For completeness, should probably right a function like this for the other
    functions, but there always just pass dtype, elements, unique to
    `_tuple_of_arrays` anyway, so this should be pretty good.
    '''
    shapes, dtype, elements, unique = args

    shapes = shapes.ravel()
    # Need to squeeze out due to weird behaviour of object
    dtype = np.squeeze(dtype, -1)
    elements = np.squeeze(elements, -1)

    elements_shape = max(dtype.shape, elements.shape)
    dtype_ = np.broadcast_to(dtype, elements_shape)
    if elements_shape == ():
        elements = from_dtype(dtype_.item())
    else:
        elements = [from_dtype(dd) for dd in dtype_]

    shapes_shape = max(shapes.shape, dtype.shape, elements_shape, unique.shape)
    shapes = np.broadcast_to(shapes, shapes_shape)

    S = gu._tuple_of_arrays(shapes, dtype, elements=elements, unique=unique)
    X = data.draw(S)

    assert len(shapes) == len(X)
    for spec, drawn in zip(shapes, X):
        assert tuple(spec) == np.shape(drawn)

    for ii, xx in enumerate(X):
        dd = dtype[ii] if dtype.size > 1 else dtype.item()
        uu = unique[ii] if unique.size > 1 else unique.item()
        validate_elements([xx], dtype=dd, unique=uu)


@given(parsed_sigs(), parsed_sigs())
def test_unparse_parse(i_parsed_sig, o_parsed_sig):
    # Make sure all hard coded literals within specified size
    # This is actually testing parsed_sigs().
    labels = list(set([k for arg in i_parsed_sig for k in arg]))
    assert all((not k.isdigit()) or (int(k) < 10) for k in labels)

    # We don't care about the output for this function
    signature = unparse(i_parsed_sig) + "->" + unparse(o_parsed_sig)
    # This is a 'private' function of np, so need to test it still works as we
    # think it does.
    inp, out = npfb._parse_gufunc_signature(signature)

    assert i_parsed_sig == inp
    assert o_parsed_sig == out


# Allow bigger sizes since we only generate the shapes and never alloc arrays
@given(parsed_sigs_and_sizes(max_args=10, max_dims=gu.GLOBAL_DIMS_MAX,
                             max_max_side=100), data())
def test_shapes_gufunc_shape(parsed_sig_and_size, data):
    parsed_sig, min_side, max_side = parsed_sig_and_size

    # We don't care about the output for this function
    signature = unparse(parsed_sig) + "->()"

    S = gu.gufunc_shape(signature, min_side=min_side, max_side=max_side)

    shapes = data.draw(S)
    validate_shapes(shapes, parsed_sig, min_side, max_side)


@given(parsed_sigs_and_sizes(), real_scalar_dtypes(), booleans(), data())
def test_shapes_gufunc(parsed_sig_and_size, dtype, unique, data):
    parsed_sig, min_side, max_side = parsed_sig_and_size

    # We don't care about the output for this function
    signature = unparse(parsed_sig) + "->()"

    # We could also test using elements strategy that then requires casting,
    # but that would be kind of complicated to come up with compatible combos
    elements = from_dtype(np.dtype(dtype))

    S = gu.gufunc(signature, min_side=min_side, max_side=max_side,
                  dtype=dtype, elements=elements, unique=unique)

    X = data.draw(S)
    shapes = [np.shape(xx) for xx in X]

    validate_shapes(shapes, parsed_sig, min_side, max_side)
    validate_elements(X, dtype=dtype, unique=unique)


@given(parsed_sigs(max_args=3), integers(0, 5), integers(0, 5),
       real_scalar_dtypes(), data())
def test_elements_gufunc(parsed_sig, min_side, max_side, dtype, data):
    choices = data.draw(real_from_dtype(dtype))
    elements = sampled_from(choices)

    # We don't care about the output for this function
    signature = unparse(parsed_sig) + "->()"

    min_side, max_side = sorted([min_side, max_side])

    S = gu.gufunc(signature, min_side=min_side, max_side=max_side,
                  dtype=dtype, elements=elements)

    X = data.draw(S)

    validate_elements(X, choices=choices, dtype=dtype)


@given(parsed_sigs_and_sizes(max_args=10, max_dims=gu.GLOBAL_DIMS_MAX,
                             max_max_side=100),
       lists(booleans(), min_size=10, max_size=10),
       integers(0, gu.GLOBAL_DIMS_MAX),
       data())
def test_shapes_gufunc_broadcast_shape(parsed_sig_and_size, excluded,
                                       max_dims_extra, data):
    parsed_sig, min_side, max_side = parsed_sig_and_size

    # We don't care about the output for this function
    signature = unparse(parsed_sig) + "->()"

    excluded = excluded[:len(parsed_sig)]
    assert len(excluded) == len(parsed_sig)  # Make sure excluded long enough
    excluded, = np.where(excluded)
    excluded = tuple(excluded)

    S = gu.gufunc_broadcast_shape(signature, excluded=excluded,
                                  min_side=min_side, max_side=max_side,
                                  max_dims_extra=max_dims_extra)

    shapes = data.draw(S)

    validate_bcast_shapes(shapes, parsed_sig,
                          min_side, max_side, max_dims_extra)


@given(parsed_sigs_and_sizes(max_args=3),
       lists(booleans(), min_size=3, max_size=3), integers(0, 3),
       real_scalar_dtypes(), booleans(), data())
def test_shapes_gufunc_broadcast(parsed_sig_and_size, excluded,
                                 max_dims_extra, dtype, unique, data):
    parsed_sig, min_side, max_side = parsed_sig_and_size

    # We don't care about the output for this function
    signature = unparse(parsed_sig) + "->()"

    excluded = excluded[:len(parsed_sig)]
    assert len(excluded) == len(parsed_sig)  # Make sure excluded long enough
    excluded, = np.where(excluded)
    excluded = tuple(excluded)

    elements = from_dtype(np.dtype(dtype))

    S = gu.gufunc_broadcast(signature, excluded=excluded,
                            min_side=min_side, max_side=max_side,
                            max_dims_extra=max_dims_extra,
                            dtype=dtype, elements=elements, unique=unique)

    X = data.draw(S)
    shapes = [np.shape(xx) for xx in X]

    validate_bcast_shapes(shapes, parsed_sig,
                          min_side, max_side, max_dims_extra)
    validate_elements(X, dtype=dtype, unique=unique)


@given(parsed_sigs(max_args=3), lists(booleans(), min_size=3, max_size=3),
       integers(0, 5), integers(0, 5), integers(0, 3), real_scalar_dtypes(),
       data())
def test_elements_gufunc_broadcast(parsed_sig, excluded, min_side, max_side,
                                   max_dims_extra, dtype, data):
    # We don't care about the output for this function
    signature = unparse(parsed_sig) + "->()"

    excluded = excluded[:len(parsed_sig)]
    assert len(excluded) == len(parsed_sig)  # Make sure excluded long enough
    excluded, = np.where(excluded)
    excluded = tuple(excluded)

    min_side, max_side = sorted([min_side, max_side])

    choices = data.draw(real_from_dtype(dtype))
    elements = sampled_from(choices)

    S = gu.gufunc_broadcast(signature, excluded=excluded,
                            min_side=min_side, max_side=max_side,
                            max_dims_extra=max_dims_extra,
                            dtype=dtype, elements=elements)

    X = data.draw(S)

    validate_elements(X, choices=choices, dtype=dtype)


@given(parsed_sigs_and_sizes(max_args=3), parsed_sigs(),
       lists(real_scalar_dtypes(), min_size=3, max_size=3),
       lists(booleans(), min_size=3, max_size=3),
       integers(0, 3), real_scalar_dtypes(), booleans(), data())
def test_shapes_broadcasted(parsed_sig_and_size, o_parsed_sig, otypes,
                            excluded, max_dims_extra, dtype, unique, data):
    parsed_sig, min_side, max_side = parsed_sig_and_size
    signature = unparse(parsed_sig) + "->" + unparse(o_parsed_sig)

    # These are of type np.dtype, but we test use str elsewhere
    otypes = otypes[:len(parsed_sig)]
    assert len(otypes) == len(parsed_sig)  # Make sure otypes long enough

    excluded = excluded[:len(parsed_sig)]
    assert len(excluded) == len(parsed_sig)  # Make sure excluded long enough
    excluded, = np.where(excluded)
    excluded = tuple(excluded)

    elements = from_dtype(np.dtype(dtype))

    def dummy(*args):
        assert False, "this function shouldn't get called"

    S = gu.broadcasted(dummy, signature, otypes=otypes, excluded=excluded,
                       min_side=min_side, max_side=max_side,
                       max_dims_extra=max_dims_extra,
                       itypes=dtype, elements=elements, unique=unique)

    f0, f_vec, X = data.draw(S)

    # First argument is pass thru
    assert f0 is dummy
    assert id(f0) == id(dummy)

    # Second is result of np.vectorize, which we test elsewhere

    # Third same as gufunc_broadcast
    shapes = [np.shape(xx) for xx in X]
    validate_bcast_shapes(shapes, parsed_sig,
                          min_side, max_side, max_dims_extra)
    validate_elements(X, dtype=dtype, unique=unique)


@given(parsed_sigs(max_args=3), parsed_sigs(),
       lists(real_scalar_dtypes(), min_size=3, max_size=3),
       lists(booleans(), min_size=3, max_size=3),
       integers(0, 5), integers(0, 5), integers(0, 3), real_scalar_dtypes(),
       data())
def test_elements_broadcasted(parsed_sig, o_parsed_sig, otypes, excluded,
                              min_side, max_side, max_dims_extra, dtype, data):
    signature = unparse(parsed_sig) + "->" + unparse(o_parsed_sig)

    # These are of type np.dtype, but we test use str elsewhere
    otypes = otypes[:len(parsed_sig)]
    assert len(otypes) == len(parsed_sig)  # Make sure otypes long enough

    excluded = excluded[:len(parsed_sig)]
    assert len(excluded) == len(parsed_sig)  # Make sure excluded long enough
    excluded, = np.where(excluded)
    excluded = tuple(excluded)

    min_side, max_side = sorted([min_side, max_side])

    def dummy(*args):
        assert False, "this function shouldn't get called"

    choices = data.draw(real_from_dtype(dtype))

    elements = sampled_from(choices)
    S = gu.broadcasted(dummy, signature, otypes=otypes, excluded=excluded,
                       min_side=min_side, max_side=max_side,
                       max_dims_extra=max_dims_extra,
                       itypes=dtype, elements=elements)

    f0, f_vec, X = data.draw(S)

    validate_elements(X, choices=choices, dtype=dtype)


@given(integers(0, len(NP_BROADCASTABLE) - 1),
       integers(0, 5), integers(0, 5), integers(0, 3), data())
def test_np_broadcasted(func_choice, min_side, max_side, max_dims_extra, data):
    otype = "int64"

    f, signature = NP_BROADCASTABLE[func_choice]

    min_side, max_side = sorted([min_side, max_side])

    S = gu.broadcasted(f, signature, otypes=[otype],
                       min_side=min_side, max_side=max_side,
                       max_dims_extra=max_dims_extra,
                       itypes=np.int64,
                       elements=integers(min_value=0, max_value=100))

    f0, f_vec, args = data.draw(S)

    assert f0 is f

    R1 = f0(*args)
    R2 = f_vec(*args)
    assert R1.dtype == otype
    assert R2.dtype == otype
    assert np.shape(R1) == np.shape(R2)
    assert np.all(R1 == R2)  # All int so no round off error


@given(integers(0, 5), integers(0, 5), integers(0, 3), data())
def test_np_multi_broadcasted(min_side, max_side, max_dims_extra, data):
    min_side, max_side = sorted([min_side, max_side])

    def multi_out_f(x, y, q):
        """Function should already be fully broadcast compatible."""
        z = np.matmul(x, y)
        R = (z, z + 0.5 * q)
        return R

    signature = "(n,m),(m,p),()->(n,p),(n,p)"
    otypes = ["int64", "float64"]

    S = gu.broadcasted(multi_out_f, signature, otypes=otypes, excluded=(2,),
                       min_side=min_side, max_side=max_side,
                       max_dims_extra=max_dims_extra,
                       itypes=np.int64,
                       elements=integers(min_value=0, max_value=100))

    f0, f_vec, args = data.draw(S)

    assert f0 is multi_out_f
    assert np.shape(args[2]) == (), "argument should be excluded from bcast"

    R1 = f0(*args)
    R2 = f_vec(*args)

    for rr1, rr2, ot in zip(R1, R2, otypes):
        assert rr1.dtype == ot
        assert rr2.dtype == ot
        assert np.shape(rr1) == np.shape(rr2)
        assert np.all(rr1 == rr2)


@given(parsed_sigs_and_sizes(min_min_side=1),
       integers(0, 3), booleans(), real_scalar_dtypes(), booleans(), data())
def test_shapes_axised(parsed_sig_and_size, max_dims_extra,
                       allow_none, dtype, unique, data):
    parsed_sig, min_side, max_side = parsed_sig_and_size
    # First argument must be 1D, this may give extra entries in shape dict,
    # but that is ok since we test that case too then.
    parsed_sig[0] = pad_left(parsed_sig[0], 1, "n")[:1]
    signature = unparse(parsed_sig) + "->()"  # output dims ignored here

    def dummy(*args, **kwargs):
        assert False, "this function shouldn't get called"

    elements = from_dtype(np.dtype(dtype))

    S = gu.axised(dummy, signature, min_side=min_side, max_side=max_side,
                  max_dims_extra=max_dims_extra, allow_none=allow_none,
                  itypes=dtype, elements=elements, unique=unique)

    f0, f_ax, X, axis = data.draw(S)

    # First argument is pass thru
    assert f0 is dummy
    assert id(f0) == id(dummy)

    # Second is result of np.vectorize, which we test elsewhere

    # Third same as gufunc_broadcast
    shapes = [np.shape(xx) for xx in X]
    if axis is None:
        # First arg shape can be arbitrary with axis=None
        assert len(shapes[0]) >= 1
        validate_shapes(shapes[1:], parsed_sig[1:], min_side, max_side)
    else:
        shapes[0] = (X[0].shape[axis],)
        validate_shapes(shapes, parsed_sig, min_side, max_side)

    validate_elements(X, dtype=dtype, unique=unique)

    # Test fourth
    assert allow_none or (axis is not None)


@given(parsed_sigs(), integers(1, 5), integers(1, 5), integers(0, 3),
       booleans(), real_scalar_dtypes(), data())
def test_elements_axised(parsed_sig, min_side, max_side, max_dims_extra,
                         allow_none, dtype, data):
    # First argument must be 1D
    parsed_sig[0] = pad_left(parsed_sig[0], 1, "n")[:1]
    signature = unparse(parsed_sig) + "->()"  # output dims ignored here

    min_side, max_side = sorted([min_side, max_side])

    def dummy(*args, **kwargs):
        assert False, "this function shouldn't get called"

    choices = data.draw(real_from_dtype(dtype))
    elements = sampled_from(choices)

    S = gu.axised(dummy, signature, min_side=min_side, max_side=max_side,
                  max_dims_extra=max_dims_extra, allow_none=allow_none,
                  itypes=dtype, elements=elements)

    f0, f_ax, X, axis = data.draw(S)

    validate_elements(X, choices=choices, dtype=dtype)


@given(integers(0, len(NP_AXIS) - 1),
       integers(1, 5), integers(1, 5), integers(0, 3), data())
def test_np_axised(func_choice, min_side, max_side, max_dims_extra, data):
    f, signature, allow_none = NP_AXIS[func_choice]

    min_side, max_side = sorted([min_side, max_side])

    S = gu.axised(f, signature, min_side=min_side, max_side=max_side,
                  max_dims_extra=max_dims_extra, allow_none=allow_none,
                  itypes=np.int64,
                  elements=integers(min_value=0, max_value=100))

    f0, f_ax, args, axis = data.draw(S)

    assert f0 is f

    R1 = f0(*args, axis=axis)
    R2 = f_ax(*args, axis=axis)
    assert R1.dtype == R2.dtype
    assert np.shape(R1) == np.shape(R2)
    assert np.all(R1 == R2)