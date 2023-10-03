def assert_raises(excClass, callableObj, *args, **kwargs):
    """
    Like unittest.TestCase.assertRaises, but returns the exception.
    """
    try:
        callableObj(*args, **kwargs)
    except excClass as e:
        return e
    else:
        excName = excClass.__name__ if hasattr(excClass,'__name__') else str(excClass)
        raise AssertionError(f"{excName} not raised")
