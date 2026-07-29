"""Public LLM API, kept compatible with the former ``app.services.llm`` module."""

import sys
from types import ModuleType

from . import providers, scene_queries, scripts, social, terms

for _module in (providers, scripts, terms, scene_queries, social):
    for _name, _value in vars(_module).items():
        if not _name.startswith("__"):
            globals()[_name] = _value


class _LLMModule(ModuleType):
    """Keep legacy test patches of shared LLM internals effective across modules."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for module in (providers, scripts, terms, scene_queries, social):
            if name in vars(module):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _LLMModule
