import copy
import inspect
from itertools import chain
from typing import Any

from django.core.exceptions import FieldDoesNotExist, FieldError
from django.db.models import Model
from django.db.models.base import ModelBase

from .json_model_options import JSONModelOptions


def _has_contribute_to_class(value):
    # Only call contribute_to_class() if it's bound.
    return not inspect.isclass(value) and hasattr(value, "contribute_to_class")


class JSONModelBase(type):
    """
    Metaclass for all JSON Models.

    Adapted from Django's ModelBase class.

    Django uses metaclasses to accomplish some of the "magic" functionality related to fields. This is how fields can
    be defined as class attributes, but function as properties on an instance of the model class.

    The metaclass is used to inspect the way the model is defined, including collecting declaration data from super-
    classes (not currently supported for JSONModel). It actually creates a new type of the same name that is used for
    creating instances of the model. Fields implement contribute_to_class in order to control how their values are
    accessed from model instances.

    This information is then put into an instance of the "options" class, which is set as the _meta attribute of the
    final model type.
    """

    def __new__(mcs, name, bases, attrs, **kwargs):
        # Adapted from ModelBase.__new__ to omit logic related to subclassing / abstract classes, relation fields, and
        # proxy models.

        super_new = super().__new__

        # Also ensure initialization is only performed for subclasses of Model
        # (excluding Model class itself).
        parents = [b for b in bases if isinstance(b, JSONModelBase)]
        if not parents:
            return super_new(mcs, name, bases, attrs)

        # Create the class.
        module = attrs.pop("__module__")
        new_attrs = {"__module__": module}
        classcell = attrs.pop("__classcell__", None)
        if classcell is not None:
            new_attrs["__classcell__"] = classcell
        attr_meta = attrs.pop("Meta", None)
        # Pass all attrs without a (Django-specific) contribute_to_class()
        # method to type.__new__() so that they're properly initialized
        # (i.e. __set_name__()).
        contributable_attrs = {}
        for obj_name, obj in list(attrs.items()):
            if _has_contribute_to_class(obj):
                contributable_attrs[obj_name] = obj
            else:
                new_attrs[obj_name] = obj
        new_class = super_new(mcs, name, bases, new_attrs, **kwargs)

        abstract = getattr(attr_meta, "abstract", False)
        meta = attr_meta or getattr(new_class, "Meta", None)
        base_meta = getattr(new_class, "_meta", None)

        # skipping ModelBase logic related to app_label since it's not used (Django uses it for its model registry)

        new_class.add_to_class("_meta", JSONModelOptions(meta))
        if not abstract and base_meta and not base_meta.abstract:
            # Non-abstract child classes inherit some attributes from their
            # non-abstract parent (unless an ABC comes before it in the
            # method resolution order).
            if not hasattr(meta, "ordering"):
                new_class._meta.ordering = base_meta.ordering

        # Add remaining attributes (those with a contribute_to_class() method)
        # to the class.
        for obj_name, obj in contributable_attrs.items():
            new_class.add_to_class(obj_name, obj)

        # All the fields of any type declared on this model
        new_fields = chain(
            new_class._meta.local_fields, new_class._meta.local_many_to_many, new_class._meta.private_fields
        )
        field_names = {f.name for f in new_fields}

        # skipping ModelBase logic related to proxying
        new_class._meta.concrete_model = new_class

        # skipping ModelBase logic related to parent links (relations)

        # Track fields inherited from base models.
        inherited_attributes = set()
        # Do the appropriate setup for any model parents.
        for base in new_class.mro():
            if base not in parents or not hasattr(base, "_meta"):
                # Things without _meta aren't functional models, so they're
                # uninteresting parents.
                inherited_attributes.update(base.__dict__)
                continue

            parent_fields = base._meta.local_fields + base._meta.local_many_to_many
            if not base._meta.abstract:
                # Check for clashes between locally declared fields and those
                # on the base classes.
                for field in parent_fields:
                    if field.name in field_names:
                        raise FieldError(
                            "Local field %r in class %r clashes with field of "
                            "the same name from base class %r."
                            % (
                                field.name,
                                name,
                                base.__name__,
                            )
                        )
                    else:
                        inherited_attributes.add(field.name)

                # Concrete classes...
                base = base._meta.concrete_model

                # skipping ModelBase logic related to ptr fields for parent links

                # IMPORTANT: parents[base] entry is required to create a link between the class and parents that
                #            define fields. It is None JSONModel inheritance works more like regular OOP inheritance,
                #            where with Django Model classes, it can require joining across multiple tables (e.g in
                #            the case of a Model subclassing another non-abstract Model).
                new_class._meta.parents[base] = None
            else:
                base_parents = base._meta.parents.copy()

                # Add fields from abstract base class if it wasn't overridden.
                for field in parent_fields:
                    if (
                        field.name not in field_names
                        and field.name not in new_class.__dict__
                        and field.name not in inherited_attributes
                    ):
                        new_field = copy.deepcopy(field)
                        new_class.add_to_class(field.name, new_field)
                        # Replace parent links defined on this base by the new
                        # field. It will be appropriately resolved if required.
                        if field.one_to_one:
                            for parent, parent_link in base_parents.items():
                                if field == parent_link:
                                    base_parents[parent] = new_field

                # Pass any non-abstract parent classes onto child.
                new_class._meta.parents.update(base_parents)

            # Inherit private fields (like GenericForeignKey) from the parent
            # class
            for field in base._meta.private_fields:
                if field.name in field_names:
                    if not base._meta.abstract:
                        raise FieldError(
                            "Local field %r in class %r clashes with field of "
                            "the same name from base class %r."
                            % (
                                field.name,
                                name,
                                base.__name__,
                            )
                        )
                else:
                    field = copy.deepcopy(field)
                    if not base._meta.abstract:
                        field.mti_inherited = True
                    new_class.add_to_class(field.name, field)

        return new_class

    def add_to_class(cls, name, value):
        ModelBase.add_to_class(cls, name, value)

    def _prepare(cls):
        ModelBase._prepare(cls)


class JSONModel(metaclass=JSONModelBase):
    """
    Base class for models used with BaseJSONModelField.

    Adapted from Django's Model class.
    """

    _meta: JSONModelOptions
    _selector_value: Any = None

    def __init__(self, _has_input: bool = None, _skip_clean: bool = False, **data):
        """
        Initializes an instance either with an initial set of data that must include values for required fields, or as
        a completely empty instance (as is done for ModelForm instances used to create new objects)

        Arguments:
            _has_input - for internal use - allows the initializer to distinguish between being invoked with no data
                         (effectively an uninitialized instance), and being invoked with an empty data mapping
            _skip_clean - for internal use - when True, skips validation if input is provided. Used when initializing
                          objects from DB JSON data to prevent malformed data from breaking object loading.
        """

        if _has_input is None:
            _has_input = bool(data)

        if not _has_input:
            self._init_field_defaults()
            return

        self._init_fields(data)

        if not _skip_clean:
            # clean is only run if values are provided, otherwise it will throw validation errors for any required
            # fields and break forms for new objects
            self.full_clean()

    def _init_fields(self, initial: dict) -> None:
        """
        Iterates through all defined fields and sets a value from the `initial` dict, defaulting to None.

        This ensures that the DeferredAttribute accessor added to the model instance for accessing field values is
        replaced; if it is not replaced, it will raise an error if anything attempts to access the field's value.

        Parameters
        ----------
        initial
        """

        # Iterate through the list of fields rather than data keys so that a None value is set for any missing fields
        for field in self._meta.fields:
            value = field.to_python(str(initial[field.name])) if field.name in initial else field.get_default()
            setattr(self, field.name, value)

    def _init_field_defaults(self) -> None:
        for field in self._meta.fields:
            if field.has_default():
                setattr(self, field.name, field.get_default())

    def _ensure_fields_initialized(self, data: dict = None) -> None:
        """
        Similar to `_init_fields`, ensures all fields have a value set to prevent errors when attempting to use their
        field accessor. Unlike _init_fields, only sets a None value if no value has been defined yet.
        """

        for field in self._meta.fields:
            try:
                getattr(self, field.name)
            except FieldDoesNotExist:
                # field value has not been set yet, still using DeferredAttribute
                setattr(self, field.name, data.get(field.name) if data else None)

    ##############################
    # adaptations of Model methods
    ##############################

    @classmethod
    def check(cls, **kwargs):
        # adapted from Model.check - most checks from Model.check aren't needed here since they deal with features not
        # supported for JSONModel
        errors = [
            *cls._check_fields(**kwargs),
        ]

        return errors

    @classmethod
    def _check_fields(cls, **kwargs):
        """Perform all field checks."""

        # adapted from Model._check_fields - only local fields need to be checked, no other types of fields are
        # supported

        errors = []
        for field in cls._meta.local_fields:
            errors.extend(field.check(**kwargs))
        return errors

    ###############################################################################################################
    # passthrough methods - these can be used as-is from Django's Model class directly rather than copy/pasting the
    # code
    ###############################################################################################################

    def full_clean(self, exclude=None, *args, **kwargs):
        self._ensure_fields_initialized()
        return Model.full_clean(self, exclude, validate_unique=False)

    def clean_fields(self, exclude=None):
        return Model.clean_fields(self, exclude)

    def _get_FIELD_display(self, field):
        # used by fields that allow "choices"
        return Model._get_FIELD_display(self, field)

    def clean(self):
        """
        Hook for doing any extra model-wide validation after clean() has been
        called on every field by self.clean_fields. Any ValidationError raised
        by this method will not be associated with a particular field; it will
        have a special-case association with the field defined by NON_FIELD_ERRORS.
        """
        pass

    ###################################################################################################################
    # placeholder methods - these are methods used by Django Models that pertain to functionality not needed or not
    #                       supported by JSONModel. They are provided as noops so to allow JSONModel classes to be used
    #                       interchangeably with Django Model classes for ModelForms and ModelAdmin.
    ###################################################################################################################

    def validate_unique(self, exclude=None):
        pass
