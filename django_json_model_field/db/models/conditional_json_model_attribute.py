from typing import Optional, TYPE_CHECKING

from .json_model import JSONModel

if TYPE_CHECKING:
    from .conditional_json_model_field import ConditionalJSONModelField


class ConditionalJSONModelAttribute:
    """
    A custom accessor to be used with ConditionalJSONModelField.

    ConditionalJSONModelField creates subclasses of each JSONModel class defined in the model_map, each of which have
    their respective selector value set on the _selector_value attribute. When JSONModel values are saved to their
    JSON column in a database, ConditionalJSONModelField adds a structure to the JSON that includes the value of
    _selector_value so that the JSON structure can be deserialized into the correct JSONModel class when it is read from
    the database. The _selector_value attribute allows this to be done without having to make additional checks to the
    actual selector field, which can potentially incur additional performance or database access overhead if the
    selector field includes a relation lookup.

    However, if there is user code that creates its own instances of a JSONModel, it will likely not be using the
    _selector_value-annotated classes, but the original definition of the class that will not have a value set for
    _selector_value. In these cases, when that object is set on a ConditionalJSONModelField, this accessor will
    determine the expected JSONModel class based on the value of the selector field, and set the attribute value on the
    user-created instance. Note that this is specifically done on the instance and not the original JSONModel class
    definition because it is possible that JSONModels can be shared between multiple fields.
    """

    def __init__(self, field: "ConditionalJSONModelField"):
        self.field = field

    def _get_value(self, host_instance):
        return host_instance.__dict__.get(self.field.attname)

    def _set_value(self, host_instance, value):
        # store the value on the host instance's __dict__, otherwise the field will be considered to be a "deferred"
        # field by the logic in Model.save(), and the field will be omitted from updates to the DB
        host_instance.__dict__[self.field.attname] = value

    def __get__(self, host_instance, owner):
        return self._get_value(host_instance)

    def __set__(self, host_instance, value: Optional[JSONModel]):
        if value is None or value == {}:
            self._set_value(host_instance, value)
            return

        # _make_selector_json_model_class adds the _selector_value attribute when mapping JSONModel classes - if the
        # JSONModel value already has a value set for _selector_value, just store the value and don't bother with the
        # checks below. This can potentially avoid performance penalties if the selector field uses one or more relation
        # lookups.
        if bool(getattr(value, "_selector_value", None)):
            self._set_value(host_instance, value)
            return

        json_model_class = type(value)

        # this will be a subclass of the originally defined JSONModel class due to _make_selector_json_model_class
        expected_json_model_class: Optional[type] = self.field.get_json_model_class(host_instance)

        if expected_json_model_class is None:
            # No JSONModel value is needed based on the current selector value - it should not be getting set to a
            # non-None value
            raise TypeError(f"{self.field.attname} is not expecting a value")

        # for a valid value, it will be:
        #   - the same class if the value is being set from a JSONModelField, or any source that uses
        #     get_json_model_class to get the correct JSONModel class for the host host_instance's state
        #   - the immediate superclass of expected_json_model_class if the value being set is an instance created by
        #     user code that imported the JSONModel class directly from its declaration module
        if not issubclass(expected_json_model_class, json_model_class):
            expected_mro = expected_json_model_class.mro()
            # [0] is the class itself, [1] would be its immediate superclass
            original_json_class = expected_mro[1]
            raise TypeError(f"{self.field.attname} must be an instance of {original_json_class.__name__}")

        # _make_selector_json_model_class adds the _selector_value attribute when mapping JSONModel classes, but
        # it will not be set on user-created instances - set it on the instance here (NOT on json_model_class, it
        # is possible to reuse JSONModels between multiple fields!)
        setattr(value, "_selector_value", getattr(expected_json_model_class, "_selector_value"))
        self._set_value(host_instance, value)





