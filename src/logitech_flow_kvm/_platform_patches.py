"""
Applies compatibility patches on non-Linux platforms before logitech_receiver is imported.

solaar's logitech_receiver.diversion unconditionally imports GTK3 (Gdk, Gtk), which is
unavailable on macOS without a full GTK install. Since logitech_flow_kvm never uses
the diversion/key-remapping feature, we intercept the diversion module import and replace
it with a minimal stub so the rest of logitech_receiver loads cleanly.
"""

import importlib.abc
import importlib.machinery
import platform
import sys
import types


if platform.system() != "Linux":

    class _DiversionStubLoader(importlib.abc.Loader):
        def create_module(self, spec):
            return types.ModuleType(spec.name)

        def exec_module(self, module):
            def process_notification(*args, **kwargs):
                pass

            module.process_notification = process_notification

    class _DiversionImportHook(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname == "logitech_receiver.diversion":
                return importlib.machinery.ModuleSpec(
                    fullname,
                    _DiversionStubLoader(),
                    is_package=False,
                )
            return None

    sys.meta_path.insert(0, _DiversionImportHook())
