from typing import Optional, Tuple, Type

from django.db.models.constants import LOOKUP_SEP
from django.forms import Form, Widget

from django_json_model_field.util import get_type_init_with_metaclass


class NestedFormInput(Widget):
    """
    Widget implementation used to render a nested Django form as the UI representation of a field
    """

    @classmethod
    def with_fields_from_host(cls, *fields) -> Type["NestedFormInput"]:
        """
        A class factory method that allows specifying fields from the host model for that will be included with the data
        for the nested form when calling `value_from_datadict`. When specified, the values for these fields will be
        included in a `_host` key. This is used with ConditionalJSONModelField to help determine when the value of the
        selector field has changed.
        """

        return get_type_init_with_metaclass(cls)(cls.__name__, (cls,), {"include_from_host": tuple(fields)})

    template_name = "django_json_model_field/forms/widgets/nested_form_input.html"

    include_from_host: Tuple[str, ...] = ()
    included_from_host_key = "_host"

    def get_context(self, name, value, attrs):
        attrs = attrs or {}
        form: Optional[Form] = attrs.pop("form")

        context = super().get_context(name, value, attrs)
        context.update(nested_form=form)

        return context

    def value_from_datadict(self, data, files, name):
        """
        Used to extract data from a form POST to a dict by looking for data entries whose key is prefixed with the name
        of the widget's field.
        """

        # names for nested form fields will be prefixed with the name of the original field
        prefix = f"{name}-"
        nested_keys = [(key, key[len(prefix) :]) for key in data if key.startswith(prefix)]
        nested_data = {field: data[data_key] for data_key, field in nested_keys}

        if self.include_from_host:
            prefix_parts = name.split("-")
            host_prefix = "-".join(prefix_parts[:-1])
            host_prefix = f"{host_prefix}-" if host_prefix else ""
            host_keys = [
                (f"{host_prefix}{field}", field) for field in self.include_from_host if LOOKUP_SEP not in field
            ]
            host_data = {field: data[data_key] for data_key, field in host_keys}
            nested_data.update({self.included_from_host_key: host_data})

        return nested_data

    def value_omitted_from_data(self, data, files, name):
        """
        This method normally checks the posted data to see if there's a missing entry for the widget's field, but nested
        data is spread across multiple fields, so use value_from_datadict to see if any of the nested form's fields have
        entries in the data.
        """

        return self.value_from_datadict(data, files, name) is None
