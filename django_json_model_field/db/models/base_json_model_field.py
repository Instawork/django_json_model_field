from abc import ABCMeta, abstractmethod
from typing import Any, Optional, TYPE_CHECKING, Type, Union

from django.core import checks
from django.core.exceptions import ValidationError
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import Field, Model

from django.db.models import JSONField

if TYPE_CHECKING:
    from .json_model import JSONModel


class BaseJSONModelField(JSONField, metaclass=ABCMeta):
    """
    A subclass of JSONField that allows defining a specific model for the data stored in a JSON field.
    """

    def get_formfield_kwargs(self, **kwargs):
        return kwargs

    def formfield(self, **kwargs):
        from django_json_model_field import forms

        kwargs = self.get_formfield_kwargs(
            form_class=kwargs.pop("form_class", forms.NestedModelFormField),
            **kwargs,
        )

        # don't use super().formfield because the form field doesn't need the same things as the base JSONField
        return Field.formfield(self, **kwargs)

    @abstractmethod
    def get_json_model_class(self, host: Union[Model, dict]) -> Optional[Type["JSONModel"]]:
        """
        Returns the `JSONModel` class to be used to represent the field data
        """

        raise NotImplementedError()

    def from_db_value(self, value, expression, connection):
        # overridden method from Field and JSONField used to convert the DB's JSON (string) value to a dict, and then
        # from the dict to an instance of the JSONModel defined for the field
        data: Optional[dict] = super().from_db_value(value, expression, connection)

        return self.from_db_data_dict(data, connection)

    def from_db_data_dict(self, data: Optional[dict], connection: BaseDatabaseWrapper) -> Optional["JSONModel"]:
        host_data = self.get_json_model_host_data(data)
        json_model = self.get_json_model_class(host=host_data)
        if json_model is None:
            return None

        has_input = data is not None
        data = data or {}
        converted_data = {
            field.name: self._convert_field(connection, field, data.get(field.name))
            for field in json_model._meta.fields
        }
        return json_model(_has_input=has_input, **converted_data, _skip_clean=True) if json_model else None

    def _convert_field(self, connection, field: Field, value: Any):
        """
        Uses the field's db converters to restore the value to its Python/Django representation
        """

        # See code comments in _get_db_prep_value_from_model for why this is necessary
        converters = field.get_db_converters(connection)
        for converter in converters:
            value = converter(value=value, expression=None, connection=connection)

        return value

    def save_form_data(self, instance: Model, data: dict):
        json_model_class = self.get_json_model_class(instance)
        value = json_model_class(**data) if json_model_class else None
        setattr(instance, self.name, value)

    def get_json_model_host_data(self, data: Optional[dict]) -> dict:
        return {}

    def clean(self, value: Optional[Union[dict, "JSONModel"]], model_instance: Optional[Model]):
        """
        Convert the value's type and run validation. Validation errors
        from to_python() and validate() are propagated. Return the correct
        value if no error is raised.
        """
        value = self.to_python(value, model_instance)
        try:
            self.validate(value, model_instance)
        except ValidationError:
            # Raising ValidationError with the model_validation code - this ensures that the caller is notified of any
            # nested validation errors from attempting to validate the JSONModel instance, and allows form fields
            # derived from this field to hide the messaging from this validation error in favor of showing the
            # validation errors on the individual fields for the JSONModel.
            raise ValidationError("The model contains validation errors", code="model_validation")
        self.run_validators(value)
        return value

    def to_python(self, value: Optional[Union[dict, "JSONModel"]], model_instance: Model = None) -> Optional["JSONModel"]:
        from django_json_model_field.db.models import JSONModel

        # note: signature is overridden from the base Field signature to include the model instance. This is possible
        #       since the `clean` method is also overridden to pass its model_instance argument along.

        # the value must either already be a JSONModel instance, or a dict that can then be converted to JSONModel
        # instance so that the field validation for that model can be checked before saving
        if isinstance(value, JSONModel):
            return value

        if isinstance(value, dict):
            # Use model initialization to ensure the data is valid and any values are correctly prepped for
            # serialization
            json_model_class = self.get_json_model_class(model_instance)

            if json_model_class is None:
                return None

            # _skip_clean=True because it will be validated elsewhere
            return json_model_class(_skip_clean=True, **value)

        return None

    def validate(self, value: Optional["JSONModel"], model_instance: Optional[Model]):
        if value is not None:
            value.full_clean()

        # the JSONField superclass validates whether the value is valid JSON, so it needs the data in dict form
        super().validate(self.get_prep_value(value) if value else None, model_instance)

    def get_prep_value(self, value):
        if value is None or value == {}:
            return None

        if isinstance(value, dict):
            # already prepped (e.g. from get_db_prep_value), just pass it through
            return super().get_prep_value(value)

        from django_json_model_field.db.models import JSONModel

        if isinstance(value, JSONModel):
            data = self._get_prep_value_from_model(value)
            return super().get_prep_value(data)

        raise TypeError("Cannot prep values that are not instances of JSONModel")

    def _get_prep_value_from_model(self, instance: "JSONModel") -> dict:
        """
        Validates instance data and converts it to a dict to be used as input data for a JSON field
        """

        instance.full_clean()
        # Using each field's value_to_string in turn will help ensure that the individual field values are JSON
        # serializable.
        return {field.name: field.value_to_string(instance) for field in instance._meta.fields}

    def get_db_prep_value(self, value, connection, prepared=False):
        if prepared:
            raise NotImplementedError()

        if value is None or value == {}:
            return None

        from django_json_model_field.db.models import JSONModel

        if isinstance(value, JSONModel):
            data = self._get_db_prep_value_from_model(value, connection)
            return super().get_db_prep_value(data, connection, prepared)

        return super().get_db_prep_value(value, connection, prepared)

    def _get_db_prep_value_from_model(self, instance: "JSONModel", connection) -> dict:
        """
        Validates instance data and converts it to a dict to be used before saving to the database
        """

        instance.full_clean()

        # Using each field's get_db_prep_value in turn will help ensure that the individual field values are JSON
        # serializable.
        #
        # Note that this is uses get_db_prep_value as opposed to get_prep_value that is used in
        # _get_prep_value_from_model - get_db_prep_value is used to convert values to representations suitable to be
        # stored in a specific type of database column (e.g. DurationField uses an integer when the DB does not have a
        # specialized "duration" type column). While this is not strictly necessary for a JSONModel since the values are
        # being serialized to JSON, using get_db_prep_value will ensure that the representations of values stored in the
        # JSON are interchangeable with those that use the same Django field type on a field for an actual DB column.
        # This will make querying the data more straightforward outside of Django, such as when querying a database
        # directly for debugging or maintenance, or when the data is synchronized to another DB provider for a data
        # warehouse or processed through an ETL.
        return {
            field.name: field.get_db_prep_value(field.value_from_object(instance), connection)
            for field in instance._meta.fields
        }

    def check(self, **kwargs):
        """
        Extends the base field checks to also include model checks for the JSONModel class

        Checks are run once during the Django app startup - running the JSONModel checks from here allows the checks
        to be evaluated for JSONModel classes that are used by fields without having to also implement an additional
        registry for JSONModel classes to integrate with Django's checks system (see check_all_models in
        django/core/checks/model_checks.py). If a use case for using JSONModel classes outside a BaseJSONModelField emerges,
        this approach will need to be reconsidered.
        """

        errors = super().check(**kwargs)
        errors.extend(self._check_null())
        errors.extend(self._check_json_model_arguments())

        return errors

    def _check_null(self):
        """
        Checks the `null` and `blank` arguments to make sure they are both True. To prevent duplicate errors from being
        surfaced to the end user, JSONModelFields are always nullable. Since the actual JSONModel class used to store
        data may vary, all data validation enforcement is deferred to the JSONModel's fields and derived forms.
        """

        errors = []
        if self.null is not True:
            errors.append(checks.Critical("JSONModelFields must set null=True", obj=self))
        if self.blank is not True:
            errors.append(checks.Critical("JSONModelFields must set blank=True", obj=self))

        return errors

    def _check_json_model_arguments(self, **kwargs):
        """
        Checks arguments related to specifying the JSONModel for the field to ensure the right combination of arguments
        was provided, and that the arguments are of valid types.
        """
        return []

    def _check_model_argument(self, model: Any, arg_name: str):
        """
        Verifies that any types provided to be used as JSONModel classes are types that subclass JSONModel
        """

        if not isinstance(model, type):
            return [
                checks.Critical(
                    "Expected a type",
                    obj=model,
                    hint=f"{arg_name} must be a class"
                )
            ]

        from django_json_model_field.db.models import JSONModel

        if not issubclass(model, JSONModel):
            return [
                checks.Critical(
                    "Expected a subclass of JSONModel",
                    obj=model,
                    hint=f"{arg_name} must be a subclass of JSONModel"
                )
            ]

        return []

    def _check_json_models(self, **kwargs):
        """
        Runs model checks on any JSONModel types specified for use in the field.
        """

        return []
