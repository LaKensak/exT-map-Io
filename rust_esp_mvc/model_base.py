"""Model layer: process memory, game snapshots and camera sampling.

The proven coherent-batch and bounded-prediction logic remains implemented by
the compatibility runtime. This facade deliberately disables the experimental
SCI skeleton path until its transform chain is validated in game.
"""

from . import legacy_runtime as legacy


class RustGameModel(legacy.RustGame):
    """Game-state model with the unvalidated skeleton resolver disabled."""

    def _resolve_bone_slots_batch(self, pm_to_bp):
        # BONES TEMPORARILY DISABLED.
        #
        # Keep the supplied New SCI mapping and resolver in legacy_runtime.py
        # for the later skeleton pass, but do not perform its extra memory
        # transactions from the production MVC entry point.
        return None


Mem = legacy.Mem
CameraSampler = legacy.CameraSampler
find_modules = legacy.find_modules
print_module_diagnostics = legacy.print_module_diagnostics

