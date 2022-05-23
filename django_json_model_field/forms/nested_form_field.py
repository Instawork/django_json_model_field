from typing import Callable, Optional, Type, Union

from django.core.exceptions import ValidationError
from django.forms import BaseForm, BaseModelForm, BoundField, Field

from .widgets import NestedFormInput


class NestedFormBoundField(BoundField):
    """
    Subclasses the default BoundField to add support for initializing nested forms as a field's presentation.

    BoundFields are used to manage binding a field instance of a form to a specific data value since Field
    implementations on their own are stateless.
    """

    field: "NestedFormField"

    def get_nested_form_class(self) -> Optional[Type[BaseForm]]:
        return self.field.get_nested_form_class(self.form)

    def get_nested_form_kwargs(self, nested_form_class: Type[BaseForm]) -> dict:
        return self.field.get_nested_form_kwargs(self.form, self.name, nested_form_class)

    def get_nested_form(self) -> Optional[BaseForm]:
        nested_form_class = self.get_nested_form_class()

        if nested_form_class is None:
            return None

        nested_form_kwargs = self.get_nested_form_kwargs(nested_form_class)

        if "instance" in nested_form_kwargs and not issubclass(nested_form_class, BaseModelForm):
            nested_form_kwargs.pop("instance")

        return nested_form_class(**nested_form_kwargs)

    def as_widget(self, widget=None, attrs=None, only_initial=False):
        attrs = attrs or {}
        attrs["form"] = self.get_nested_form()
        return super().as_widget(widget, attrs, only_initial)


NestedFormClassFn = Callable[[Union[BaseForm, dict]], Optional[Type[BaseForm]]]
NestedFormClassArg = Union[Type[BaseForm], NestedFormClassFn]


class NestedFormField(Field):
    """
    A form field implementation that can be used to represent dictionary-like data using a nested Django form.
    """

    bound_field_class: Type[NestedFormBoundField] = NestedFormBoundField
    nested_form_class: Optional[Type[BaseForm]]
    widget: Type[NestedFormInput] = NestedFormInput

    _invalid_nested_field = "invalid_nested_field"

    def __init__(self, nested_form_class: NestedFormClassArg, *args, **kwargs):
        self._nested_form_class = nested_form_class
        if isinstance(nested_form_class, type) and issubclass(nested_form_class, BaseForm):
            self.nested_form_class = nested_form_class
        else:
            setattr(self, "get_nested_form_class", nested_form_class)

        # "required" validation will be delegated to the nested form
        kwargs.pop("required", None)
        super().__init__(required=False, *args, **kwargs)

        # check for BaseForm rather than Form - BaseForm is what is used by the modelform_factory function
        if isinstance(nested_form_class, type) and not issubclass(nested_form_class, BaseForm):
            raise TypeError("When specifying a type for nested_form_class, it must be a subclass of Form")

        if not isinstance(self.widget, NestedFormInput):
            raise TypeError("widgets for NestedFormField must be an instance of NestedFormInput or a subclass thereof")

        if not issubclass(self.bound_field_class, NestedFormBoundField):
            raise TypeError("bound_field_class must be a subclass of NestedFormBoundField")

        # TODO: set widget.is_required on widget based on whether the nested form has any required fields

    def get_nested_form_class(self, host: Union[BaseForm, dict], raw_data: dict = None) -> Optional[Type[BaseForm]]:
        return self.nested_form_class

    def clean(self, value):
        validation_form = self.get_form_for_validation(raw_data=value)
        if validation_form is None:
            setattr(self, "_validation_form_cleaned_data", value)
        else:
            validation_form.full_clean()
            setattr(self, "_validation_form_cleaned_data", validation_form.cleaned_data)
        return super().clean(value)

    def to_python(self, value):
        validation_form = self.get_form_for_validation(raw_data=value)
        if validation_form is None:
            return super().to_python(value)

        validation_form.is_valid()
        return validation_form.cleaned_data

    def get_form_for_validation(self, raw_data: dict = None, cleaned_data: dict = None):
        """
        Gets an instance of the nested form to be used for data conversion / cleaning / validation.
        """

        # This is a little naughty since it seems like field instances aren't supposed to be stateful or keep references
        # to anything specific to a form value, but it also seemed silly to have to instantiate the nested form and have
        # it run its cleaning / validation logic multiple times in order to maintain the statelessness.

        if raw_data is None and cleaned_data is None:
            raise ValueError("one of raw_data or cleaned_data must be specified")

        if (
            hasattr(self, "_validation_form")
            and (raw_data is not None and getattr(self, "_validation_form_raw_data", None) == raw_data)
            or (cleaned_data is not None and getattr(self, "_validation_form_cleaned_data", None) == cleaned_data)
        ):
            return getattr(self, "_validation_form")

        if raw_data is None:
            raise ValueError("raw_data must be provided when there is no previously initialized form")

        if cleaned_data is not None:
            # this shouldn't happen, something weird is going on
            raise ValueError("cleaned_data must not be provided when there is no previously initialized form")

        host_data = raw_data.pop(self.widget.included_from_host_key, None)
        nested_form_class = self.get_nested_form_class(host_data, raw_data=raw_data)
        form = nested_form_class(data=raw_data) if nested_form_class is not None else None

        setattr(self, "_validation_form_raw_data", raw_data)
        setattr(self, "_validation_form", form)
        return form

    def validate(self, value):
        super().validate(value)

        validation_form = self.get_form_for_validation(cleaned_data=value)

        if validation_form is not None and not validation_form.is_valid():
            # The empty string used for the validation message allows any validation errors on the nested form to bubble
            # up, and since there's no content for the message, the UI isn't affected and won't show seemingly
            # duplicative errors.
            raise ValidationError("", code=self._invalid_nested_field)

    def get_bound_field(self, form, field_name) -> BoundField:
        return self.bound_field_class(form, self, field_name)

    def get_nested_form_kwargs(self, host_form: BaseForm, field_name: str, nested_form_class: Type[BaseForm]) -> dict:
        return dict(
            prefix=host_form.add_prefix(field_name),
            data=self.get_nested_form_data(host_form, field_name, nested_form_class),
            initial=self.get_nested_form_initial(host_form, field_name, nested_form_class),
            renderer=host_form.renderer,
        )

    def get_nested_form_data(self, host_form: BaseForm, field_name: str, nested_form_class: Type[BaseForm]) -> dict:
        # form.data is defaulted to an empty dict in Form.__init__, so it will always have a non-None value regardless
        # of whether it is bound - however, Form.__init__ sets is_bound based on whether the data and files arguments
        # passed are None, so is_bound must be checked to prevent the nested form from thinking it is bound.
        return host_form.data.copy() if host_form.is_bound else None

    def get_nested_form_initial(self, host_form: BaseForm, field_name: str, nested_form_class: Type[BaseForm]) -> dict:
        return host_form.initial.pop(field_name, None)
