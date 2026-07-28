"""Shared base model for API payloads."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """Base class for every request and response model.

    Python uses ``snake_case``; JavaScript conventionally uses ``camelCase``.
    Rather than forcing one side to adopt the other's style, the alias generator
    translates automatically: ``autonomy_mode`` in Python becomes
    ``autonomyMode`` in JSON.

    ``populate_by_name=True`` lets tests construct models with the Python names.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )
