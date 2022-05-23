from typing import Any, Callable, Optional, Type, Union, overload

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db.models import Model
from django.db.models.constants import LOOKUP_SEP
from django.forms import BaseForm, BaseModelForm, CharField, Form, ModelForm, modelform_factory

from django_json_model_field.db.models import JSONModel

from .nested_form_field import NestedFormField

AnyModel = Union[Model, JSONModel]
NestedModelClassFn = Callable[[Union[Model, dict]], Optional[Type[AnyModel]]]
NestedModelClassArg = Union[Type[AnyModel], NestedModelClassFn]


class _SelectorOnlyForm(Form):
    """
    Form used internally by NestedModelFormField to track the value of the selector field when there is no form required
    for the current selector value.
    """

    _selector_value = CharField(widget=CharField.hidden_widget, required=False)


class BaseSelectedNestedModelForm(ModelForm):
    """
    Base ModelForm class used internally by NestedModelFormField to track the value of the selector field when a
    selector is used to vary the model class used for the form.
    """

    _selector_value = CharField(widget=CharField.hidden_widget, required=False)


class NestedModelFormField(NestedFormField):
    """
    Subclass of `NestedFormField` that adds support for generating forms based on JSONModel or Model classes, as well
    as multi-variant models using a "selector" field.
    """

    _selector_value_field = "_selector_value"
    _selector_value_changed = "selector_value_changed"

    default_error_messages = {
        # hide the error message for model_validation so that a validation errors on the nested form do not cause an
        # additional, more generic validation error message to be shown for this field
        "model_validation": ""
    }

    @overload
    def __init__(self, nested_model_class: Type[JSONModel], *args, **kwargs):
        ...

    @overload
    def __init__(
        self,
        nested_model_class: NestedModelClassFn,
        selector_field: str,
        *args,
        **kwargs,
    ):
        ...

    def __init__(
        self,
        nested_model_class: NestedModelClassArg,
        selector_field: str = None,
        get_selector_value: Callable[[Model], Optional[Any]] = None,
        *args,
        **kwargs,
    ):
        self.selector_field = selector_field
        if self.selector_field:
            # this must happen before super().__init__ because it deals with instantiating the widget
            self.widget = self.widget.with_fields_from_host(self.selector_field)

        super().__init__(self._get_nested_model_form_class, *args, **kwargs)

        if isinstance(nested_model_class, type) and issubclass(nested_model_class, JSONModel):
            self.nested_model_class = nested_model_class
        else:
            setattr(self, "_get_nested_model_class", nested_model_class)

        if get_selector_value:
            self._get_selector_value_from_instance = get_selector_value

    def get_nested_form_kwargs(self, host_form: BaseForm, field_name: str, nested_form_class: Type[BaseForm]) -> dict:
        # the "initial" for JSONModelFields can be a JSONModel instance - move it to the "instance" kwarg
        # FIXME: why?
        instance = self._fix_nested_form_initial(host_form, field_name)

        kwargs = super().get_nested_form_kwargs(host_form, field_name, nested_form_class)
        if instance:
            kwargs.update(instance=instance)
        return kwargs

    def _fix_nested_form_initial(self, host_form: BaseForm, field_name: str) -> Optional[JSONModel]:
        initial = host_form.initial.pop(field_name, {})
        instance: Optional[JSONModel] = None

        if isinstance(initial, JSONModel):
            instance = initial
            initial = {}

        host_form.initial[field_name] = initial
        return instance

    def get_nested_form_initial(self, host_form: BaseForm, field_name: str, nested_form_class: Type[BaseForm]) -> dict:
        initial = super().get_nested_form_initial(host_form, field_name, nested_form_class)

        if self.selector_field and isinstance(host_form, BaseModelForm):
            initial = initial or {}
            initial.update({self._selector_value_field: self._get_selector_value(host_form)})

        return initial

    def get_nested_form_data(self, host_form: BaseForm, field_name: str, nested_form_class: Type[BaseForm]) -> dict:
        data = super().get_nested_form_data(host_form, field_name, nested_form_class)

        if data is None:
            return None

        if self.selector_field:
            self._update_form_data_for_selector_field(host_form, field_name, nested_form_class, data)

        return data

    def _update_form_data_for_selector_field(
        self, host_form: BaseForm, field_name: str, nested_form_class: Type[BaseForm], data: dict
    ):
        """
        Sets the hidden selector value field's value based on the value selected the last time the form was
        submitted. This allows the condition that triggers the "selector_value_changed" ValidationError in
        NestedModelFormField.validate to be cleared so that the user can save their changes on the next
        request.
        """

        selector_value = self._get_selector_value(host_form)
        if selector_value is not None:

            selector_data_key = host_form.add_prefix(f"{field_name}-{self._selector_value_field}")
            data[selector_data_key] = selector_value

            if host_form.is_bound and self.selector_field in host_form.changed_data:
                self._update_form_data_for_selector_field_change(host_form, field_name, nested_form_class, data)

    def _update_form_data_for_selector_field_change(
        self, host_form: BaseForm, field_name: str, nested_form_class: Type[BaseForm], data: dict
    ):
        """
        If the value of the selector field changes, set initial data using defaults for the new form.
        Since the host form is already bound, any "initial" data provided for the nested form will be ignored,
        preventing defaults from being set as they usually are.
        """

        # TODO: data for field names shared between the old form and the new one should get removed to avoid
        #       pre-populating the field on the new form with the old data

        # TODO: known issue - when changing the selector on an existing object from the original type, to a different
        #       selector value that uses a different model, and then back causes the original data to be lost.
        #       The missing data can probably be repopulated from the original instance.

        fields = nested_form_class._meta.model._meta.fields
        data.update(
            {
                host_form.add_prefix(f"{field_name}-{nested_field.name}"): nested_field.get_default()
                for nested_field in fields
                if nested_field.has_default()
            }
        )

    def validate(self, value):
        validation_form = self.get_form_for_validation(cleaned_data=value)
        if validation_form is not None and self.selector_field is not None and LOOKUP_SEP not in self.selector_field:
            # FIXME: remove LOOKUP_SEP not in self.selector_field and implement a check if the relation is editable
            validation_form.is_valid()  # trigger validation / change detection
            initial_selector_value = validation_form.cleaned_data.get(self._selector_value_field)
            instance = getattr(validation_form, "instance", None)
            required_selector_value = getattr(instance, self._selector_value_field, None)
            if (
                initial_selector_value or required_selector_value
            ) and initial_selector_value != required_selector_value:
                # if the select field has changed, raise a ValidationError to prevent the user from unintentionally
                # saving default values for the new form.
                raise ValidationError(
                    "The '{selector_field}' field value has changed, "
                    "please verify entries in the updated form and save.".format(selector_field=self.selector_field),
                    code=self._selector_value_changed,
                )

        # this must happen AFTER the above check, otherwise validation errors for the new form may be thrown first
        super().validate(value)

    def _get_nested_model_class(self, host_instance: Union[AnyModel, dict]) -> Optional[Type[AnyModel]]:
        return self.nested_model_class

    def get_nested_model_class(self, host: Optional[Union[BaseForm, dict]]) -> Optional[Type[AnyModel]]:
        if hasattr(self, "nested_model_class"):
            return self.nested_model_class

        if host is None:
            return None

        if isinstance(host, BaseModelForm):
            return self._get_nested_model_class(host.instance)

        return self._get_nested_model_class(host)

    def _get_nested_model_form_class(
        self, host: Optional[Union[BaseForm, dict]], raw_data: dict = None
    ) -> Optional[BaseForm]:
        """
        Returns a form type used to represent the field's data
        """

        if host == {} and LOOKUP_SEP in self.selector_field and raw_data:
            # for selectors using a relation field, inject the selector value to allow the correct model class to
            # get looked up, since it will be the same as when the form was initially rendered
            host.update({self.selector_field: raw_data.get(self._selector_value_field)})

        model_class = self.get_nested_model_class(host)

        if model_class is None and self.selector_field is None:
            # this shouldn't ever happen - it would've gotten caught by the model and field checks, but having the
            # conditional check here helps make mypy happy
            raise ImproperlyConfigured("get_nested_model_class returned None with no selector field")

        fields = [field.name for field in model_class._meta.fields] if model_class else []

        if self.selector_field is None:
            # simplest use case: static model class, don't need to worry about any selector data
            return modelform_factory(model_class, fields=fields)

        if model_class is not None:
            # next simplest use case: variable model class, requires using BaseSelectedNestedModelForm to track
            # changes to the selector field
            fields.append(self._selector_value_field)
            return (
                modelform_factory(model_class, form=BaseSelectedNestedModelForm, fields=fields) if model_class else None
            )

        # other variable model class use cases when no model_class is returned
        selector_value = self._get_selector_value(host)
        if selector_value is None:
            # either it's a new object (newly initialized by the ModelForm), or the selector field is optional
            # and doesn't have a value - _SelectorOnlyForm allows tracking the selector field when no other data is
            # used
            return _SelectorOnlyForm

        # selector field has a value, but it doesn't map to a model (does not require additional data)
        return None

    def _get_selector_value(self, host: Optional[Union[BaseForm, dict]]) -> Optional[Any]:
        if host is None:
            return None

        if isinstance(host, BaseModelForm) and self.selector_field:
            if LOOKUP_SEP in self.selector_field:
                return self._get_selector_value_from_instance(host.instance)

            selector_field = host.fields[self.selector_field]
            selector_value = self._get_selector_value_from_instance(host.instance)
            return None if selector_value in selector_field.empty_values else selector_value

        selector_value = host.get(self.selector_field)
        # TODO: using self.empty_values since there's no reference to the model field ... it's probably good enough?
        return None if selector_value in self.empty_values else selector_value

    def _get_selector_value_from_instance(self, host: Model) -> Optional[Any]:
        return getattr(host, self.selector_field)
