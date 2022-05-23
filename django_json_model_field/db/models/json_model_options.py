import inspect
from collections import OrderedDict
from typing import TYPE_CHECKING, Type

from django.core.exceptions import FieldDoesNotExist
from django.db.models.options import Options as ModelOptions

from django.utils.datastructures import ImmutableList
from django.utils.functional import cached_property
from django.utils.translation import override

if TYPE_CHECKING:
    from .json_model import JSONModel

PROXY_PARENTS = object()

IMMUTABLE_WARNING = (
    "The return type of '%s' should never be mutated. If you want to manipulate this list "
    "for your own use, make a copy first."
)

DEFAULT_NAMES = (
    "abstract",
    "ordering",
    "verbose_name",
    "verbose_name_plural",
)


def make_immutable_fields_list(name, data):
    return ImmutableList(data, warning=IMMUTABLE_WARNING % name)


class JSONModelOptions:
    """
    Options class for JSONModel

    Adapted from Django's Options class (imported in this module as ModelOptions for easier disambiguation)
    """

    FORWARD_PROPERTIES = {"fields", "concrete_fields", "local_concrete_fields", "_forward_fields_map"}
    REVERSE_PROPERTIES = {"related_objects", "fields_map", "_relation_tree"}

    model: Type["JSONModel"]

    # not supported
    proxy = False
    proxy_for_model = None

    def __init__(self, meta):
        self._get_fields_cache = {}
        self.local_fields = []
        self.private_fields = []
        self.model_name = None
        self.verbose_name = None
        self.verbose_name_plural = None
        self.object_name = None
        self.meta = meta
        self.parents = OrderedDict()
        self.concrete_model = None
        self.ordering = []
        self._ordering_clash = False
        self.abstract = False

        ##########################################################################################################
        # attributes used for compatibility with Model; values are only used for conditional checks an enumeration
        ##########################################################################################################

        self.app_label = None
        self.db_table = "n/a"
        self.required_db_features: tuple = ()
        self.local_many_to_many: list = []
        self.many_to_many: list = []
        self.proxy = None
        self._relation_tree = ()

    def get_field(self, field_name):
        """
        Return a field instance given the name of a forward or reverse field.
        """

        # Adapted from Options.get_field to omit checks for related fields, which are not supported

        try:
            # Retrieve field instance by name from cached or just-computed
            # field map.
            return self.fields_map[field_name]
        except KeyError:
            raise FieldDoesNotExist("%s has no field named '%s'" % (self.object_name, field_name))

    def __repr__(self):
        return "<JSONModelOptions for %s>" % self.object_name

    def __str__(self):
        return self.model_name

    ###################################################################################################################
    # passthrough methods - these can be used as-is from Django's model Options class directly rather than copy/pasting
    # the code
    ###################################################################################################################

    def add_field(self, field, private=False):
        if field.is_relation:
            raise TypeError("Relation fields are not supported")

        return ModelOptions.add_field(self, field, private)

    def contribute_to_class(self, cls, name):
        return ModelOptions.contribute_to_class(self, cls, name)

    def get_base_chain(self, model):
        return ModelOptions.get_base_chain(self, model)

    def get_parent_list(self):
        return ModelOptions.get_parent_list(self)

    def get_ancestor_link(self, ancestor):
        return ModelOptions.get_ancestor_link(self, ancestor)

    def get_fields(self, include_parents=True, include_hidden=False):
        return ModelOptions.get_fields(self, include_parents, include_hidden)

    def _get_fields(self, forward=True, reverse=True, include_parents=True, include_hidden=False, seen_models=None):
        return ModelOptions._get_fields(self, forward, reverse, include_parents, include_hidden, seen_models)

    def _expire_cache(self, forward=True, reverse=True):
        ModelOptions._expire_cache(self, forward, reverse)

    ####################################################################################################################
    # property re-implementations - these are copied from Django's model Options class since they cannot be accessed via
    #                               a passthrough call like the methods due to being properties
    ####################################################################################################################

    @cached_property
    def _property_names(self):
        """Return a set of the names of the properties defined on the model."""
        names = []
        for name in dir(self.model):
            attr = inspect.getattr_static(self.model, name)
            if isinstance(attr, property):
                names.append(name)
        return frozenset(names)

    @property
    def verbose_name_raw(self):
        """Return the untranslated verbose name."""
        with override(None):
            return str(self.verbose_name)

    @cached_property
    def fields(self):
        """
        Return a list of all forward fields on the model and its parents,
        excluding ManyToManyFields.

        Private API intended only to be used by Django itself; get_fields()
        combined with filtering of field properties is the public API for
        obtaining this field list.
        """
        # For legacy reasons, the fields property should only contain forward
        # fields that are not private or with a m2m cardinality. Therefore we
        # pass these three filters as filters to the generator.
        # The third lambda is a longwinded way of checking f.related_model - we don't
        # use that property directly because related_model is a cached property,
        # and all the models may not have been loaded yet; we don't want to cache
        # the string reference to the related_model.
        def is_not_an_m2m_field(f):
            return not (f.is_relation and f.many_to_many)

        def is_not_a_generic_relation(f):
            return not (f.is_relation and f.one_to_many)

        def is_not_a_generic_foreign_key(f):
            return not (
                f.is_relation and f.many_to_one and not (hasattr(f.remote_field, "model") and f.remote_field.model)
            )

        return make_immutable_fields_list(
            "fields",
            (
                f
                for f in self._get_fields(reverse=False)
                if is_not_an_m2m_field(f) and is_not_a_generic_relation(f) and is_not_a_generic_foreign_key(f)
            ),
        )

    @cached_property
    def concrete_fields(self):
        """
        Return a list of all concrete fields on the model and its parents.

        Private API intended only to be used by Django itself; get_fields()
        combined with filtering of field properties is the public API for
        obtaining this field list.
        """
        return make_immutable_fields_list("concrete_fields", (f for f in self.fields if f.concrete))

    @cached_property
    def local_concrete_fields(self):
        """
        Return a list of all concrete fields on the model.

        Private API intended only to be used by Django itself; get_fields()
        combined with filtering of field properties is the public API for
        obtaining this field list.
        """
        return make_immutable_fields_list("local_concrete_fields", (f for f in self.local_fields if f.concrete))

    @cached_property
    def _forward_fields_map(self):
        res = {}
        fields = self._get_fields(reverse=False)
        for field in fields:
            res[field.name] = field
            # Due to the way Django's internals work, get_field() should also
            # be able to fetch a field by attname. In the case of a concrete
            # field with relation, includes the *_id name too
            try:
                res[field.attname] = field
            except AttributeError:
                pass
        return res

    @cached_property
    def fields_map(self):
        res = {}
        fields = self._get_fields(forward=False, include_hidden=True)
        for field in fields:
            res[field.name] = field
            # Due to the way Django's internals work, get_field() should also
            # be able to fetch a field by attname. In the case of a concrete
            # field with relation, includes the *_id name too
            try:
                res[field.attname] = field
            except AttributeError:
                pass
        return res

    ###################################################################################################################
    # placeholder methods - these are methods used by Django Models that pertain to functionality not needed or not
    #                       supported by JSONModel. They are provided as noops so to allow JSONModel classes to be used
    #                       interchangeably with Django Model classes for ModelForms and ModelAdmin.
    ###################################################################################################################

    def setup_pk(self, field):
        pass
