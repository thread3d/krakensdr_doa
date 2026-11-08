"""Compatibility shims so the krakensdr_doa web UI runs on Dash 2.x + Werkzeug 2.2.

Two independent breakages, both fixed here rather than by editing the view and
callback modules (which keeps upstream merges cheap):

1. LEGACY DASH COMPONENT MODULES.
   The UI has ~8 files doing `import dash_core_components as dcc` and
   `import dash_html_components as html`. Those top-level modules were removed in
   Dash 2.x; the components now live in `dash.dcc` and `dash.html`. Aliasing them
   in sys.modules satisfies the upstream imports verbatim.

2. dash_devices IS UNMAINTAINED.
   dash_devices 0.1.3 does
       from werkzeug.debug.tbtools import get_current_traceback
   which Werkzeug removed in 2.1 (this environment has 2.2.3). It is used only to
   render the 500 error page, so a minimal stand-in is sufficient. Downgrading
   Werkzeug instead would break Dash 2.9, which is a much worse trade.

Import this module BEFORE anything that imports dash_devices or the legacy
component modules -- `app.py` does exactly that as its first line.
"""

import html as _html
import sys
import traceback as _traceback

# --------------------------------------------------------------------------
# 1. Werkzeug shim, needed before `import dash_devices` executes
# --------------------------------------------------------------------------
try:
    import werkzeug.debug.tbtools as _tbtools

    if not hasattr(_tbtools, "get_current_traceback"):

        class _Traceback:
            """Minimal stand-in for werkzeug's removed DebugTraceback."""

            def __init__(self):
                self.plaintext = _traceback.format_exc()
                self.exception = sys.exc_info()[1]

            def render_full(self):
                return "<!doctype html><title>500</title><pre>" + _html.escape(self.plaintext) + "</pre>"

            def render_summary(self):
                return self.render_full()

            def render_debugger_html(self, *args, **kwargs):
                return self.render_full()

        def get_current_traceback(*args, **kwargs):
            # dash_devices calls it both bare and as get_current_traceback(skip=n).
            return _Traceback()

        _tbtools.get_current_traceback = get_current_traceback
        _DASH_DEVICES_SHIM = True
    else:
        _DASH_DEVICES_SHIM = False
except ImportError:
    _DASH_DEVICES_SHIM = False

# --------------------------------------------------------------------------
# 2. Legacy Dash component module aliases (dash_core_components, dash_renderer)
# --------------------------------------------------------------------------
try:
    import dash as _dash

    # In Dash 2.x these are modules (dash.dcc, dash.html). Register them under
    # the pre-2.0 names so `import dash_core_components as dcc` keeps working.
    if "dash_core_components" not in sys.modules and hasattr(_dash, "dcc"):
        sys.modules["dash_core_components"] = _dash.dcc
    if "dash_html_components" not in sys.modules and hasattr(_dash, "html"):
        sys.modules["dash_html_components"] = _dash.html

    # dash_devices also imports the standalone `dash_renderer` package, which Dash
    # 2.x folded into itself. It reads exactly two attributes,
    # _js_dist and _js_dist_dependencies, and dash._dash_renderer has both.
    if "dash_renderer" not in sys.modules:
        try:
            import dash._dash_renderer as _renderer
            sys.modules["dash_renderer"] = _renderer
        except ImportError:
            pass
    _DASH_ALIAS_SHIM = True
except ImportError:
    _DASH_ALIAS_SHIM = False


def report():
    """One line describing what was actually needed -- handy in ui.log."""
    parts = []
    parts.append("werkzeug shim: " + ("applied" if _DASH_DEVICES_SHIM else "not needed"))
    parts.append("dash alias:    " + ("applied" if _DASH_ALIAS_SHIM else "dash missing"))
    return "dash_compat: " + ", ".join(parts)
