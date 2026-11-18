from .lean_models import install as _install_lean_models

# Must run before anything imports aiogram; see app/lean_models.py.
_install_lean_models()
