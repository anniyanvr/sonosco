import inspect
from typing import Set

from common.constants import CLASS_NAME_FIELD, CLASS_MODULE_FIELD, PRIMITIVES, COLLECTIONS


def get_constructor_args(cls) -> Set[str]:
    """
    E.g.

        class Bar():
                def __init__(self, arg1, arg2):

        get_constructor_args(Bar)
        # returns ['arg1', 'arg2']
    Args:
        cls (object):

    Returns: set containing names of constructor arguments

    """
    return set(inspect.getfullargspec(cls.__init__).args[1:])


def get_class_by_name(name: str) -> type:
    """
    Returns type object of class specified by name
    Args:
        name: full name of the class (with packages)

    Returns: class object

    """
    components = name.split('.')
    mod = __import__(components[0])
    for comp in components[1:]:
        mod = getattr(mod, comp)
    return mod


def is_serialized_collection(obj: object) -> bool:
    """
    Checks if object is a collection

    Args:
        obj: any python object

    Returns: True if object is a collection

    """
    return type(obj) in COLLECTIONS


def is_serialized_primitive(obj: object) -> bool:
    """
    Checks if object is a primitive

    Args:
        obj: any python object

    Returns: True if object is a primitive

    """
    return type(obj) in PRIMITIVES


def is_serialized_type(obj: object) -> bool:
    """
    Checks if object is a type

    Args:
        obj: any python object

    Returns: True if object is a type

    """
    return type(obj) is dict and CLASS_MODULE_FIELD in obj and CLASS_NAME_FIELD in obj


def raise_unsupported_data_type():
    """
    Raises TypeError

    Returns:

    """
    raise TypeError("Unsupported data type. Currently only primitives, lists of primitives and types"
                    "objects can be serialized")