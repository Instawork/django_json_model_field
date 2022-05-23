def get_type_init_with_metaclass(cls: type) -> type:
    # Calling type(cls) allows the resulting type to be used to create a type that has the same metaclass as cls.
    return type(cls)
