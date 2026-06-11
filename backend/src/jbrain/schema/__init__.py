"""The entity schema registry: typed Python over the YAML in `schemas/`.

The YAML files are the authoring surface (docs/entity.md, decision 1); this
package is the runtime that loads them once into an in-process `SchemaRegistry`
and exposes the four consumers — prompt digest, value-shape validation, render
config, resolution config — as pure functions. Nothing here gates storage:
predicate-name validation never rejects (docs/entity.md invariant); only a
malformed `value_json` shape can.
"""

from jbrain.schema.loader import default_defs_dir, load_registry
from jbrain.schema.models import (
    EntityType,
    Facet,
    Meta,
    Predicate,
    SchemaError,
    SchemaRegistry,
)

__all__ = [
    "EntityType",
    "Facet",
    "Meta",
    "Predicate",
    "SchemaError",
    "SchemaRegistry",
    "default_defs_dir",
    "load_registry",
]
