"""The FYPA viewer GUI, split by feature.

``PdnViewer`` (:mod:`fypa.viewer.window`) is assembled from one mixin per
feature or tab; ``fypa.altium_viewer`` re-exports every name for existing
callers.

When a test patches a module-level name (``QMessageBox``, a helper
function, a module global), patch it on the module that *looks it up*,
e.g. ``fypa.viewer.session.QMessageBox`` — patching the
``fypa.altium_viewer`` re-export changes nothing the code sees.
"""
