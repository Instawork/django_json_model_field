from typing import Optional, TYPE_CHECKING, Type, Union

from django.core import checks
from django.db.models import Model

from .base_json_model_field import BaseJSONModelField

if TYPE_CHECKING:
    from .json_model import JSONModel


class JSONModelField(BaseJSONModelField):
    def __init__(
        self,
        json_model: Optional[Type["JSONModel"]],
        verbose_name: str = None,
        name: str = None,
        null: bool = True,
        blank: bool = True,
        encoder=None,
        decoder=None,
        **kwargs,
    ):
        """

        Parameters
        ----------
        json_model     A single JSONModel class that will be used to work with and validate JSON data stored using the
                       field.
        """
        # validation of model field arguments is handled by Django's checks system - see the `check` method below.

        self.json_model = json_model
        super().__init__(verbose_name, name, encoder, decoder, null=null, blank=blank, **kwargs)

    def get_formfield_kwargs(self, **kwargs):
        kwargs.update(
            nested_model_class=self.json_model,
        )
        return kwargs

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        args = [self.json_model, *args]
        return name, path, args, kwargs

    def get_json_model_class(self, host: Union[Model, dict]) -> Optional[Type["JSONModel"]]:
        """
        Returns the `JSONModel` class to be used to represent the field data
        """

        return self.json_model

    def _check_json_model_arguments(self, **kwargs):
        """
        Checks arguments related to specifying the JSONModel for the field to ensure the right combination of arguments
        was provided, and that the arguments are of valid types.
        """

        if self.json_model is None:
            return [checks.Critical("json_model must have a value", obj=self)]

        return self._check_model_argument(self.json_model, "json_model")

    def _check_json_models(self, **kwargs):
        """
        Runs model checks on any JSONModel types specified for use in the field.
        """

        if self.json_model:
            return self.json_model.check(**kwargs)

        return []
