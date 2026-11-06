"""Shared picker behavior for harnesses configured through models.yaml."""

from yuki_conductor.harness.base import ModelOption
from yuki_conductor.model_config import ModelAlias, load_harness_models


class ConfiguredModels:
    name: str

    def resolve_model(self, alias: str | None) -> ModelAlias:
        return load_harness_models(self.name).resolve(alias)

    def model_aliases(self) -> dict[str, str]:
        return {a.label: a.id for a in load_harness_models(self.name).aliases.values()}

    def models(self) -> list[ModelOption]:
        return [ModelOption(id=a.id, label=a.label)
                for a in load_harness_models(self.name).aliases.values()]

    def default_model(self) -> str:
        return load_harness_models(self.name).default

    def default_label(self) -> str:
        return self.resolve_model(None).label
