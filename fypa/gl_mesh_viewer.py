"""Custom QOpenGLWidget that renders a 2D FEM mesh natively on the GPU.

This is the high-performance replacement for the pyqtgraph
``GraphicsLayoutWidget`` + ``ImageItem`` heatmap canvas. Instead of
rasterising the mesh to a CPU RGBA texture and uploading it every time
the user zooms, the triangle mesh lives in a GPU vertex buffer and the
fragment shader interpolates the colour per pixel using a 1-D colormap
LUT texture. Pan and zoom become single matrix updates — no resampling,
always pixel-sharp at any zoom level.

Public API
----------
Construction: ``GLMeshViewer(parent=None)``.

Data flow:
* :meth:`set_mesh(xs, ys, tris)` — upload vertex positions + triangle
  indices once per (layer, rail, rail-only) change.
* :meth:`set_values(values)` — push per-vertex scalar values for the
  current mode. Cheap (one buffer upload).
* :meth:`set_levels(vmin, vmax)` — update colormap window. Free (one
  uniform update — no GPU work until next paint).
* :meth:`set_colormap(lut_rgba_256)` — change the colour ramp. Rare.

View interaction:
* :meth:`fit_to_data()` — reset view so the data fills the widget.
* :meth:`set_view_center_scale(cx, cy, mm_per_pixel)` — explicit view.
* :meth:`view_range()` — current visible mm rectangle.

Signals:
* :attr:`viewChanged` — emitted on pan / zoom / resize.
* :attr:`mouseHoveredAt(world_x, world_y, inside)` — every mouse move.

Overlays (drawn via :class:`QPainter` inside ``paintGL``):
* :meth:`set_overlay_top_left(text_html)` — title chip top-left corner.
* :meth:`set_overlay_top_right(text_html)` — legend top-right corner.
* :meth:`set_markers(list_of_marker_groups)` — scatter overlays.

Vector-field arrows (drawn via the line shader, in world space):
* :meth:`set_arrows(positions, color)` — push GL_LINES vertices that
  encode arrow shafts + arrowheads.
* :meth:`clear_arrows()`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from OpenGL import GL
from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QMatrix4x4,
    QPainter,
    QPen,
    QPolygonF,
    QSurfaceFormat,
    QTextDocument,
    QVector3D,
)
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLFramebufferObject,
    QOpenGLFramebufferObjectFormat,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
    QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget


# OpenGL 3.3 core profile shaders. The vertex shader applies an
# orthographic MVP transform; the fragment shader looks up a colour from
# the 1-D colormap texture using the normalised value passed through
# from the vertex shader.
_VERTEX_SHADER_SRC = """
#version 330 core
layout(location = 0) in vec3 a_position;
layout(location = 1) in float a_value;
layout(location = 2) in float a_alpha;
layout(location = 3) in float a_neutral;
uniform mat4 u_mvp;
uniform vec2 u_levels;
out float v_norm;
out float v_alpha;
out float v_neutral;
void main() {
    gl_Position = u_mvp * vec4(a_position, 1.0);
    float span = max(u_levels.y - u_levels.x, 1e-30);
    v_norm = clamp((a_value - u_levels.x) / span, 0.0, 1.0);
    v_alpha = a_alpha;
    v_neutral = a_neutral;
}
"""

# ``v_neutral`` (0 normally, 1 for "no current" copper) blends the colormap
# colour toward a flat dim grey so copper that carries no current can be
# greyed out on demand without leaving the per-vertex colormap path. The
# grey matches the dead-end-stub overlay colour (0x60) so a solved dead-end
# island and a FEM-excluded stub read identically when the user toggles
# "Grey no current copper".
_FRAGMENT_SHADER_SRC = """
#version 330 core
in float v_norm;
in float v_alpha;
in float v_neutral;
uniform sampler1D u_cmap;
out vec4 frag_color;
const vec3 NO_CURRENT_GREY = vec3(0.376, 0.376, 0.376);
void main() {
    vec4 c = texture(u_cmap, v_norm);
    vec3 rgb = mix(c.rgb, NO_CURRENT_GREY, clamp(v_neutral, 0.0, 1.0));
    frag_color = vec4(rgb, c.a * v_alpha);
}
"""

# Flat-color line shader for the layer outlines. Vertex carries its own
# RGB so we can pack all layers' outlines into a single GL_LINES batch
# and draw them with one call.
_LINE_VERTEX_SHADER_SRC = """
#version 330 core
layout(location = 0) in vec3 a_position;
layout(location = 1) in vec3 a_color;
uniform mat4 u_mvp;
out vec3 v_color;
void main() {
    gl_Position = u_mvp * vec4(a_position, 1.0);
    v_color = a_color;
}
"""

_LINE_FRAGMENT_SHADER_SRC = """
#version 330 core
in vec3 v_color;
out vec4 frag_color;
void main() {
    frag_color = vec4(v_color, 1.0);
}
"""

# Flat-colour overlay shader — same MVP-only transform as the line shader
# but the per-vertex colour carries a fourth alpha channel so Board
# Features / per-layer all-copper rows can be drawn with partial
# transparency without affecting any of the other line-shader batches.
_OVERLAY_VERTEX_SHADER_SRC = """
#version 330 core
layout(location = 0) in vec3 a_position;
layout(location = 1) in vec4 a_color;
uniform mat4 u_mvp;
out vec4 v_color;
void main() {
    gl_Position = u_mvp * vec4(a_position, 1.0);
    v_color = a_color;
}
"""

_OVERLAY_FRAGMENT_SHADER_SRC = """
#version 330 core
in vec4 v_color;
out vec4 frag_color;
void main() {
    frag_color = v_color;
}
"""

# Thick-line shader for the layer / pad / stub outline batch. The vertex
# stage just MVP-transforms (same inputs as the flat line shader); the
# geometry stage widens each GL_LINES segment into a screen-space quad of
# constant pixel width. glLineWidth past 1.0 is rejected on Core profile
# drivers, so the width has to be real geometry — and doing it per-segment
# in clip space keeps it a constant pixel width at any zoom.
_THICK_LINE_VERTEX_SHADER_SRC = """
#version 330 core
layout(location = 0) in vec3 a_position;
layout(location = 1) in vec3 a_color;
uniform mat4 u_mvp;
out vec3 v_color;
void main() {
    gl_Position = u_mvp * vec4(a_position, 1.0);
    v_color = a_color;
}
"""

_THICK_LINE_GEOMETRY_SHADER_SRC = """
#version 330 core
layout(lines) in;
layout(triangle_strip, max_vertices = 4) out;
in vec3 v_color[];
out vec3 g_color;
uniform vec2 u_viewport;   // render-target size in pixels
uniform float u_half_px;   // half the desired line width, in pixels
void main() {
    vec4 c0 = gl_in[0].gl_Position;
    vec4 c1 = gl_in[1].gl_Position;
    // Segment direction in pixel space, so the width is isotropic on
    // screen regardless of the viewport aspect ratio.
    vec2 n0 = c0.xy / c0.w;
    vec2 n1 = c1.xy / c1.w;
    vec2 dir = (n1 - n0) * u_viewport;
    float len = length(dir);
    if (len < 1e-6) return;          // zero-length segment: emit nothing
    dir /= len;
    // Perpendicular, half-width pixels, converted back to an NDC offset.
    vec2 off = vec2(-dir.y, dir.x) * u_half_px * 2.0 / u_viewport;
    // Apply as a clip-space shift (offset * w keeps it constant in NDC).
    vec4 e0 = vec4(off * c0.w, 0.0, 0.0);
    vec4 e1 = vec4(off * c1.w, 0.0, 0.0);
    gl_Position = c0 + e0; g_color = v_color[0]; EmitVertex();
    gl_Position = c0 - e0; g_color = v_color[0]; EmitVertex();
    gl_Position = c1 + e1; g_color = v_color[1]; EmitVertex();
    gl_Position = c1 - e1; g_color = v_color[1]; EmitVertex();
    EndPrimitive();
}
"""

_THICK_LINE_FRAGMENT_SHADER_SRC = """
#version 330 core
in vec3 g_color;
out vec4 frag_color;
void main() {
    frag_color = vec4(g_color, 1.0);
}
"""

# Fullscreen-pass shader for the supersampling (SSAA) downsample. A single
# clip-space-covering triangle is generated from gl_VertexID alone — no VBO
# needed — and the fragment shader samples the resolved oversize colour
# buffer through hardware LINEAR filtering. At the fixed 2:1 ratio that
# averages each 2x2 source block, i.e. an exact box downsample.
_SS_BLIT_VERTEX_SHADER_SRC = """
#version 330 core
out vec2 v_uv;
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    v_uv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

_SS_BLIT_FRAGMENT_SHADER_SRC = """
#version 330 core
in vec2 v_uv;
uniform sampler2D u_tex;
out vec4 frag_color;
void main() {
    frag_color = texture(u_tex, v_uv);
}
"""


# Viewport background (clear) colour when editor mode is active — a light
# bluish tone so it's unmistakable which mode the user is in. Coder-tunable:
# any "#rrggbb" hex string. Viewer mode keeps the dark substrate below.
_EDITOR_BG_HEX = "#272735"

# Outline width (device px) of the yellow editor-mode selection box drawn
# around the selected component. Matches the selected source / sink
# marker's edge thickness (altium_viewer._EDITOR_MARKER_EDGE_W ×
# _EDITOR_MARKER_SELECT_SCALE) so the two selection cues read as one
# consistent style.
_EDITOR_SELECTION_BOX_PX = 2.0 #3.6

# Width (device px) of the layer / pad / stub outline overlay. glLineWidth
# past 1.0 raises GL_INVALID_VALUE on Core profile drivers, so the width
# is produced by the thick-line geometry shader, which expands each
# GL_LINES segment into a screen-space quad of this constant pixel width —
# uniform at every zoom (a world-space ribbon would turn finely
# tessellated copper outlines spiky). Coder-tunable.
_OUTLINE_WIDTH_PX = 2.0


@dataclass
class LegendRow:
    """One clickable row in the top-right legend chip.

    ``key`` is emitted via :attr:`GLMeshViewer.legendRowClicked` when the
    row is clicked; the host uses it to identify which marker group to
    toggle. ``glyph`` is the single-character symbol drawn in ``color``
    in the swatch column. ``hidden`` styles the row as "visibility off"
    — a diagonal slash drawn across the row, matching the off-state of
    the eye-icon toggle elsewhere in the UI.
    """
    key: str
    label: str
    glyph: str
    color: str
    hidden: bool = False


@dataclass
class MarkerGroup:
    """A batch of identically-styled markers — one per directive role.

    ``xs``/``ys`` are world-space (mm). ``color`` is the fill (HTML hex).
    ``symbol`` is one of ``"o"``, ``"s"``, ``"d"``, ``"star"``. ``size``
    is the diameter in pixels at any zoom level (pxMode-equivalent).
    ``zs`` is the optional per-marker world-z (mm, pre-exaggeration);
    only consulted in 3D mode — if None or omitted, every marker is
    treated as z=0.

    ``world_diameters_mm`` opts the marker into world-space sizing:
    when set, each marker's pixel diameter is
    ``max(world_diameters_mm[i] / mm_per_pixel, min_pixel_diameter)``,
    so the marker visually matches its physical footprint at zoom-in
    while staying visible (overlap is fine and intentional) at
    zoom-out. ``size`` is ignored in that mode.

    ``ring_colors`` adds a second, outer outline per marker in the given
    HTML colour — used to show the copper layer a source / sink / series
    marker sits on. It's one entry per marker (``None``/``""`` skips that
    marker's ring); the glyph is drawn ``ring_width`` px larger behind the
    normal marker so the layer colour reads as a band hugging its edge.
    """
    xs: np.ndarray
    ys: np.ndarray
    color: str
    symbol: str
    size: int
    edge_color: str = "#000000"
    edge_width: float = 0.8
    zs: np.ndarray | None = None
    world_diameters_mm: np.ndarray | None = None
    min_pixel_diameter: float = 0.0
    ring_colors: list[str | None] | None = None
    ring_width: float = 0.0
    # Per-marker slot override: when set, entry ``i`` is the marker's
    # ``(length_mm, width_mm, rotation_deg, rounded)`` capsule (a slotted
    # drill) — ``rounded`` picks an obround vs a square-cornered rectangle —
    # or ``None`` to fall back to the normal symbol. The short axis (width)
    # is still floored at ``min_pixel_diameter`` so a tiny slot stays visible.
    world_obrounds: (list[tuple[float, float, float, bool] | None]
                     | None) = None


def _install_default_surface_format() -> None:
    """Request an OpenGL 3.3 Core context BEFORE the first QOpenGLWidget
    is constructed. Idempotent — call from the module that creates the
    widget once at application startup."""
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    # 24-bit depth buffer is needed for the 3D-mode depth test; in 2D
    # mode we just leave depth-test off so it's effectively unused.
    fmt.setDepthBufferSize(24)
    fmt.setStencilBufferSize(0)
    # 4x MSAA. Copper is world-scaled filled triangles; zoomed far out a
    # thin trace shrinks below a pixel and a single-sample rasteriser only
    # keeps it where a triangle happens to cover the pixel centre — the
    # trace breaks up / partly vanishes. Multisampling tests 4 sub-samples
    # per pixel, so sub-pixel copper renders as a (coverage-weighted)
    # antialiased fragment instead of dropping out. QOpenGLWidget honours
    # this by allocating a multisample FBO and resolving on composition.
    fmt.setSamples(4)
    fmt.setSwapInterval(1)  # vsync on — smoother and easier on the GPU
    QSurfaceFormat.setDefaultFormat(fmt)


class GLMeshViewer(QOpenGLWidget):
    """OpenGL-backed FEM mesh viewer with pan/zoom and overlays."""

    viewChanged = Signal()
    # x_mm, y_mm, inside_widget — emitted on mouseMove (throttling is
    # the caller's responsibility; widget always emits per-event).
    mouseHoveredAt = Signal(float, float, bool)
    # x_mm, y_mm — emitted on a left-click WITHOUT meaningful drag (i.e.
    # press + release with the cursor moving < a few pixels). Useful for
    # "click empty space to clear a highlight" interactions.
    clicked = Signal(float, float)
    # Editor-mode free-marker drag. A left press that the registered
    # hit-test claims fires editorDragStarted; subsequent moves fire
    # editorDragMoved; the release fires editorDragReleased. All carry
    # the cursor's world-mm position. While a drag is active the widget
    # neither pans nor emits ``clicked``.
    editorDragStarted = Signal(float, float)
    editorDragMoved = Signal(float, float)
    editorDragReleased = Signal(float, float)
    # Top-right legend chip row clicked. Carries the row's ``key`` as
    # supplied via :meth:`set_overlay_top_right_legend`. The host uses it
    # to toggle the corresponding marker category's visibility.
    legendRowClicked = Signal(str)

    # Maximum cursor movement (in screen pixels) between press and
    # release that still counts as a click rather than a drag. 4 px is
    # comfortable on both mice and trackpads.
    _CLICK_DRAG_THRESHOLD_PX: float = 4.0

    # --- Supersampling (SSAA) ---------------------------------------------
    # When supersampling is on, the whole scene is rendered to an offscreen
    # FBO at _SS_FACTOR x the widget's device resolution (with _SS_SAMPLES x
    # MSAA on top) and box-downsampled into the widget. A copper trace too
    # thin to survive the rasteriser at native resolution is rasterised
    # _SS_FACTOR x fatter in the oversized buffer, so it registers there,
    # and the downsample turns it into a faint averaged tint rather than
    # dropping it. _SS_FACTOR is pinned at 2: a 2:1 LINEAR downsample is an
    # exact 2x2 box filter, whereas other factors would need a custom
    # kernel. Effective sample count per native pixel is _SS_FACTOR**2 *
    # _SS_SAMPLES (16 here) vs 4 for the plain MSAA path.
    _SS_FACTOR: int = 2
    _SS_SAMPLES: int = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        # --- GPU resources, populated in initializeGL ---
        self._program: QOpenGLShaderProgram | None = None
        self._vao: QOpenGLVertexArrayObject | None = None
        self._pos_vbo: QOpenGLBuffer | None = None
        self._val_vbo: QOpenGLBuffer | None = None
        # Per-vertex alpha (editor-mode copper dimming). Optional — when no
        # alpha array is uploaded, attribute 2 is fed a constant 1.0 so the
        # mesh draws fully opaque exactly as before.
        self._alpha_vbo: QOpenGLBuffer | None = None
        # Per-vertex "neutral" mask (0/1). Optional — when no mask is
        # uploaded, attribute 3 is fed a constant 0.0 so no copper is greyed.
        # A 1.0 entry blends that vertex toward the no-current grey (used by
        # the "Grey no current copper" toggle for solved dead-end islands).
        self._neutral_vbo: QOpenGLBuffer | None = None
        self._ibo: QOpenGLBuffer | None = None
        self._cmap_tex: QOpenGLTexture | None = None
        self._u_mvp_loc: int = -1
        self._u_levels_loc: int = -1
        self._u_cmap_loc: int = -1
        # Line / outline rendering (GL_LINES, flat per-vertex colour).
        self._line_program: QOpenGLShaderProgram | None = None
        self._line_vao: QOpenGLVertexArrayObject | None = None
        self._line_pos_vbo: QOpenGLBuffer | None = None
        self._line_col_vbo: QOpenGLBuffer | None = None
        self._line_u_mvp_loc: int = -1
        # Thick-line program — the outline batch's geometry shader widens
        # each GL_LINES segment to a constant pixel width (the _line_vao /
        # VBOs above are reused; only the shader program differs).
        self._thick_line_program: QOpenGLShaderProgram | None = None
        self._thick_u_mvp_loc: int = -1
        self._thick_u_viewport_loc: int = -1
        self._thick_u_half_px_loc: int = -1
        # Via cylinder rendering (GL_TRIANGLES). Shares the line shader
        # (vec3 pos + vec3 colour, no LUT). Only drawn in 3D mode.
        self._cyl_vao: QOpenGLVertexArrayObject | None = None
        self._cyl_pos_vbo: QOpenGLBuffer | None = None
        self._cyl_col_vbo: QOpenGLBuffer | None = None
        # Current-arrow rendering (GL_LINES). Shares the line shader.
        # A separate batch so it doesn't collide with the layer-outline
        # batch — they're toggled independently from the side panel.
        self._arrow_vao: QOpenGLVertexArrayObject | None = None
        self._arrow_pos_vbo: QOpenGLBuffer | None = None
        self._arrow_col_vbo: QOpenGLBuffer | None = None
        # Stub-copper rendering (GL_TRIANGLES). Shares the line shader.
        # These are flat-coloured polygons representing copper that was
        # excluded from the FEM (dead-end stubs) — drawn so the user can
        # still SEE the copper exists, even though no heatmap value is
        # computed for it. Drawn in both 2D and 3D modes.
        self._stub_vao: QOpenGLVertexArrayObject | None = None
        self._stub_pos_vbo: QOpenGLBuffer | None = None
        self._stub_col_vbo: QOpenGLBuffer | None = None
        # Series-bar rendering (GL_TRIANGLES). Shares the line shader.
        # Each RESISTOR/series directive contributes a gradient-filled
        # rectangle (two triangles) connecting its two terminal pin positions.
        self._sbar_vao: QOpenGLVertexArrayObject | None = None
        self._sbar_pos_vbo: QOpenGLBuffer | None = None
        self._sbar_col_vbo: QOpenGLBuffer | None = None
        # Board-outline rendering (GL_TRIANGLES). Shares the line shader.
        # The PCB's mechanical outline drawn as a thick ribbon of triangles
        # so it's bold and visible at any zoom (and uniformly thick across
        # drivers — glLineWidth past 1.0 is unreliable on Core profiles).
        self._bdrl_vao: QOpenGLVertexArrayObject | None = None
        self._bdrl_pos_vbo: QOpenGLBuffer | None = None
        self._bdrl_col_vbo: QOpenGLBuffer | None = None
        # Overlay-fill rendering (GL_TRIANGLES). Shares the line shader.
        # Flat-coloured polygons for the Heatmap tab's Overlays control
        # when an overlay is set to solid (rather than wire-mesh) fill —
        # filled pads, vias and component bodies. Drawn on top of the
        # heatmap mesh in both 2D and 3D modes.
        self._ovl_vao: QOpenGLVertexArrayObject | None = None
        self._ovl_pos_vbo: QOpenGLBuffer | None = None
        self._ovl_col_vbo: QOpenGLBuffer | None = None
        # Dedicated RGBA overlay shader (vec3 pos + vec4 colour). Kept
        # separate from the shared line shader so Board Features / per-layer
        # all-copper rows can be drawn with per-vertex alpha without the
        # other line-shader batches (cylinders, board outline, stubs, arrows,
        # series bars) having to grow an alpha channel they don't need.
        self._overlay_program: QOpenGLShaderProgram | None = None
        self._overlay_u_mvp_loc: int = -1
        self._gl_initialized: bool = False

        # --- CPU-side cached mesh data (re-uploaded when changed) ---
        self._n_indices: int = 0
        self._n_vertices: int = 0
        self._pending_positions: np.ndarray | None = None
        self._pending_indices: np.ndarray | None = None
        self._pending_values: np.ndarray | None = None
        self._pending_cmap: np.ndarray | None = None
        # Per-vertex alpha batch — None means "draw fully opaque". When set,
        # its length matches the vertex count and _draw_mesh enables
        # blending so dimmed copper (alpha < 1) shows the background through.
        self._pending_alpha: np.ndarray | None = None
        self._n_alpha: int = 0
        self._pending_neutral: np.ndarray | None = None
        self._n_neutral: int = 0
        # Outline batch — the layer / pad / stub outlines. Packed (N, 3)
        # positions + (N, 3) colours, vertices in pairs (GL_LINES), so
        # N == 2 * num_segments. The thick-line geometry shader widens
        # each segment at draw time (see _draw_lines).
        self._n_line_vertices: int = 0
        self._pending_line_positions: np.ndarray | None = None
        self._pending_line_colors: np.ndarray | None = None
        # Via cylinder triangles batch (GL_TRIANGLES). Vertex triples
        # (every 3 = 1 triangle). Drawn in 3D mode only.
        self._n_cyl_vertices: int = 0
        self._pending_cyl_positions: np.ndarray | None = None
        self._pending_cyl_colors: np.ndarray | None = None
        # Current-arrow batch (GL_LINES). Vertex pairs (every 2 = 1
        # segment). One arrow contributes 3 segments (shaft + two head
        # wings) = 6 vertices.
        self._n_arrow_vertices: int = 0
        self._pending_arrow_positions: np.ndarray | None = None
        self._pending_arrow_colors: np.ndarray | None = None
        # Stub-copper batch (GL_TRIANGLES). Vertex triples; flat-coloured
        # polygons of copper that the FEM stub filter dropped.
        self._n_stub_vertices: int = 0
        self._pending_stub_positions: np.ndarray | None = None
        self._pending_stub_colors: np.ndarray | None = None
        # Series-bar batch (GL_TRIANGLES). Two triangles per RESISTOR
        # directive; vertices carry heatmap gradient colors. The first
        # ``_n_sbar_under_vertices`` vertices are drawn BEFORE the heatmap
        # mesh so bottom-side components sit visually beneath the bottom
        # copper in 2D mode; the rest are drawn after (on top).
        self._n_sbar_vertices: int = 0
        self._n_sbar_under_vertices: int = 0
        self._pending_sbar_positions: np.ndarray | None = None
        self._pending_sbar_colors: np.ndarray | None = None
        # Board-outline batch (GL_TRIANGLES). Pre-triangulated ribbon of
        # the PCB's mechanical outline; flat-coloured vertices.
        self._n_bdrl_vertices: int = 0
        self._pending_bdrl_positions: np.ndarray | None = None
        self._pending_bdrl_colors: np.ndarray | None = None
        # Overlay-fill batch (GL_TRIANGLES). Vertex triples; flat-coloured
        # solid-fill polygons for the Overlays control. The leading
        # ``_n_ovl_under_vertices`` are drawn BEFORE the heatmap mesh (2D
        # mode — bottom-side board features behind the bottom copper); the
        # rest after, on top.
        self._n_ovl_vertices: int = 0
        self._n_ovl_under_vertices: int = 0
        self._pending_ovl_positions: np.ndarray | None = None
        self._pending_ovl_colors: np.ndarray | None = None

        # Wireframe overlay over the main heatmap mesh. Off by default;
        # toggled by :meth:`set_show_mesh_edges`. When on, paintGL redraws
        # the mesh's triangles as line segments using the line shader with
        # a constant attribute colour, on top of the filled heatmap.
        self._show_mesh_edges: bool = False

        # Supersampling (SSAA). Off until the host calls
        # :meth:`set_supersampling`. The two offscreen FBOs and the
        # downsample shader program are created lazily in the GL thread
        # (initializeGL / paintGL, where the context is current) and the
        # FBOs are rebuilt whenever the widget size or device-pixel ratio
        # changes.
        self._supersample: bool = False
        self._ss_blit_program: QOpenGLShaderProgram | None = None
        self._ss_blit_u_tex_loc: int = -1
        self._ss_blit_vao: QOpenGLVertexArrayObject | None = None
        self._ss_ms_fbo: QOpenGLFramebufferObject | None = None
        self._ss_resolve_fbo: QOpenGLFramebufferObject | None = None
        self._ss_fbo_size: tuple[int, int] = (0, 0)

        # --- Data extents ---
        self._data_bounds: tuple[float, float, float, float] | None = None
        # Max per-vertex z (in un-exaggerated mm) of the uploaded mesh.
        # Used by the 3D wheel/middle-drag zoom to enforce a hard stop
        # just above the copper top and to keep the apparent zoom step
        # constant as the camera approaches the board.
        self._data_z_max: float = 0.0

        # --- View mode ---
        # "2d" = orthographic top-down (the original/default behaviour).
        # "3d" = perspective view of the stacked layers (z carried in the
        # vertex positions). Toggled by :meth:`set_view_mode`.
        self._view_mode: str = "2d"

        # --- 2D view state (orthographic; "mm per logical pixel") ---
        self._view_center_x: float = 0.0
        self._view_center_y: float = 0.0
        self._mm_per_pixel: float = 1.0
        self._levels: tuple[float, float] = (0.0, 1.0)

        # --- 3D camera (orbital around a target point in world mm) ---
        # Yaw rotates around world +Z (vertical); pitch around the
        # camera's right axis. Distance is camera-to-target in mm.
        # ``_cam_target`` defaults to the centre of the data when
        # ``fit_to_data`` runs.
        self._cam_target: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._cam_yaw_deg: float = self._CAM_DEFAULT_YAW_DEG
        self._cam_pitch_deg: float = self._CAM_DEFAULT_PITCH_DEG  # 0 = side view, 90 = top
        self._cam_distance: float = 1.0    # mm
        # Vertical exaggeration applied ONLY in 3D mode so layer
        # separation is visually distinguishable on a 1-2 mm thick
        # stackup vs a 200+ mm wide board.
        self._vertical_exaggeration: float = 10.0
        # Perspective field-of-view (vertical), in degrees.
        self._cam_fov_deg: float = 35.0

        # --- Mouse interaction state ---
        # Left-button press: pan in 2D, click (without drag) in either mode.
        self._press_origin: QPointF | None = None
        self._press_center: tuple[float, float] = (0.0, 0.0)
        self._is_panning: bool = False
        # Right-button press: pan (both modes) / rotate (3D + Shift) —
        # snapshots the view state at press time so dx/dy are cumulative
        # from the press point rather than incremental.
        self._right_press_origin: QPointF | None = None
        self._right_press_center: tuple[float, float] = (0.0, 0.0)
        self._right_press_mpp: float = 1.0
        self._right_press_target: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._right_press_yaw: float = 0.0
        self._right_press_pitch: float = 0.0
        self._right_press_shift: bool = False
        self._right_is_dragging: bool = False
        # Middle-button press: drag up = zoom in, drag down = zoom out.
        # In 2D this scales mm/pixel around the world point under the
        # press cursor. In 3D this dollies the camera in/out (pivots
        # around the camera target, which is the simplest sensible
        # behaviour without doing per-frame ray-casts).
        self._middle_press_origin: QPointF | None = None
        self._middle_press_world: tuple[float, float] = (0.0, 0.0)
        self._middle_press_mpp: float = 1.0
        self._middle_press_distance: float = 1.0
        # Last hover position in widget-logical pixels — kept so the host
        # can re-unproject the cursor against an arbitrary z-plane (used
        # by the 3D-mode probe to pick the layer under the cursor without
        # round-tripping through ``screen_to_world``'s 2D-only path).
        self._last_hover_pixel: tuple[float, float] = (0.0, 0.0)

        # --- Overlay state (drawn via QPainter on top of GL) ---
        self._overlay_top_left_html: str = ""
        self._overlay_top_right_html: str = ""
        # Structured top-right legend (rows with per-row click handling /
        # hidden-state slash). When non-empty, replaces the plain-HTML
        # top-right chip. Filled by :meth:`set_overlay_top_right_legend`.
        self._overlay_top_right_legend: list[LegendRow] = []
        # Extra horizontal inset (px) for the top-right chip so the host
        # can push the legend left when a floating side panel overlays
        # the right edge. Set via :meth:`set_legend_right_inset`.
        self._legend_right_inset: float = 0.0
        # Hit-test rects for the structured legend, rebuilt each paint by
        # :meth:`_draw_legend_chip`. Each entry: (widget-pixel rect, key).
        self._legend_row_rects: list[tuple[QRectF, str]] = []
        # Set when a left press lands on a legend row — the matching
        # release is then swallowed so the host's empty-space ``clicked``
        # handler doesn't also fire (which would e.g. clear the Vias-tab
        # jump highlight as a side effect of toggling a legend row).
        self._legend_press_consumed: bool = False
        self._markers: list[MarkerGroup] = []
        # Overlay text labels (reference designators for the Overlays
        # control). Each entry: ``{"x", "y", "z", "text", "color",
        # "height_mm", "rotation_deg"}``. Drawn by the QPainter overlay
        # pass, projected through :meth:`world_to_screen`.
        self._overlay_labels: list[dict] = []
        # Measurement-line endpoints in world mm: (x0, y0, x1, y1) or
        # ``None`` when no measurement is active. Drawn by the QPainter
        # overlay pass as a thin white line on top of everything else.
        self._measurement_line: tuple[float, float, float, float] | None = None

        # --- Background (clear) colour — dark theme to match the rest ---
        self._bg_r = 0x1f / 255.0
        self._bg_g = 0x1f / 255.0
        self._bg_b = 0x1f / 255.0

        # --- Editor mode ---
        # Viewer mode (default) uses the dark substrate above; editor mode
        # swaps in a bluish background + a faint world-mm grid so it is
        # unmistakable which mode the user is in. ``paintGL`` picks the
        # clear colour each frame from ``_editor_mode``.
        self._editor_mode: bool = False
        # World-space (x0, y0, x1, y1) bbox of the selected editor-mode
        # component, drawn as a yellow selection box; None when nothing
        # (or a non-component) is selected.
        self._editor_selection_bbox: tuple[
            float, float, float, float] | None = None
        # World-mm closed rings outlining a click-selected copper primitive
        # (viewer mode). Drawn as a dashed yellow polygon over the copper.
        # ``None`` when nothing is selected. A track / arc gets one ring; a
        # region-with-holes gets the outer ring plus one ring per hole.
        self._primitive_selection_rings: list[
            list[tuple[float, float]]] | None = None
        # Free-marker drag: the host registers a pure hit-test callback
        # (``world_x, world_y -> bool``); a left press over a marker is
        # claimed as a drag gesture instead of a pan / click, and the
        # editorDrag* signals report it back so the host can move the
        # marker (constrained to copper) without rebuilding the mesh.
        self._editor_drag_hit_test = None
        self._editor_drag_active: bool = False
        self._editor_cursor_state: str = "default"
        self._bg_normal = (self._bg_r, self._bg_g, self._bg_b)
        # Editor-mode clear colour, from the coder-tunable _EDITOR_BG_HEX.
        self._bg_editor = QColor(_EDITOR_BG_HEX).getRgbF()[:3]

        # --- 3D camera animation state ---
        # Used by :meth:`reset_3d_view` to interpolate yaw / pitch /
        # target / distance from the current camera to the reset pose
        # over ~0.5 s instead of snapping. ``_view_anim`` is the active
        # from/to snapshot dict, or ``None`` when nothing is animating.
        self._view_anim: dict | None = None
        self._view_anim_timer: QTimer | None = None
        self._view_anim_elapsed: QElapsedTimer | None = None

    # ------------------------------------------------------------------
    # Public data interface
    # ------------------------------------------------------------------

    def set_mesh(self, xs: np.ndarray, ys: np.ndarray,
                 tris: np.ndarray,
                 data_bounds: tuple[float, float, float, float] | None = None,
                 zs: np.ndarray | None = None,
                 ) -> None:
        """Upload a new triangulated mesh to the GPU.

        ``xs``/``ys`` are 1-D float arrays of vertex coordinates (mm).
        ``zs`` is an optional 1-D float array of per-vertex z (defaults
        to zeros for the 2D case). ``tris`` is an (M, 3) int array of
        vertex indices. ``data_bounds`` is the (xmin, xmax, ymin, ymax)
        rectangle used for ``fit_to_data``.
        """
        positions = np.empty((xs.size, 3), dtype=np.float32)
        positions[:, 0] = xs
        positions[:, 1] = ys
        if zs is not None:
            positions[:, 2] = zs
            self._data_z_max = float(np.max(zs)) if zs.size else 0.0
        else:
            positions[:, 2] = 0.0
            self._data_z_max = 0.0
        indices = np.asarray(tris, dtype=np.uint32).ravel()
        self._pending_positions = positions
        self._pending_indices = indices
        self._n_vertices = positions.shape[0]
        self._n_indices = indices.size
        # A new mesh invalidates any per-vertex alpha (vertex count and
        # ordering changed); the host re-pushes it via set_vertex_alpha.
        self._pending_alpha = None
        self._n_alpha = 0
        # Same for the per-vertex neutral mask — re-pushed via
        # set_vertex_neutral after each set_mesh.
        self._pending_neutral = None
        self._n_neutral = 0
        if data_bounds is not None:
            self._data_bounds = (float(data_bounds[0]), float(data_bounds[1]),
                                  float(data_bounds[2]), float(data_bounds[3]))
        else:
            self._data_bounds = (float(xs.min()), float(xs.max()),
                                  float(ys.min()), float(ys.max()))
        # Initial value array of zeros until set_values is called; lets
        # the mesh render in the lowest colormap entry rather than crash.
        self._pending_values = np.zeros(self._n_vertices, dtype=np.float32)
        self.update()

    def set_view_mode(self, mode: str) -> None:
        """Switch between ``"2d"`` (orthographic top-down) and ``"3d"``
        (perspective view of the stacked layers). Triggers a fit on the
        first switch into 3D so the camera frames the data."""
        mode = mode.lower()
        if mode not in ("2d", "3d"):
            raise ValueError(f"unknown view mode: {mode!r}")
        if mode == self._view_mode:
            return
        self._view_mode = mode
        if mode == "3d":
            self._fit_3d_to_data()
        self.viewChanged.emit()
        self.update()

    def set_view_mode_preserving(self, mode: str) -> None:
        """Switch view mode while keeping the same world region framed.
        Unlike :meth:`set_view_mode`, this does NOT re-fit on entering 3D.

        2D → 3D: the current 2D centre becomes the 3D look-at point at
        z=0; the camera goes to the default top-down orientation; the
        camera distance is picked so the world height visible at the
        focal plane matches what 2D was showing.

        3D → 2D: the 3D look-at point (projected onto z=0) becomes the
        2D centre; ``mm_per_pixel`` is derived from the current camera
        distance + FOV so the apparent scale matches.

        Equivalent to :meth:`set_view_mode` when already in the target
        mode (no-op).
        """
        mode = mode.lower()
        if mode not in ("2d", "3d"):
            raise ValueError(f"unknown view mode: {mode!r}")
        if mode == self._view_mode:
            return
        h_px = max(1, self.height())
        fov_half = math.radians(self._cam_fov_deg) * 0.5
        tan_half = max(math.tan(fov_half), 1e-9)
        if mode == "3d":
            cx, cy, mpp = (self._view_center_x, self._view_center_y,
                            self._mm_per_pixel)
            half_world_h = h_px * mpp * 0.5
            self._cam_target = (cx, cy, 0.0)
            self._cam_yaw_deg = self._CAM_DEFAULT_YAW_DEG
            self._cam_pitch_deg = self._CAM_DEFAULT_PITCH_DEG
            self._cam_distance = max(half_world_h / tan_half, 1.0)
        else:
            tx, ty, _ = self._cam_target
            half_world_h = self._cam_distance * tan_half
            self._view_center_x = float(tx)
            self._view_center_y = float(ty)
            self._mm_per_pixel = max(2.0 * half_world_h / h_px, 1e-9)
        self._view_mode = mode
        self.viewChanged.emit()
        self.update()

    def view_mode(self) -> str:
        return self._view_mode

    def set_vertical_exaggeration(self, factor: float) -> None:
        """Set the multiplier applied to per-vertex z values in 3D mode.
        Without exaggeration the ~1.6 mm stackup is invisible next to a
        200+ mm wide board. Defaults to 50x."""
        if not math.isfinite(factor) or factor <= 0:
            return
        self._vertical_exaggeration = float(factor)
        if self._view_mode == "3d":
            self.viewChanged.emit()
            self.update()

    def vertical_exaggeration(self) -> float:
        return self._vertical_exaggeration

    # --- 3Dconnexion NavLib camera bridge --------------------------------

    def navlib_camera_matrix(self) -> list[list[float]]:
        """4×4 row-major camera-to-world matrix for NavLib."""
        from fypa.navlib_camera import camera_matrix_2d, camera_matrix_3d

        w_px = max(1, self.width())
        h_px = max(1, self.height())
        if self._view_mode == "3d":
            return camera_matrix_3d(
                self._cam_target,
                self._cam_yaw_deg,
                self._cam_pitch_deg,
                self._cam_distance,
            )
        return camera_matrix_2d(
            self._view_center_x,
            self._view_center_y,
            self._mm_per_pixel,
            w_px,
            h_px,
        )

    def apply_navlib_camera_matrix(
        self, matrix: list[list[float]],
    ) -> None:
        """Apply a NavLib camera-to-world matrix to the current view."""
        from fypa.navlib_camera import (
            parse_camera_matrix_2d,
            parse_camera_matrix_3d,
        )

        w_px = max(1, self.width())
        h_px = max(1, self.height())
        if self._view_mode == "3d":
            yaw, pitch, dist = parse_camera_matrix_3d(
                matrix, self._cam_target,
            )
            self._cam_yaw_deg = max(-180.0, min(180.0, yaw))
            self._cam_pitch_deg = max(-89.0, min(89.0, pitch))
            self._cam_distance = max(min(dist, 1e7), 0.01)
        else:
            cx, cy, mpp = parse_camera_matrix_2d(matrix, w_px, h_px)
            self._view_center_x = cx
            self._view_center_y = cy
            self._mm_per_pixel = max(min(mpp, 1e6), 1e-9)
        self.viewChanged.emit()
        self.update()

    def navlib_model_extents(
        self,
    ) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """NavLib model bounding box (min, max) in world mm."""
        from fypa.navlib_camera import model_extents_from_bounds

        return model_extents_from_bounds(
            self._data_bounds, self._data_z_max,
        )

    def navlib_view_extents(
        self,
    ) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """NavLib orthographic view extents (min, max) in world mm."""
        from fypa.navlib_camera import view_extents_2d

        return view_extents_2d(
            self._view_center_x,
            self._view_center_y,
            self._mm_per_pixel,
            max(1, self.width()),
            max(1, self.height()),
        )

    def apply_navlib_view_extents(
        self,
        pmin: tuple[float, float, float],
        pmax: tuple[float, float, float],
    ) -> None:
        """Apply NavLib orthographic zoom/pan via view extents (2D mode)."""
        from fypa.navlib_camera import apply_view_extents_2d

        cx, cy, mpp = apply_view_extents_2d(
            pmin, pmax,
            max(1, self.width()),
            max(1, self.height()),
        )
        self.set_view_center_scale(cx, cy, mpp)

    def navlib_pointer_world(self) -> tuple[float, float, float]:
        """World-space point under the cursor for NavLib zoom pivot."""
        hx, hy = self._last_hover_pixel
        if self._view_mode == "3d":
            wx, wy = self.screen_to_world_at_z(hx, hy, self._cam_target[2])
            return wx, wy, self._cam_target[2]
        wx, wy = self.screen_to_world(hx, hy)
        return wx, wy, 0.0

    # SpaceMouse axis motion (Linux spnav path) — normalized device axes
    # in roughly [-1, 1], integrated per frame.
    _SPACEMOUSE_PAN_SPEED_2D: float = 800.0
    _SPACEMOUSE_ZOOM_SPEED: float = 1.8
    _SPACEMOUSE_ORBIT_SPEED: float = 120.0
    _SPACEMOUSE_DEADZONE: float = 0.05

    def apply_spacemouse_motion(
        self,
        tx: float,
        ty: float,
        tz: float,
        rx: float,
        ry: float,
        rz: float,
        dt: float,
    ) -> None:
        """Apply a 6-DOF SpaceMouse sample (Linux spnav backend)."""
        def _dz(v: float) -> float:
            return 0.0 if abs(v) < self._SPACEMOUSE_DEADZONE else v

        tx, ty, tz = _dz(tx), _dz(ty), _dz(tz)
        rx, ry, rz = _dz(rx), _dz(ry), _dz(rz)
        if not any((tx, ty, tz, rx, ry, rz)):
            return
        dt = max(dt, 1e-3)
        if self._view_mode == "2d":
            scale = self._SPACEMOUSE_PAN_SPEED_2D * self._mm_per_pixel * dt
            self._view_center_x -= tx * scale
            self._view_center_y += ty * scale
            if tz:
                factor = math.exp(-tz * self._SPACEMOUSE_ZOOM_SPEED * dt)
                hx, hy = self._last_hover_pixel
                wx, wy = self.screen_to_world(hx, hy)
                new_mpp = max(min(self._mm_per_pixel * factor, 1e6), 1e-9)
                new_cx = wx - (hx - self.width() * 0.5) * new_mpp
                new_cy = wy + (hy - self.height() * 0.5) * new_mpp
                self.set_view_center_scale(new_cx, new_cy, new_mpp)
            else:
                self.viewChanged.emit()
                self.update()
            return

        # 3D: pan in view plane, dolly on tz, orbit on rx/ry.
        h_px = max(1, self.height())
        mm_per_px = (2.0 * self._cam_distance * math.tan(
            math.radians(self._cam_fov_deg) * 0.5)) / h_px
        pan_scale = mm_per_px * self._SPACEMOUSE_PAN_SPEED_2D * 0.15 * dt
        yaw = math.radians(self._cam_yaw_deg)
        pitch = math.radians(self._cam_pitch_deg)
        right = (math.cos(yaw), math.sin(yaw), 0.0)
        up = (-math.sin(pitch) * math.sin(yaw),
              math.sin(pitch) * math.cos(yaw),
              math.cos(pitch))
        tx0, ty0, tz0 = self._cam_target
        self._cam_target = (
            tx0 + tx * pan_scale * right[0] - ty * pan_scale * up[0],
            ty0 + tx * pan_scale * right[1] - ty * pan_scale * up[1],
            tz0 + tx * pan_scale * right[2] - ty * pan_scale * up[2],
        )
        if tz:
            factor = math.exp(-tz * self._SPACEMOUSE_ZOOM_SPEED * dt)
            self._cam_distance = self._dolly_cam_distance(
                self._cam_distance, factor,
            )
        if rx or ry:
            self._cam_yaw_deg -= rx * self._SPACEMOUSE_ORBIT_SPEED * dt
            self._cam_pitch_deg = max(
                -89.0,
                min(89.0,
                    self._cam_pitch_deg + ry * self._SPACEMOUSE_ORBIT_SPEED * dt),
            )
        self.viewChanged.emit()
        self.update()

    def set_values(self, values: np.ndarray) -> None:
        """Update the per-vertex scalar values that the fragment shader
        uses to look up colours. Must match the vertex count from the
        most recent :meth:`set_mesh` call."""
        arr = np.asarray(values, dtype=np.float32)
        if arr.size != self._n_vertices:
            raise ValueError(
                f"set_values length {arr.size} doesn't match vertex count "
                f"{self._n_vertices}"
            )
        self._pending_values = arr
        self.update()

    def set_levels(self, vmin: float, vmax: float) -> None:
        """Set the colormap window. Cheap — just changes a uniform."""
        if vmax <= vmin:
            vmax = vmin + 1e-12
        self._levels = (float(vmin), float(vmax))
        self.update()

    def set_vertex_alpha(self, alphas: np.ndarray | None) -> None:
        """Set (or clear) the per-vertex alpha used to dim copper in editor
        mode. ``alphas`` must match the vertex count from the most recent
        :meth:`set_mesh`; pass ``None`` to go back to fully-opaque drawing."""
        if alphas is None:
            self._pending_alpha = None
            self._n_alpha = 0
            self.update()
            return
        arr = np.asarray(alphas, dtype=np.float32)
        if arr.size != self._n_vertices:
            raise ValueError(
                f"set_vertex_alpha length {arr.size} doesn't match vertex "
                f"count {self._n_vertices}"
            )
        self._pending_alpha = arr
        self._n_alpha = arr.size
        self.update()

    def set_vertex_neutral(self, neutral: np.ndarray | None) -> None:
        """Set (or clear) the per-vertex neutral mask used to grey copper
        that carries no current. ``neutral`` is a 0/1 (or 0..1) float array
        matching the vertex count from the most recent :meth:`set_mesh`;
        pass ``None`` to draw every vertex from the colormap as usual."""
        if neutral is None:
            self._pending_neutral = None
            self._n_neutral = 0
            self.update()
            return
        arr = np.asarray(neutral, dtype=np.float32)
        if arr.size != self._n_vertices:
            raise ValueError(
                f"set_vertex_neutral length {arr.size} doesn't match vertex "
                f"count {self._n_vertices}"
            )
        self._pending_neutral = arr
        self._n_neutral = arr.size
        self.update()

    def set_colormap(self, lut_rgba_256: np.ndarray) -> None:
        """Replace the 1-D colormap. ``lut_rgba_256`` must be a
        (256, 4) uint8 array of RGBA values."""
        arr = np.ascontiguousarray(lut_rgba_256, dtype=np.uint8)
        if arr.shape != (256, 4):
            raise ValueError("LUT must be (256, 4) uint8")
        self._pending_cmap = arr
        self.update()

    def clear_mesh(self) -> None:
        """Drop all mesh data — the canvas paints background only."""
        self._n_indices = 0
        self._n_vertices = 0
        self._pending_positions = None
        self._pending_indices = None
        self._pending_values = None
        self._data_bounds = None
        self._data_z_max = 0.0
        self.update()

    def set_show_mesh_edges(self, enabled: bool) -> None:
        """Toggle drawing the FEM triangle edges on top of the filled
        heatmap. No mesh re-upload — just flips a flag that paintGL reads
        to decide whether to do a second wireframe pass over the same
        index buffer."""
        flag = bool(enabled)
        if flag == self._show_mesh_edges:
            return
        self._show_mesh_edges = flag
        self.update()

    def set_supersampling(self, enabled: bool) -> None:
        """Enable / disable supersampled (SSAA) rendering.

        When on, the scene is drawn to an offscreen buffer at
        :data:`_SS_FACTOR` x the device resolution and box-downsampled into
        the widget, so sub-pixel copper survives zoom-out as a faint
        averaged tint instead of being dropped by the rasteriser. Costs GPU
        memory + fill rate while enabled — hence a user-facing toggle. The
        offscreen buffers are freed (on the next paint) when turned off."""
        flag = bool(enabled)
        if flag == self._supersample:
            return
        self._supersample = flag
        self.update()

    def set_outlines(self, positions: np.ndarray,
                     colors: np.ndarray) -> None:
        """Push a batch of outline line segments to the GPU.

        ``positions`` is an (N, 2) OR (N, 3) float array; vertices come
        in pairs (``GL_LINES``), so N == 2 * num_segments. A (N, 2)
        array is broadcast to z=0. ``colors`` is an (N, 3) float array
        of RGB triples in [0..1]. The two arrays must have matching N.

        The segments are widened to a visible line at draw time by the
        thick-line geometry shader (see :meth:`_draw_lines`).
        """
        pos2_or_3 = np.ascontiguousarray(positions, dtype=np.float32)
        if pos2_or_3.ndim != 2 or pos2_or_3.shape[1] not in (2, 3):
            raise ValueError("positions must be (N, 2) or (N, 3)")
        if pos2_or_3.shape[1] == 2:
            # Promote to (N, 3) with z=0 — the shader expects vec3.
            pos = np.zeros((pos2_or_3.shape[0], 3), dtype=np.float32)
            pos[:, :2] = pos2_or_3
        else:
            pos = pos2_or_3
        col = np.ascontiguousarray(colors, dtype=np.float32)
        if col.ndim != 2 or col.shape[1] != 3:
            raise ValueError("colors must be (N, 3)")
        if pos.shape[0] != col.shape[0]:
            raise ValueError("positions and colors length mismatch")
        self._pending_line_positions = pos
        self._pending_line_colors = col
        self._n_line_vertices = pos.shape[0]
        self.update()

    def clear_outlines(self) -> None:
        """Drop all outline data — the canvas paints just the heatmap
        and any markers / overlay text."""
        self._n_line_vertices = 0
        self._pending_line_positions = None
        self._pending_line_colors = None
        self.update()

    def set_cylinders(self, positions: np.ndarray,
                      colors: np.ndarray) -> None:
        """Push a batch of cylinder triangles to the GPU.

        ``positions`` is an (N, 3) float array of vertex triples
        (consecutive triples form one triangle), so N must be a
        multiple of 3. ``colors`` is an (N, 3) RGB array in [0..1]
        matching ``positions`` length.

        Drawn only in 3D mode (skipped in 2D where stacked cylinders
        would just be confusing overlapping circles).
        """
        pos = np.ascontiguousarray(positions, dtype=np.float32)
        col = np.ascontiguousarray(colors, dtype=np.float32)
        if pos.ndim != 2 or pos.shape[1] != 3 or pos.shape[0] % 3 != 0:
            raise ValueError("positions must be (3*k, 3)")
        if col.shape != pos.shape:
            raise ValueError("colors shape must match positions")
        self._pending_cyl_positions = pos
        self._pending_cyl_colors = col
        self._n_cyl_vertices = pos.shape[0]
        self.update()

    def clear_cylinders(self) -> None:
        self._n_cyl_vertices = 0
        self._pending_cyl_positions = None
        self._pending_cyl_colors = None
        self.update()

    def set_arrows(self, positions: np.ndarray,
                   color: tuple[float, float, float] = (1.0, 1.0, 1.0),
                   ) -> None:
        """Push a batch of arrow line segments to the GPU.

        ``positions`` is an (N, 2) OR (N, 3) float array of GL_LINES
        vertices, so N must be even. Each arrow contributes 6 vertices
        (shaft + two head wings, three segments). A (N, 2) array is
        broadcast to z=0. ``color`` is a single RGB tuple in [0..1]
        applied to every vertex — arrows are drawn flat-coloured so the
        underlying heatmap colour stays readable.
        """
        pos2_or_3 = np.ascontiguousarray(positions, dtype=np.float32)
        if pos2_or_3.ndim != 2 or pos2_or_3.shape[1] not in (2, 3):
            raise ValueError("positions must be (N, 2) or (N, 3)")
        if pos2_or_3.shape[0] % 2 != 0:
            raise ValueError("positions length must be even (GL_LINES pairs)")
        if pos2_or_3.shape[1] == 2:
            pos = np.zeros((pos2_or_3.shape[0], 3), dtype=np.float32)
            pos[:, :2] = pos2_or_3
        else:
            pos = pos2_or_3
        n = pos.shape[0]
        cols = np.broadcast_to(
            np.asarray(color, dtype=np.float32), (n, 3)
        ).copy()
        self._pending_arrow_positions = pos
        self._pending_arrow_colors = cols
        self._n_arrow_vertices = n
        self.update()

    def clear_arrows(self) -> None:
        self._n_arrow_vertices = 0
        self._pending_arrow_positions = None
        self._pending_arrow_colors = None
        self.update()

    def set_stub_triangles(self, positions: np.ndarray,
                           colors: np.ndarray) -> None:
        """Push a batch of stub-copper triangles to the GPU.

        ``positions`` is an (N, 3) float array of vertex triples
        (consecutive triples form one triangle), so N must be a multiple
        of 3. ``colors`` is an (N, 3) RGB array in [0..1] matching
        ``positions`` length.

        Drawn in both 2D and 3D modes — the user wants to see this
        copper exists even though no FEM result is computed for it.
        """
        pos = np.ascontiguousarray(positions, dtype=np.float32)
        col = np.ascontiguousarray(colors, dtype=np.float32)
        if pos.ndim != 2 or pos.shape[1] != 3 or pos.shape[0] % 3 != 0:
            raise ValueError("positions must be (3*k, 3)")
        if col.shape != pos.shape:
            raise ValueError("colors shape must match positions")
        self._pending_stub_positions = pos
        self._pending_stub_colors = col
        self._n_stub_vertices = pos.shape[0]
        self.update()

    def clear_stub_triangles(self) -> None:
        self._n_stub_vertices = 0
        self._pending_stub_positions = None
        self._pending_stub_colors = None
        self.update()

    def set_overlay_fills(self, positions: np.ndarray,
                          colors: np.ndarray,
                          under_mesh_count: int = 0) -> None:
        """Push a batch of solid-fill overlay triangles to the GPU.

        Used by the Heatmap tab's Overlays control for overlays set to
        solid (rather than wire-mesh) fill — filled pads, vias and
        component bodies. ``positions`` is an (N, 3) float array of vertex
        triples (consecutive triples form one triangle), so N must be a
        multiple of 3. ``colors`` is an (N, 4) RGBA array in [0..1] of
        matching length — the alpha channel drives the per-row
        transparency control. An (N, 3) RGB array is also accepted and
        treated as fully opaque.

        ``under_mesh_count`` (must be a multiple of 3) selects how many
        leading vertices are drawn BEFORE the heatmap mesh — used in 2D
        mode to push bottom-side board features behind the bottom copper.
        The remaining vertices are drawn after the mesh, on top.
        """
        pos = np.ascontiguousarray(positions, dtype=np.float32)
        col = np.ascontiguousarray(colors, dtype=np.float32)
        if pos.ndim != 2 or pos.shape[1] != 3 or pos.shape[0] % 3 != 0:
            raise ValueError("positions must be (3*k, 3)")
        # Accept either RGB or RGBA. RGB inputs are promoted to fully
        # opaque so older call sites keep working unchanged.
        if col.ndim != 2 or col.shape[0] != pos.shape[0] or col.shape[1] not in (3, 4):
            raise ValueError("colors must be (N, 3) or (N, 4) matching positions")
        if col.shape[1] == 3:
            rgba = np.empty((col.shape[0], 4), dtype=np.float32)
            rgba[:, :3] = col
            rgba[:, 3] = 1.0
            col = rgba
        if not (0 <= under_mesh_count <= pos.shape[0]) or under_mesh_count % 3 != 0:
            raise ValueError("under_mesh_count must be a multiple of 3 in [0, N]")
        self._pending_ovl_positions = pos
        self._pending_ovl_colors = col
        self._n_ovl_vertices = pos.shape[0]
        self._n_ovl_under_vertices = int(under_mesh_count)
        self.update()

    def clear_overlay_fills(self) -> None:
        self._n_ovl_vertices = 0
        self._n_ovl_under_vertices = 0
        self._pending_ovl_positions = None
        self._pending_ovl_colors = None
        self.update()

    def set_overlay_labels(self, labels: list[dict]) -> None:
        """Set the overlay text labels (reference designators).

        ``labels`` is a list of dicts, each with ``x``/``y`` (world mm),
        optional ``z`` (world mm, default 0), ``text``, ``color`` (a
        ``#rrggbb`` string), ``height_mm`` (character height in world mm)
        and optional ``rotation_deg``. Drawn by the QPainter overlay pass.
        """
        self._overlay_labels = list(labels or [])
        self.update()

    def clear_overlay_labels(self) -> None:
        self._overlay_labels = []
        self.update()

    def set_series_bars(self, positions: np.ndarray,
                        colors: np.ndarray,
                        under_mesh_count: int = 0) -> None:
        """Push a batch of series-bar triangles to the GPU.

        Each RESISTOR directive contributes 6 vertices (two triangles
        forming a gradient-filled rectangle between its two terminal
        pin positions). ``positions`` is (N, 3) float32; ``colors``
        is (N, 3) RGB float32. Both must have matching N and N % 6 == 0.

        ``under_mesh_count`` (must be a multiple of 3) selects how many
        leading vertices are drawn BEFORE the heatmap mesh — used in 2D
        mode to push bottom-side bars behind the bottom copper. The
        remaining vertices are drawn after the mesh, on top.
        """
        pos = np.ascontiguousarray(positions, dtype=np.float32)
        col = np.ascontiguousarray(colors, dtype=np.float32)
        if pos.ndim != 2 or pos.shape[1] != 3 or pos.shape[0] % 3 != 0:
            raise ValueError("positions must be (3*k, 3)")
        if col.shape != pos.shape:
            raise ValueError("colors shape must match positions")
        if not (0 <= under_mesh_count <= pos.shape[0]) or under_mesh_count % 3 != 0:
            raise ValueError("under_mesh_count must be a multiple of 3 in [0, N]")
        self._pending_sbar_positions = pos
        self._pending_sbar_colors = col
        self._n_sbar_vertices = pos.shape[0]
        self._n_sbar_under_vertices = int(under_mesh_count)
        self.update()

    def clear_series_bars(self) -> None:
        self._n_sbar_vertices = 0
        self._n_sbar_under_vertices = 0
        self._pending_sbar_positions = None
        self._pending_sbar_colors = None
        self.update()

    def set_board_outline(self, positions: np.ndarray,
                           colors: np.ndarray) -> None:
        """Push a triangulated board-outline ribbon to the GPU.

        Vertices come as triples (GL_TRIANGLES), so ``positions`` is (N, 3)
        float and N is a multiple of 3. ``colors`` is (N, 4) RGBA float in
        [0..1] (or (N, 3) RGB, promoted to fully opaque) matching
        ``positions`` length. The caller is responsible for the ribbon
        triangulation (typically the polyline expanded by a fixed mm
        half-width). Drawn in both 2D and 3D modes; rendered through the
        RGBA overlay shader so the alpha channel drives the board-outline
        row's Transparency control.
        """
        pos = np.ascontiguousarray(positions, dtype=np.float32)
        col = np.ascontiguousarray(colors, dtype=np.float32)
        if pos.ndim != 2 or pos.shape[1] != 3 or pos.shape[0] % 3 != 0:
            raise ValueError("positions must be (3*k, 3)")
        if col.ndim != 2 or col.shape[0] != pos.shape[0] or col.shape[1] not in (3, 4):
            raise ValueError("colors must be (N, 3) or (N, 4) matching positions")
        if col.shape[1] == 3:
            rgba = np.empty((col.shape[0], 4), dtype=np.float32)
            rgba[:, :3] = col
            rgba[:, 3] = 1.0
            col = rgba
        self._pending_bdrl_positions = pos
        self._pending_bdrl_colors = col
        self._n_bdrl_vertices = pos.shape[0]
        self.update()

    def clear_board_outline(self) -> None:
        self._n_bdrl_vertices = 0
        self._pending_bdrl_positions = None
        self._pending_bdrl_colors = None
        self.update()

    # ------------------------------------------------------------------
    # View interface
    # ------------------------------------------------------------------

    def fit_to_data(self, padding: float = 1.05) -> None:
        """Pick the largest mm-per-pixel that fits ``_data_bounds`` into
        the current widget size with a small margin, then centre."""
        if self._data_bounds is None:
            return
        x_min, x_max, y_min, y_max = self._data_bounds
        self.fit_to_bounds(x_min, x_max, y_min, y_max, padding=padding)

    def fit_to_bounds(self, x_min: float, x_max: float,
                      y_min: float, y_max: float,
                      padding: float = 1.05) -> None:
        """Pick the largest mm-per-pixel that fits the given world rectangle
        into the current widget size with a small margin, then centre.

        Unlike :meth:`fit_to_data` (which keys off the pushed mesh extent),
        this takes explicit bounds so callers can frame e.g. the board
        outline rather than just the copper that happens to be meshed.
        """
        if not all(math.isfinite(v) for v in (x_min, x_max, y_min, y_max)):
            return
        w_px = max(1, self.width())
        h_px = max(1, self.height())
        board_w = max(x_max - x_min, 1e-9)
        board_h = max(y_max - y_min, 1e-9)
        mpp = max(board_w * padding / w_px, board_h * padding / h_px)
        cx = (x_min + x_max) * 0.5
        cy = (y_min + y_max) * 0.5
        self.set_view_center_scale(cx, cy, mpp)

    def set_view_center_scale(self, cx: float, cy: float,
                              mm_per_pixel: float) -> None:
        """Explicit view: centre + scale. Emits :attr:`viewChanged`."""
        if mm_per_pixel <= 0 or not math.isfinite(mm_per_pixel):
            return
        self._view_center_x = float(cx)
        self._view_center_y = float(cy)
        self._mm_per_pixel = float(mm_per_pixel)
        self.viewChanged.emit()
        self.update()

    def view_center_scale(self) -> tuple[float, float, float]:
        return self._view_center_x, self._view_center_y, self._mm_per_pixel

    def view_range(self) -> tuple[float, float, float, float]:
        """Visible world rectangle as (x_min, x_max, y_min, y_max)."""
        half_w = self.width() * self._mm_per_pixel * 0.5
        half_h = self.height() * self._mm_per_pixel * 0.5
        return (self._view_center_x - half_w, self._view_center_x + half_w,
                self._view_center_y - half_h, self._view_center_y + half_h)

    def data_bounds(self) -> tuple[float, float, float, float] | None:
        return self._data_bounds

    # --- 3D camera helpers -------------------------------------------------

    # Default 3D camera angles — restored by :meth:`reset_3d_view`.
    # Default 3D view = essentially top-down so it visually matches 2D
    # mode on first entry / reset. 89° (not 90°) avoids gimbal lock —
    # at exactly 90° the world-up (0, 0, 1) would be parallel to the
    # view direction and lookAt becomes degenerate. The 1° tilt is
    # imperceptible at typical viewer distances.
    _CAM_DEFAULT_YAW_DEG: float = 0.0
    _CAM_DEFAULT_PITCH_DEG: float = 89.0

    # Hard floor on the camera's height above the copper top (in scaled
    # world units) for 3D wheel / middle-drag zoom. Tight enough to read
    # individual traces but stops the camera before it crashes into the
    # board (Altium-style hard stop).
    _CAM_MIN_COPPER_CLEARANCE: float = 2.0

    def _fit_3d_to_data(self) -> None:
        """Centre the 3D camera target on the data and pick a distance
        that frames the board with a healthy margin."""
        if self._data_bounds is None:
            return
        x_min, x_max, y_min, y_max = self._data_bounds
        cx = (x_min + x_max) * 0.5
        cy = (y_min + y_max) * 0.5
        board_w = max(x_max - x_min, 1.0)
        board_h = max(y_max - y_min, 1.0)
        # Distance ≈ half-diagonal / tan(fov/2), with margin so the
        # board isn't kissing the viewport edges.
        diag = math.hypot(board_w, board_h) * 1.6
        self._cam_target = (cx, cy, 0.0)
        self._cam_distance = max(diag / (2.0 * math.tan(
            math.radians(self._cam_fov_deg) * 0.5)), 1.0)

    def _dolly_cam_distance(self, base_distance: float,
                              factor: float) -> float:
        """New camera distance for a wheel/middle-drag zoom step.

        Naive ``base_distance * factor`` is what the old code did, and it
        feels like the zoom slows down as the camera gets close — because
        the camera target sits at z=0 while the copper is some way above
        it (more so with vertical exaggeration), so the apparent
        magnification asymptotes well before the camera actually reaches
        the surface, and nothing stops the camera from passing through.

        Instead, apply ``factor`` to the camera's *height above the
        copper top* (in scaled world units) so each click is a constant
        magnification step regardless of how close we already are, and
        clamp at :attr:`_CAM_MIN_COPPER_CLEARANCE` for a hard stop.

        Falls back to plain multiplicative dolly when the formula isn't
        well-defined: no mesh uploaded yet, or the view pitched so close
        to horizontal that "height above copper" stops mapping cleanly
        onto camera distance.
        """
        z_top_scaled = self._data_z_max * self._vertical_exaggeration
        pitch_rad = math.radians(self._cam_pitch_deg)
        sin_p = math.sin(pitch_rad)
        if z_top_scaled <= 0.0 or sin_p < 0.2:
            new_dist = base_distance * factor
            return max(min(new_dist, 1e7), 0.01)
        tz = self._cam_target[2]
        cam_z = tz + base_distance * sin_p
        H = cam_z - z_top_scaled
        H_min = self._CAM_MIN_COPPER_CLEARANCE
        new_H = max(H * factor, H_min)
        new_cam_z = z_top_scaled + new_H
        new_cam_dist = (new_cam_z - tz) / sin_p
        return max(min(new_cam_dist, 1e7), 0.01)

    def reset_3d_view(self) -> None:
        """Reset the 3D camera to its default yaw / pitch and re-fit
        the target + distance to frame the data. Equivalent to flipping
        2D → 3D fresh; used by the host's '0' hotkey.

        Animated over ~0.5 s with smoothstep easing rather than
        snapping, so the user can see where the camera was vs where
        it lands. If there's no data yet there's nothing to frame, so
        just snap the angles."""
        if self._data_bounds is None:
            self._cam_yaw_deg = self._CAM_DEFAULT_YAW_DEG
            self._cam_pitch_deg = self._CAM_DEFAULT_PITCH_DEG
            self.viewChanged.emit()
            self.update()
            return
        # Compute the reset target/distance without disturbing the
        # live camera — we'll animate towards them.
        saved_target = self._cam_target
        saved_distance = self._cam_distance
        self._fit_3d_to_data()
        to_target = self._cam_target
        to_distance = self._cam_distance
        self._cam_target = saved_target
        self._cam_distance = saved_distance
        self._animate_3d_view_to(
            yaw_deg=self._CAM_DEFAULT_YAW_DEG,
            pitch_deg=self._CAM_DEFAULT_PITCH_DEG,
            target=to_target,
            distance=to_distance,
            duration_ms=500,
        )

    def _animate_3d_view_to(self, *, yaw_deg: float, pitch_deg: float,
                            target: tuple[float, float, float],
                            distance: float, duration_ms: int) -> None:
        """Start an interpolation of the orbital-camera state to the
        given pose. A new call while one is already running re-snaps
        ``from_*`` to the current (mid-animation) values, so a second
        '0' press partway through still produces a smooth motion."""
        # Shortest angular path for yaw so we don't take the long way
        # around when current yaw is already near the target.
        delta_yaw = yaw_deg - self._cam_yaw_deg
        while delta_yaw > 180.0:
            delta_yaw -= 360.0
        while delta_yaw < -180.0:
            delta_yaw += 360.0
        self._view_anim = {
            "from_yaw": self._cam_yaw_deg,
            "delta_yaw": delta_yaw,
            "from_pitch": self._cam_pitch_deg,
            "to_pitch": pitch_deg,
            "from_target": self._cam_target,
            "to_target": target,
            "from_distance": self._cam_distance,
            "to_distance": distance,
            "duration_ms": max(1, duration_ms),
        }
        if self._view_anim_timer is None:
            self._view_anim_timer = QTimer(self)
            self._view_anim_timer.setInterval(16)  # ~60 Hz
            self._view_anim_timer.timeout.connect(self._tick_view_animation)
        self._view_anim_elapsed = QElapsedTimer()
        self._view_anim_elapsed.start()
        self._view_anim_timer.start()

    def _tick_view_animation(self) -> None:
        anim = self._view_anim
        if anim is None or self._view_anim_elapsed is None:
            if self._view_anim_timer is not None:
                self._view_anim_timer.stop()
            return
        t = min(1.0, self._view_anim_elapsed.elapsed() / anim["duration_ms"])
        # Smoothstep ease-in-out: 3t² − 2t³. No extra dependency,
        # zero slope at both endpoints, looks natural.
        ease = t * t * (3.0 - 2.0 * t)
        self._cam_yaw_deg = anim["from_yaw"] + anim["delta_yaw"] * ease
        self._cam_pitch_deg = (anim["from_pitch"]
                                + (anim["to_pitch"] - anim["from_pitch"]) * ease)
        fx, fy, fz = anim["from_target"]
        tx, ty, tz = anim["to_target"]
        self._cam_target = (
            fx + (tx - fx) * ease,
            fy + (ty - fy) * ease,
            fz + (tz - fz) * ease,
        )
        self._cam_distance = (anim["from_distance"]
                              + (anim["to_distance"] - anim["from_distance"]) * ease)
        self.viewChanged.emit()
        self.update()
        if t >= 1.0:
            self._view_anim_timer.stop()
            self._view_anim = None
            self._view_anim_elapsed = None

    def _camera_position(self) -> tuple[float, float, float]:
        """World-space camera position derived from yaw/pitch/distance
        around :attr:`_cam_target`."""
        tx, ty, tz = self._cam_target
        yaw = math.radians(self._cam_yaw_deg)
        pitch = math.radians(self._cam_pitch_deg)
        # Standard spherical-to-cartesian. Pitch=0 puts the camera on
        # the equator (side view); pitch=90 → directly above (top down).
        cosp = math.cos(pitch)
        x = tx + self._cam_distance * cosp * math.sin(yaw)
        y = ty - self._cam_distance * cosp * math.cos(yaw)
        z = tz + self._cam_distance * math.sin(pitch)
        return x, y, z

    def _current_mvp(self) -> QMatrix4x4:
        """Compute the MVP matrix for the current view mode. In 2D this
        is a straight orthographic projection of the visible rect (with
        a wide z-range so layers at any z still pass the clip test). In
        3D this is perspective * lookAt with the per-vertex z scaled by
        :attr:`_vertical_exaggeration`."""
        mvp = QMatrix4x4()
        if self._view_mode == "3d":
            w_px = max(1, self.width())
            h_px = max(1, self.height())
            aspect = w_px / h_px
            near = max(self._cam_distance * 0.01, 0.1)
            far = max(self._cam_distance * 100.0, near + 1.0)
            mvp.perspective(self._cam_fov_deg, aspect, near, far)
            cam_x, cam_y, cam_z = self._camera_position()
            tx, ty, tz = self._cam_target
            mvp.lookAt(
                QVector3D(cam_x, cam_y, cam_z),
                QVector3D(tx, ty, tz),
                QVector3D(0.0, 0.0, 1.0),
            )
            # Per-vertex z exaggeration goes into the model matrix.
            mvp.scale(1.0, 1.0, self._vertical_exaggeration)
        else:
            x_min, x_max, y_min, y_max = self.view_range()
            # Wide z range so any leftover non-zero z's in the buffer
            # still pass the clip test in 2D mode.
            mvp.ortho(x_min, x_max, y_min, y_max, -1e6, 1e6)
        return mvp

    # ------------------------------------------------------------------
    # Overlay interface
    # ------------------------------------------------------------------

    def set_overlay_top_left(self, html: str) -> None:
        self._overlay_top_left_html = html or ""
        self.update()

    def set_overlay_top_right(self, html: str) -> None:
        self._overlay_top_right_html = html or ""
        # Plain-HTML and structured legend share the same corner — setting
        # one clears the other so we never render both stacked.
        self._overlay_top_right_legend = []
        self._legend_row_rects = []
        self.update()

    def set_overlay_top_right_legend(self,
                                     rows: list[LegendRow]) -> None:
        """Push a structured legend to the top-right chip. Each row gets
        per-row click hit-testing (emits :attr:`legendRowClicked` with
        the row's ``key``) and an off-state slash when ``hidden`` is set.
        Replaces any plain-HTML chip set via :meth:`set_overlay_top_right`.
        """
        self._overlay_top_right_legend = list(rows)
        self._overlay_top_right_html = ""
        self.update()

    def set_legend_right_inset(self, px: float) -> None:
        """Shift the top-right chip (legend or plain HTML) left by ``px``
        pixels. The host calls this when a floating right-edge panel is
        shown so the legend doesn't disappear behind it. Zero restores
        the default flush-to-right placement."""
        inset = max(0.0, float(px))
        if inset == self._legend_right_inset:
            return
        self._legend_right_inset = inset
        self.update()

    def set_markers(self, groups: list[MarkerGroup]) -> None:
        self._markers = list(groups)
        self.update()

    def clear_markers(self) -> None:
        self._markers = []
        self.update()

    def set_editor_mode(self, on: bool) -> None:
        """Toggle the editor-mode look — bluish background + faint world-mm
        grid. Pure display state; does not touch any mesh data."""
        on = bool(on)
        if on == self._editor_mode:
            return
        self._editor_mode = on
        # Leaving editor mode cancels any in-progress free-marker drag.
        if not on:
            self._editor_drag_active = False
        self._apply_editor_cursor("default")
        self.update()

    def set_editor_drag_hit_test(self, hit_test) -> None:
        """Register the host's free-marker hit-test — a callable
        ``(world_x, world_y) -> bool`` that reports whether a draggable
        free marker sits under the point. Pass ``None`` to disable
        editor-mode marker dragging."""
        self._editor_drag_hit_test = hit_test

    def _apply_editor_cursor(self, state: str) -> None:
        """Set the viewport cursor. Used for both editor-mode marker
        dragging (``"open"`` hovering a marker, ``"closed"`` while
        dragging one) and for the top-right legend chip (``"pointing"``
        hovering a clickable row). ``"default"`` resets to the inherited
        cursor. A no-op when unchanged so per-move calls don't churn."""
        if state == self._editor_cursor_state:
            return
        self._editor_cursor_state = state
        if state == "open":
            self.setCursor(Qt.OpenHandCursor)
        elif state == "closed":
            self.setCursor(Qt.ClosedHandCursor)
        elif state == "pointing":
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.unsetCursor()

    def set_editor_selection_bbox(self, bbox) -> None:
        """Set (or clear, with ``None``) the component bounding box drawn
        as the editor-mode yellow selection box. ``bbox`` is
        ``(x0, y0, x1, y1)`` in world mm. A no-op when unchanged so the
        per-render push doesn't trigger a redundant repaint."""
        new = (tuple(float(v) for v in bbox)
               if bbox is not None else None)
        if new == self._editor_selection_bbox:
            return
        self._editor_selection_bbox = new
        self.update()

    def set_primitive_selection_outline(self, rings) -> None:
        """Set (or clear, with ``None``) the dashed-yellow outline drawn
        over a click-selected copper primitive. ``rings`` is a list of
        closed rings, each a list of ``(x_mm, y_mm)`` world-mm tuples.
        No-op when unchanged so a redundant push doesn't repaint."""
        if rings is None:
            new = None
        else:
            new = [[(float(x), float(y)) for x, y in ring] for ring in rings]
        if new == self._primitive_selection_rings:
            return
        self._primitive_selection_rings = new
        self.update()

    def set_measurement_line(self, x0: float, y0: float,
                              x1: float, y1: float) -> None:
        """Show a thin white line from world-mm ``(x0, y0)`` to ``(x1, y1)``.
        Drawn on top of every other overlay (markers / chips). Used by
        the host's Shift-drag voltage-difference tool."""
        self._measurement_line = (float(x0), float(y0),
                                  float(x1), float(y1))
        self.update()

    def clear_measurement_line(self) -> None:
        if self._measurement_line is not None:
            self._measurement_line = None
            self.update()

    # ------------------------------------------------------------------
    # Coord transforms (logical pixels → world mm and back)
    # ------------------------------------------------------------------

    def screen_to_world(self, x_px: float, y_px: float) -> tuple[float, float]:
        """Convert a widget-local pixel position to world mm. ``y_px`` is
        Qt-style top-down; we flip to mm-style bottom-up internally.

        In 2D this is the orthographic-projection inverse. In 3D, where a
        single screen pixel corresponds to a whole world-space ray, the
        return value is the ray's intersection with the z=0 plane — use
        :meth:`screen_to_world_at_z` to pick a different z (e.g. an inner
        layer's stackup height) so the cursor lands on the copper the
        user is actually looking at."""
        if self._view_mode == "3d":
            return self.screen_to_world_at_z(x_px, y_px, 0.0)
        cx, cy = self._view_center_x, self._view_center_y
        mpp = self._mm_per_pixel
        wx = cx + (x_px - self.width() * 0.5) * mpp
        wy = cy - (y_px - self.height() * 0.5) * mpp
        return wx, wy

    def screen_to_world_at_z(self, x_px: float, y_px: float,
                               z_world: float) -> tuple[float, float]:
        """Unproject a widget-local pixel to the world-space (x, y) where
        the camera ray meets the plane z = ``z_world``.

        In 2D mode the z plane is irrelevant — returns the same
        coordinates as :meth:`screen_to_world` would. In 3D mode the
        ray is built from the inverse MVP (so ``z_world`` is in the
        same un-exaggerated mm as the host's stackup z values; the
        vertical-exaggeration scale baked into the MVP cancels out on
        inversion). If the ray is parallel to the requested plane the
        far-plane unprojection is returned as a graceful fallback.
        """
        if self._view_mode != "3d":
            cx, cy = self._view_center_x, self._view_center_y
            mpp = self._mm_per_pixel
            wx = cx + (x_px - self.width() * 0.5) * mpp
            wy = cy - (y_px - self.height() * 0.5) * mpp
            return wx, wy
        w_px = max(1, self.width())
        h_px = max(1, self.height())
        ndc_x = 2.0 * float(x_px) / w_px - 1.0
        ndc_y = 1.0 - 2.0 * float(y_px) / h_px
        mvp = self._current_mvp()
        inv, ok = mvp.inverted()
        if not ok:
            return 0.0, 0.0

        def _unproject(ndc_z: float) -> tuple[float, float, float]:
            # QMatrix4x4 has no QVector4D multiply; do the dot products
            # manually (same pattern as world_to_screen).
            r0 = inv.row(0); r1 = inv.row(1)
            r2 = inv.row(2); r3 = inv.row(3)
            x = (r0.x() * ndc_x + r0.y() * ndc_y
                 + r0.z() * ndc_z + r0.w())
            y = (r1.x() * ndc_x + r1.y() * ndc_y
                 + r1.z() * ndc_z + r1.w())
            z = (r2.x() * ndc_x + r2.y() * ndc_y
                 + r2.z() * ndc_z + r2.w())
            w = (r3.x() * ndc_x + r3.y() * ndc_y
                 + r3.z() * ndc_z + r3.w())
            if abs(w) < 1e-12:
                w = 1e-12
            return x / w, y / w, z / w

        ox, oy, oz = _unproject(-1.0)   # near
        fx, fy, fz = _unproject(1.0)    # far
        dz = fz - oz
        if abs(dz) < 1e-9:
            return fx, fy
        t = (float(z_world) - oz) / dz
        wx = ox + t * (fx - ox)
        wy = oy + t * (fy - oy)
        return wx, wy

    def last_hover_pixel(self) -> tuple[float, float]:
        """Most recent mouse-move pixel position in widget-local logical
        coordinates. Lets the host re-unproject the cursor against an
        arbitrary z (per-layer ray picking) without re-fielding the
        mouse event itself."""
        return self._last_hover_pixel

    def set_last_hover_pixel(self, x_px: float, y_px: float) -> None:
        """Seed the last-hover pixel from outside the move-event path —
        used when the host wants to synthesize a hover without a real
        mouse event (e.g. on toggling the cursor tooltip on)."""
        self._last_hover_pixel = (float(x_px), float(y_px))

    def world_to_screen(self, wx: float, wy: float,
                        wz: float = 0.0) -> tuple[float, float]:
        """Project a world-space point (mm) to widget-pixel coords.

        In 2D mode ``wz`` is ignored. In 3D mode the point is run
        through the current MVP (including the vertical-exaggeration
        scale) and undergoes perspective divide. Points behind the
        camera return a far-off-screen sentinel so caller clipping
        skips them naturally.
        """
        if self._view_mode == "3d":
            mvp = self._current_mvp()
            # PySide6's QMatrix4x4 doesn't expose ``__mul__`` for
            # QVector4D, so do the row-by-row dot product manually to
            # get clip-space coords (we need ``w`` for the
            # behind-camera test, which ``map(QVector3D)`` swallows).
            x = float(wx); y = float(wy); z = float(wz)
            r0 = mvp.row(0); r1 = mvp.row(1); r3 = mvp.row(3)
            cx = r0.x()*x + r0.y()*y + r0.z()*z + r0.w()
            cy = r1.x()*x + r1.y()*y + r1.z()*z + r1.w()
            cw = r3.x()*x + r3.y()*y + r3.z()*z + r3.w()
            if cw <= 1e-6:
                return -1e9, -1e9
            ndc_x = cx / cw
            ndc_y = cy / cw
            x_px = (ndc_x + 1.0) * 0.5 * self.width()
            y_px = (1.0 - ndc_y) * 0.5 * self.height()
            return x_px, y_px
        # 2D orthographic.
        cx, cy = self._view_center_x, self._view_center_y
        mpp = self._mm_per_pixel
        x_px = self.width() * 0.5 + (wx - cx) / mpp
        y_px = self.height() * 0.5 - (wy - cy) / mpp
        return x_px, y_px

    # ------------------------------------------------------------------
    # QOpenGLWidget lifecycle
    # ------------------------------------------------------------------

    def initializeGL(self) -> None:
        # Background colour — the bare-substrate look outside the mesh.
        GL.glClearColor(self._bg_r, self._bg_g, self._bg_b, 1.0)
        # Build & link the shader program.
        prog = QOpenGLShaderProgram()
        prog.addShaderFromSourceCode(QOpenGLShader.Vertex, _VERTEX_SHADER_SRC)
        prog.addShaderFromSourceCode(QOpenGLShader.Fragment,
                                      _FRAGMENT_SHADER_SRC)
        if not prog.link():
            log = prog.log()
            raise RuntimeError(f"GLMeshViewer: shader link failed: {log}")
        self._program = prog
        self._u_mvp_loc = prog.uniformLocation("u_mvp")
        self._u_levels_loc = prog.uniformLocation("u_levels")
        self._u_cmap_loc = prog.uniformLocation("u_cmap")

        # VAO + VBOs. We allocate Qt wrappers (cleanup at destruction)
        # and use raw GL calls through them for the actual data uploads.
        self._vao = QOpenGLVertexArrayObject()
        self._vao.create()
        self._pos_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._pos_vbo.create()
        self._val_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._val_vbo.create()
        self._alpha_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._alpha_vbo.create()
        self._neutral_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._neutral_vbo.create()
        self._ibo = QOpenGLBuffer(QOpenGLBuffer.IndexBuffer)
        self._ibo.create()

        # 1-D colormap texture. We make a placeholder grayscale until
        # set_colormap is called by the host.
        self._cmap_tex = QOpenGLTexture(QOpenGLTexture.Target1D)
        self._cmap_tex.setMinificationFilter(QOpenGLTexture.Linear)
        self._cmap_tex.setMagnificationFilter(QOpenGLTexture.Linear)
        self._cmap_tex.setWrapMode(QOpenGLTexture.ClampToEdge)
        self._cmap_tex.setSize(256)
        self._cmap_tex.setFormat(QOpenGLTexture.RGBA8_UNorm)
        self._cmap_tex.allocateStorage(QOpenGLTexture.RGBA, QOpenGLTexture.UInt8)
        placeholder = np.tile(np.linspace(0, 255, 256, dtype=np.uint8)[:, None],
                              (1, 4))
        placeholder[:, 3] = 255
        self._cmap_tex.setData(0, QOpenGLTexture.RGBA, QOpenGLTexture.UInt8,
                                placeholder.tobytes())

        # Line / outline shader program — vec2 position + vec3 colour,
        # MVP-transformed, no texture lookup. Used for per-layer copper
        # outlines drawn on top of the heatmap mesh.
        line_prog = QOpenGLShaderProgram()
        line_prog.addShaderFromSourceCode(QOpenGLShader.Vertex,
                                            _LINE_VERTEX_SHADER_SRC)
        line_prog.addShaderFromSourceCode(QOpenGLShader.Fragment,
                                            _LINE_FRAGMENT_SHADER_SRC)
        if not line_prog.link():
            log = line_prog.log()
            raise RuntimeError(f"GLMeshViewer: line shader link failed: {log}")
        self._line_program = line_prog
        self._line_u_mvp_loc = line_prog.uniformLocation("u_mvp")
        self._line_vao = QOpenGLVertexArrayObject()
        self._line_vao.create()
        self._line_pos_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._line_pos_vbo.create()
        self._line_col_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._line_col_vbo.create()

        # Overlay shader — same MVP-only transform as the line shader, but
        # the per-vertex colour carries alpha so Board Features rows can
        # fade out without disturbing any other batch.
        overlay_prog = QOpenGLShaderProgram()
        overlay_prog.addShaderFromSourceCode(QOpenGLShader.Vertex,
                                             _OVERLAY_VERTEX_SHADER_SRC)
        overlay_prog.addShaderFromSourceCode(QOpenGLShader.Fragment,
                                             _OVERLAY_FRAGMENT_SHADER_SRC)
        if not overlay_prog.link():
            log = overlay_prog.log()
            raise RuntimeError(
                f"GLMeshViewer: overlay shader link failed: {log}")
        self._overlay_program = overlay_prog
        self._overlay_u_mvp_loc = overlay_prog.uniformLocation("u_mvp")

        # Thick-line program for the outline batch — a geometry shader
        # widens each GL_LINES segment into a constant-pixel-width quad.
        # Separate from the flat line program because a `layout(lines)`
        # geometry shader only accepts line primitives, while the line
        # program is also used for triangle batches (board outline, etc.).
        thick_prog = QOpenGLShaderProgram()
        thick_prog.addShaderFromSourceCode(QOpenGLShader.Vertex,
                                           _THICK_LINE_VERTEX_SHADER_SRC)
        thick_prog.addShaderFromSourceCode(QOpenGLShader.Geometry,
                                           _THICK_LINE_GEOMETRY_SHADER_SRC)
        thick_prog.addShaderFromSourceCode(QOpenGLShader.Fragment,
                                           _THICK_LINE_FRAGMENT_SHADER_SRC)
        if not thick_prog.link():
            log = thick_prog.log()
            raise RuntimeError(
                f"GLMeshViewer: thick-line shader link failed: {log}")
        self._thick_line_program = thick_prog
        self._thick_u_mvp_loc = thick_prog.uniformLocation("u_mvp")
        self._thick_u_viewport_loc = thick_prog.uniformLocation("u_viewport")
        self._thick_u_half_px_loc = thick_prog.uniformLocation("u_half_px")

        # Via cylinder VAO/VBOs — share the line shader.
        self._cyl_vao = QOpenGLVertexArrayObject()
        self._cyl_vao.create()
        self._cyl_pos_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._cyl_pos_vbo.create()
        self._cyl_col_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._cyl_col_vbo.create()

        # Current-arrow VAO/VBOs — share the line shader.
        self._arrow_vao = QOpenGLVertexArrayObject()
        self._arrow_vao.create()
        self._arrow_pos_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._arrow_pos_vbo.create()
        self._arrow_col_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._arrow_col_vbo.create()

        # Stub-copper VAO/VBOs — share the line shader.
        self._stub_vao = QOpenGLVertexArrayObject()
        self._stub_vao.create()
        self._stub_pos_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._stub_pos_vbo.create()
        self._stub_col_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._stub_col_vbo.create()

        # Series-bar VAO/VBOs — share the line shader.
        self._sbar_vao = QOpenGLVertexArrayObject()
        self._sbar_vao.create()
        self._sbar_pos_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._sbar_pos_vbo.create()
        self._sbar_col_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._sbar_col_vbo.create()

        # Board-outline VAO/VBOs — share the line shader.
        self._bdrl_vao = QOpenGLVertexArrayObject()
        self._bdrl_vao.create()
        self._bdrl_pos_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._bdrl_pos_vbo.create()
        self._bdrl_col_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._bdrl_col_vbo.create()

        # Overlay-fill VAO/VBOs — share the line shader.
        self._ovl_vao = QOpenGLVertexArrayObject()
        self._ovl_vao.create()
        self._ovl_pos_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._ovl_pos_vbo.create()
        self._ovl_col_vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._ovl_col_vbo.create()

        # Supersampling (SSAA) downsample shader — a textured fullscreen
        # triangle that resolves the oversized offscreen buffer into the
        # widget. An empty VAO satisfies the core-profile rule that a VAO
        # be bound for any draw; the triangle's three vertices come from
        # gl_VertexID, so no VBO is attached.
        ss_prog = QOpenGLShaderProgram()
        ss_prog.addShaderFromSourceCode(QOpenGLShader.Vertex,
                                        _SS_BLIT_VERTEX_SHADER_SRC)
        ss_prog.addShaderFromSourceCode(QOpenGLShader.Fragment,
                                        _SS_BLIT_FRAGMENT_SHADER_SRC)
        if not ss_prog.link():
            log = ss_prog.log()
            raise RuntimeError(
                f"GLMeshViewer: SSAA blit shader link failed: {log}")
        self._ss_blit_program = ss_prog
        self._ss_blit_u_tex_loc = ss_prog.uniformLocation("u_tex")
        self._ss_blit_vao = QOpenGLVertexArrayObject()
        self._ss_blit_vao.create()

        self._gl_initialized = True

    def resizeGL(self, w: int, h: int) -> None:
        # Use device pixels for the GL viewport on Hi-DPI displays.
        dpr = self.devicePixelRatio()
        GL.glViewport(0, 0, int(w * dpr), int(h * dpr))
        # Resize doesn't change centre/scale — that's the CAD-style fixed
        # zoom; the visible area grows or shrinks instead. Host can call
        # fit_to_data if it wants a re-fit.
        self.viewChanged.emit()

    def paintGL(self) -> None:
        # Reset the GL state the previous frame's QPainter overlay pass may
        # have left behind. QPainter on a QOpenGLWidget renders through the
        # GL paint engine, which — on some drivers — leaves GL_SCISSOR_TEST
        # enabled (clipped to the last thing it drew) and the colour/depth
        # write masks altered when it ends. That state is global to the
        # context and survives into this paintGL, so the next glClear and
        # every draw get clipped/masked to a stale rect, leaving the frame
        # partially updated ("transformed/illegible") until enough repaints
        # (e.g. dragging the mouse) happen to scrub in every region. Drivers
        # that scrub this state on QPainter.end() never show it — hence it
        # only reproduces on some machines. Reset before we touch the
        # framebuffer so glClear and the depth-sorted passes start clean.
        # See https://github.com/anarthrous-eda/FYPA/issues/1
        GL.glDisable(GL.GL_SCISSOR_TEST)
        GL.glDisable(GL.GL_STENCIL_TEST)
        GL.glColorMask(GL.GL_TRUE, GL.GL_TRUE, GL.GL_TRUE, GL.GL_TRUE)
        GL.glDepthMask(GL.GL_TRUE)
        # Render straight to the widget's framebuffer, unless supersampling
        # is enabled AND its offscreen buffers are available this frame —
        # then render oversized and downsample. The QPainter overlay pass
        # always runs last, at native resolution, so text / markers / chips
        # stay crisp regardless of the supersample factor.
        if (self._supersample
                and self._ss_blit_program is not None
                and self._ensure_ss_fbos()):
            self._render_scene_supersampled()
        else:
            if self._ss_resolve_fbo is not None:
                self._release_ss_fbos()
            self._render_scene()
        # Overlays (markers + text chips) drawn via QPainter — works on
        # the QOpenGLWidget because QPainter uses the GL paint engine.
        # MUST be called outside the active program's bind to avoid
        # state corruption from the painter.
        self._draw_overlays()

    def _render_scene_supersampled(self) -> None:
        """Render the scene oversized into the MSAA supersample FBO, then
        resolve and box-downsample it into the widget's framebuffer.

        Assumes :meth:`_ensure_ss_fbos` has just returned ``True``."""
        sw, sh = self._ss_fbo_size
        default_fbo = self.defaultFramebufferObject()
        # 1. Draw the scene into the oversized multisample FBO.
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._ss_ms_fbo.handle())
        GL.glViewport(0, 0, sw, sh)
        self._render_scene()
        # 2. Resolve MSAA into a same-size single-sample texture FBO. A
        #    multisample buffer can't be blitted with scaling, so the
        #    resolve and the downscale have to be two separate steps.
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, self._ss_ms_fbo.handle())
        GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER,
                             self._ss_resolve_fbo.handle())
        GL.glBlitFramebuffer(0, 0, sw, sh, 0, 0, sw, sh,
                             GL.GL_COLOR_BUFFER_BIT, GL.GL_NEAREST)
        # 3. Downsample the resolved buffer into the widget framebuffer
        #    with a textured fullscreen triangle. The resolve texture is
        #    LINEAR-filtered, so sampling it at the (smaller) widget
        #    resolution averages each 2x2 oversize block. A blit can't be
        #    used here — scaling into the widget's own multisample FBO is
        #    a GL error — but drawing a quad into it is ordinary rendering.
        dpr = self.devicePixelRatio()
        w = max(1, int(self.width() * dpr))
        h = max(1, int(self.height() * dpr))
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, default_fbo)
        GL.glViewport(0, 0, w, h)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glDisable(GL.GL_BLEND)
        self._ss_blit_program.bind()
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._ss_resolve_fbo.texture())
        self._ss_blit_program.setUniformValue(self._ss_blit_u_tex_loc, 0)
        self._ss_blit_vao.bind()
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        self._ss_blit_vao.release()
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        self._ss_blit_program.release()

    def _ensure_ss_fbos(self) -> bool:
        """Create or resize the offscreen supersample FBOs to match the
        current widget size. Returns ``True`` when a valid pair is ready
        to render into; ``False`` if supersampling can't run this frame
        (e.g. the oversized buffer would exceed the driver's renderbuffer
        limit, or allocation failed) so the caller falls back to a plain
        native-resolution paint.

        Called only from :meth:`paintGL`, so the GL context is current."""
        dpr = self.devicePixelRatio()
        w = max(1, int(self.width() * dpr))
        h = max(1, int(self.height() * dpr))
        sw, sh = w * self._SS_FACTOR, h * self._SS_FACTOR
        try:
            max_dim = int(GL.glGetIntegerv(GL.GL_MAX_RENDERBUFFER_SIZE))
        except Exception:
            max_dim = 0
        if max_dim and (sw > max_dim or sh > max_dim):
            return False
        if (self._ss_ms_fbo is not None
                and self._ss_resolve_fbo is not None
                and self._ss_fbo_size == (sw, sh)):
            return True
        self._release_ss_fbos()
        ms_fmt = QOpenGLFramebufferObjectFormat()
        ms_fmt.setSamples(self._SS_SAMPLES)
        ms_fmt.setAttachment(QOpenGLFramebufferObject.Attachment.Depth)
        ms_fbo = QOpenGLFramebufferObject(sw, sh, ms_fmt)
        resolve_fbo = QOpenGLFramebufferObject(
            sw, sh, QOpenGLFramebufferObjectFormat())
        if not (ms_fbo.isValid() and resolve_fbo.isValid()):
            return False
        # The downsample samples the resolve texture with LINEAR filtering
        # to box-average each 2x2 block; clamp so edge texels don't wrap.
        GL.glBindTexture(GL.GL_TEXTURE_2D, resolve_fbo.texture())
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER,
                           GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER,
                           GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S,
                           GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T,
                           GL.GL_CLAMP_TO_EDGE)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        self._ss_ms_fbo = ms_fbo
        self._ss_resolve_fbo = resolve_fbo
        self._ss_fbo_size = (sw, sh)
        return True

    def _release_ss_fbos(self) -> None:
        """Drop the offscreen supersample FBOs, freeing their GPU memory.
        Runs from the paint path only — releasing the last Python
        reference fires the QOpenGLFramebufferObject destructor, which
        needs a current GL context to delete its renderbuffers."""
        self._ss_ms_fbo = None
        self._ss_resolve_fbo = None
        self._ss_fbo_size = (0, 0)

    def _render_scene(self) -> None:
        """Draw the full GL scene — copper mesh, stubs, overlays, outlines,
        arrows — into whichever framebuffer is currently bound. Split out
        of :meth:`paintGL` so it can target either the widget directly or
        the oversized supersample buffer."""
        # Clear colour follows the editor/viewer mode — set per-frame here
        # (rather than once in initializeGL) so set_editor_mode takes effect
        # without needing the GL context made current off the paint path.
        bg = self._bg_editor if self._editor_mode else self._bg_normal
        GL.glClearColor(bg[0], bg[1], bg[2], 1.0)
        # In 3D every pass uses depth testing for correct front/back
        # ordering. In 2D depth testing is selectively enabled on the
        # layered fills (the under-mesh overlay batch — which carries the
        # all-copper geometry — plus the rail mesh itself plus the outline
        # pass) so they sort by their per-vertex layer-z: rail mesh sits
        # above same-layer all-copper, top layers sit above bottom layers
        # both for fills and for outlines. The other passes (stubs,
        # series bars, over-mesh overlays, board outline) keep the
        # plain painter's-order behaviour. Always clear depth in 2D so
        # the seeded buffer starts fresh each frame.
        in_2d = self._view_mode != "3d"
        if not in_2d:
            GL.glEnable(GL.GL_DEPTH_TEST)
        else:
            GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        self._flush_pending_uploads()
        # Stubs first — flat-grey copper polygons drawn underneath the
        # heatmap mesh. If a stub happens to overlap a solved layer
        # piece (unlikely after the filter, but possible), the heatmap
        # paints on top.
        if (self._n_stub_vertices > 0
                and self._line_program is not None):
            self._draw_stubs()
        # Bottom-side series bars sit before the mesh so the bottom
        # copper draws over them (the resistor body is physically below
        # the bottom copper). Top-side bars are drawn after the mesh
        # below, so they sit on top.
        if (self._n_sbar_under_vertices > 0
                and self._line_program is not None):
            self._draw_series_bars(0, self._n_sbar_under_vertices)
        # Under-mesh overlay batch (2D) — bottom-side board features plus
        # every layer's all-copper geometry. Enable depth test + write
        # with GL_LEQUAL so the layers sort by their per-vertex z (top
        # all-copper paints above bottom all-copper above bottom-side
        # silkscreen / designators, all of which can then be partially or
        # fully covered by the rail mesh below).
        if (self._n_ovl_under_vertices > 0
                and self._line_program is not None):
            if in_2d:
                GL.glEnable(GL.GL_DEPTH_TEST)
                GL.glDepthFunc(GL.GL_LEQUAL)
            self._draw_overlay_fills(0, self._n_ovl_under_vertices)
            if in_2d:
                GL.glDisable(GL.GL_DEPTH_TEST)
                GL.glDepthFunc(GL.GL_LESS)
        if self._n_indices > 0 and self._program is not None:
            # Rail mesh in 2D draws with GL_LEQUAL + write so a top-layer
            # rail triangle covers the (lower-z) under-mesh all-copper at
            # its pixel while a bottom-layer rail triangle is rejected
            # where top-layer all-copper already wrote a closer z. Equal
            # z (rail and same-layer all-copper) is what makes the rail
            # paint over its own layer's all-copper.
            if in_2d:
                GL.glEnable(GL.GL_DEPTH_TEST)
                GL.glDepthFunc(GL.GL_LEQUAL)
            self._draw_mesh()
            if in_2d:
                GL.glDisable(GL.GL_DEPTH_TEST)
                GL.glDepthFunc(GL.GL_LESS)
            if (self._show_mesh_edges
                    and self._line_program is not None):
                self._draw_mesh_wireframe()
        over_first = self._n_sbar_under_vertices
        over_count = self._n_sbar_vertices - over_first
        if (over_count > 0
                and self._line_program is not None):
            self._draw_series_bars(over_first, over_count)
        # Overlay solid fills before the line batch so wire-mesh overlay
        # outlines (and the pad / layer outlines) sit crisply on top.
        ovl_over_first = self._n_ovl_under_vertices
        ovl_over_count = self._n_ovl_vertices - ovl_over_first
        if (ovl_over_count > 0
                and self._line_program is not None):
            self._draw_overlay_fills(ovl_over_first, ovl_over_count)
        if (self._n_line_vertices > 0
                and self._line_program is not None):
            # In 2D, test AND write depth so the outlines sort correctly
            # both against the mesh (a bottom-layer outline is hidden
            # where a higher layer's copper covers it) and against each
            # other (where two layers' thick-line quads overlap near a
            # shared edge, the higher-layer outline wins regardless of
            # submission order). GL_LEQUAL keeps adjacent same-layer
            # quads from self-occluding at joins.
            if in_2d:
                GL.glEnable(GL.GL_DEPTH_TEST)
                GL.glDepthFunc(GL.GL_LEQUAL)
            self._draw_lines()
            if in_2d:
                GL.glDisable(GL.GL_DEPTH_TEST)
                GL.glDepthFunc(GL.GL_LESS)
        if (self._view_mode == "3d"
                and self._n_cyl_vertices > 0
                and self._line_program is not None):
            self._draw_cylinders()
        if (self._n_arrow_vertices > 0
                and self._line_program is not None):
            self._draw_arrows()
        # Board outline last so it always paints on top of the heatmap,
        # outlines, stubs, and arrows — the user uses this overlay to see
        # the PCB boundary against all the other visuals.
        if (self._n_bdrl_vertices > 0
                and self._line_program is not None):
            self._draw_board_outline()

    # ------------------------------------------------------------------
    # GL upload / draw helpers
    # ------------------------------------------------------------------

    def _flush_pending_uploads(self) -> None:
        """If new mesh / values / cmap arrived since the last paint,
        push them to the GPU now (inside the active GL context)."""
        if self._pending_positions is not None:
            self._pos_vbo.bind()
            self._pos_vbo.allocate(self._pending_positions.tobytes(),
                                   self._pending_positions.nbytes)
            self._pos_vbo.release()
            self._pending_positions = None
        if self._pending_indices is not None:
            self._ibo.bind()
            self._ibo.allocate(self._pending_indices.tobytes(),
                               self._pending_indices.nbytes)
            self._ibo.release()
            self._pending_indices = None
        if self._pending_values is not None:
            self._val_vbo.bind()
            self._val_vbo.allocate(self._pending_values.tobytes(),
                                   self._pending_values.nbytes)
            self._val_vbo.release()
            self._pending_values = None
        if self._pending_alpha is not None:
            self._alpha_vbo.bind()
            self._alpha_vbo.allocate(self._pending_alpha.tobytes(),
                                     self._pending_alpha.nbytes)
            self._alpha_vbo.release()
            self._pending_alpha = None
        if self._pending_neutral is not None:
            self._neutral_vbo.bind()
            self._neutral_vbo.allocate(self._pending_neutral.tobytes(),
                                       self._pending_neutral.nbytes)
            self._neutral_vbo.release()
            self._pending_neutral = None
        if self._pending_cmap is not None:
            self._cmap_tex.bind()
            self._cmap_tex.setData(0, QOpenGLTexture.RGBA,
                                    QOpenGLTexture.UInt8,
                                    self._pending_cmap.tobytes())
            self._pending_cmap = None
        if self._pending_line_positions is not None:
            self._line_pos_vbo.bind()
            self._line_pos_vbo.allocate(
                self._pending_line_positions.tobytes(),
                self._pending_line_positions.nbytes,
            )
            self._line_pos_vbo.release()
            self._pending_line_positions = None
        if self._pending_line_colors is not None:
            self._line_col_vbo.bind()
            self._line_col_vbo.allocate(
                self._pending_line_colors.tobytes(),
                self._pending_line_colors.nbytes,
            )
            self._line_col_vbo.release()
            self._pending_line_colors = None
        if self._pending_cyl_positions is not None:
            self._cyl_pos_vbo.bind()
            self._cyl_pos_vbo.allocate(
                self._pending_cyl_positions.tobytes(),
                self._pending_cyl_positions.nbytes,
            )
            self._cyl_pos_vbo.release()
            self._pending_cyl_positions = None
        if self._pending_cyl_colors is not None:
            self._cyl_col_vbo.bind()
            self._cyl_col_vbo.allocate(
                self._pending_cyl_colors.tobytes(),
                self._pending_cyl_colors.nbytes,
            )
            self._cyl_col_vbo.release()
            self._pending_cyl_colors = None
        if self._pending_arrow_positions is not None:
            self._arrow_pos_vbo.bind()
            self._arrow_pos_vbo.allocate(
                self._pending_arrow_positions.tobytes(),
                self._pending_arrow_positions.nbytes,
            )
            self._arrow_pos_vbo.release()
            self._pending_arrow_positions = None
        if self._pending_arrow_colors is not None:
            self._arrow_col_vbo.bind()
            self._arrow_col_vbo.allocate(
                self._pending_arrow_colors.tobytes(),
                self._pending_arrow_colors.nbytes,
            )
            self._arrow_col_vbo.release()
            self._pending_arrow_colors = None
        if self._pending_stub_positions is not None:
            self._stub_pos_vbo.bind()
            self._stub_pos_vbo.allocate(
                self._pending_stub_positions.tobytes(),
                self._pending_stub_positions.nbytes,
            )
            self._stub_pos_vbo.release()
            self._pending_stub_positions = None
        if self._pending_stub_colors is not None:
            self._stub_col_vbo.bind()
            self._stub_col_vbo.allocate(
                self._pending_stub_colors.tobytes(),
                self._pending_stub_colors.nbytes,
            )
            self._stub_col_vbo.release()
            self._pending_stub_colors = None
        if self._pending_sbar_positions is not None:
            self._sbar_pos_vbo.bind()
            self._sbar_pos_vbo.allocate(
                self._pending_sbar_positions.tobytes(),
                self._pending_sbar_positions.nbytes,
            )
            self._sbar_pos_vbo.release()
            self._pending_sbar_positions = None
        if self._pending_sbar_colors is not None:
            self._sbar_col_vbo.bind()
            self._sbar_col_vbo.allocate(
                self._pending_sbar_colors.tobytes(),
                self._pending_sbar_colors.nbytes,
            )
            self._sbar_col_vbo.release()
            self._pending_sbar_colors = None
        if self._pending_bdrl_positions is not None:
            self._bdrl_pos_vbo.bind()
            self._bdrl_pos_vbo.allocate(
                self._pending_bdrl_positions.tobytes(),
                self._pending_bdrl_positions.nbytes,
            )
            self._bdrl_pos_vbo.release()
            self._pending_bdrl_positions = None
        if self._pending_bdrl_colors is not None:
            self._bdrl_col_vbo.bind()
            self._bdrl_col_vbo.allocate(
                self._pending_bdrl_colors.tobytes(),
                self._pending_bdrl_colors.nbytes,
            )
            self._bdrl_col_vbo.release()
            self._pending_bdrl_colors = None
        if self._pending_ovl_positions is not None:
            self._ovl_pos_vbo.bind()
            self._ovl_pos_vbo.allocate(
                self._pending_ovl_positions.tobytes(),
                self._pending_ovl_positions.nbytes,
            )
            self._ovl_pos_vbo.release()
            self._pending_ovl_positions = None
        if self._pending_ovl_colors is not None:
            self._ovl_col_vbo.bind()
            self._ovl_col_vbo.allocate(
                self._pending_ovl_colors.tobytes(),
                self._pending_ovl_colors.nbytes,
            )
            self._ovl_col_vbo.release()
            self._pending_ovl_colors = None

    def _draw_mesh(self) -> None:
        prog = self._program
        prog.bind()
        prog.setUniformValue(self._u_mvp_loc, self._current_mvp())
        prog.setUniformValue(self._u_levels_loc,
                              float(self._levels[0]),
                              float(self._levels[1]))
        # Colormap on texture unit 0.
        GL.glActiveTexture(GL.GL_TEXTURE0)
        self._cmap_tex.bind()
        prog.setUniformValue(self._u_cmap_loc, 0)

        # Bind VAO and set up vertex attributes.
        self._vao.bind()
        self._pos_vbo.bind()
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._pos_vbo.release()
        self._val_vbo.bind()
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 1, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._val_vbo.release()
        # Per-vertex alpha (attribute 2). When the host has uploaded an
        # alpha array matching the vertex count, bind it + enable blending
        # so dimmed copper shows the background through; otherwise feed a
        # constant 1.0 and the mesh draws fully opaque exactly as before.
        use_alpha = (self._n_alpha == self._n_vertices and self._n_alpha > 0)
        if use_alpha:
            self._alpha_vbo.bind()
            GL.glEnableVertexAttribArray(2)
            GL.glVertexAttribPointer(2, 1, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
            self._alpha_vbo.release()
            GL.glEnable(GL.GL_BLEND)
            GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        else:
            GL.glDisableVertexAttribArray(2)
            GL.glVertexAttrib1f(2, 1.0)
        # Per-vertex neutral mask (attribute 3). When present, vertices
        # flagged 1.0 are greyed in the fragment shader; otherwise feed a
        # constant 0.0 so the colormap is used unchanged.
        use_neutral = (self._n_neutral == self._n_vertices
                       and self._n_neutral > 0)
        if use_neutral:
            self._neutral_vbo.bind()
            GL.glEnableVertexAttribArray(3)
            GL.glVertexAttribPointer(3, 1, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
            self._neutral_vbo.release()
        else:
            GL.glDisableVertexAttribArray(3)
            GL.glVertexAttrib1f(3, 0.0)
        self._ibo.bind()
        GL.glDrawElements(GL.GL_TRIANGLES, self._n_indices,
                          GL.GL_UNSIGNED_INT, None)
        self._ibo.release()
        GL.glDisableVertexAttribArray(0)
        GL.glDisableVertexAttribArray(1)
        if use_neutral:
            GL.glDisableVertexAttribArray(3)
        if use_alpha:
            GL.glDisableVertexAttribArray(2)
            GL.glDisable(GL.GL_BLEND)
        self._vao.release()
        self._cmap_tex.release()
        prog.release()

    def _draw_mesh_wireframe(self) -> None:
        """Redraw the heatmap mesh's triangles as line segments to overlay
        the FEM mesh edges on top of the filled heatmap.

        Reuses the line shader (vec3 pos + vec3 colour) with attribute 1
        disabled so every vertex receives the same constant colour set
        via ``glVertexAttrib3f``. ``glPolygonMode(GL_FRONT_AND_BACK,
        GL_LINE)`` plus a GL_TRIANGLES draw is the standard wireframe
        trick — each triangle rasterises as its three edges, sharing the
        edge between adjacent triangles for free.
        """
        prog = self._line_program
        prog.bind()
        prog.setUniformValue(self._line_u_mvp_loc, self._current_mvp())
        self._vao.bind()
        self._pos_vbo.bind()
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._pos_vbo.release()
        # Constant colour for every wireframe vertex — disable the array
        # for attribute 1 and supply a generic value. Dark grey reads on
        # top of viridis without overpowering it.
        GL.glDisableVertexAttribArray(1)
        GL.glVertexAttrib3f(1, 0.08, 0.08, 0.08)
        GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE)
        GL.glLineWidth(1.0)
        self._ibo.bind()
        GL.glDrawElements(GL.GL_TRIANGLES, self._n_indices,
                          GL.GL_UNSIGNED_INT, None)
        self._ibo.release()
        GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)
        GL.glDisableVertexAttribArray(0)
        self._vao.release()
        prog.release()

    def _draw_lines(self) -> None:
        """Draw the layer / pad / stub outline batch as GL_LINES.

        The thick-line program's geometry shader widens each segment into
        a screen-space quad ``_OUTLINE_WIDTH_PX`` device px wide — a width
        that stays constant in pixels at every zoom. glLineWidth past 1.0
        raises GL_INVALID_VALUE on Core profile drivers, so the width has
        to be built as geometry rather than set as line state."""
        prog = self._thick_line_program
        if prog is None:
            return
        prog.bind()
        prog.setUniformValue(self._thick_u_mvp_loc, self._current_mvp())
        # u_half_px MUST be set with raw glUniform*: PySide6 mis-resolves
        # the lone-scalar QOpenGLShaderProgram.setUniformValue overload to
        # the int glUniform path, which is GL_INVALID_OPERATION on a float
        # uniform (the vec2 and mat4 overloads resolve fine). u_viewport
        # uses a raw call too, just to keep the two together. The program
        # is bound, so the raw glUniform calls target it.
        #
        # u_viewport is the final on-screen size — NDC is resolution
        # independent, so the offset yields _OUTLINE_WIDTH_PX final pixels
        # whether the scene renders direct or into the supersample buffer.
        dpr = self.devicePixelRatio()
        GL.glUniform2f(self._thick_u_viewport_loc,
                       float(max(1.0, self.width() * dpr)),
                       float(max(1.0, self.height() * dpr)))
        GL.glUniform1f(self._thick_u_half_px_loc,
                       float(_OUTLINE_WIDTH_PX) * 0.5)

        self._line_vao.bind()
        self._line_pos_vbo.bind()
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._line_pos_vbo.release()
        self._line_col_vbo.bind()
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._line_col_vbo.release()

        GL.glDrawArrays(GL.GL_LINES, 0, self._n_line_vertices)

        GL.glDisableVertexAttribArray(0)
        GL.glDisableVertexAttribArray(1)
        self._line_vao.release()
        prog.release()

    def _draw_cylinders(self) -> None:
        """Draw the via-cylinder triangle batch via the line shader."""
        prog = self._line_program
        prog.bind()
        prog.setUniformValue(self._line_u_mvp_loc, self._current_mvp())

        self._cyl_vao.bind()
        self._cyl_pos_vbo.bind()
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._cyl_pos_vbo.release()
        self._cyl_col_vbo.bind()
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._cyl_col_vbo.release()

        GL.glDrawArrays(GL.GL_TRIANGLES, 0, self._n_cyl_vertices)

        GL.glDisableVertexAttribArray(0)
        GL.glDisableVertexAttribArray(1)
        self._cyl_vao.release()
        prog.release()

    def _draw_stubs(self) -> None:
        """Draw the stub-copper triangle batch via the line shader.

        Flat-coloured polygons of copper the FEM excluded as dead-end
        stubs. Drawn in 2D and 3D so the user always sees the copper
        is there, even though there's no heatmap value for it.
        """
        prog = self._line_program
        prog.bind()
        prog.setUniformValue(self._line_u_mvp_loc, self._current_mvp())

        self._stub_vao.bind()
        self._stub_pos_vbo.bind()
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._stub_pos_vbo.release()
        self._stub_col_vbo.bind()
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._stub_col_vbo.release()

        GL.glDrawArrays(GL.GL_TRIANGLES, 0, self._n_stub_vertices)

        GL.glDisableVertexAttribArray(0)
        GL.glDisableVertexAttribArray(1)
        self._stub_vao.release()
        prog.release()

    def _draw_overlay_fills(self, first: int, count: int) -> None:
        """Draw a slice of the solid-fill overlay triangle batch via the
        dedicated RGBA overlay shader. ``first`` and ``count`` are vertex
        indices; both must be multiples of 3 so each draw covers whole
        triangles.

        Flat-coloured polygons for the Overlays control. The leading
        ``_n_ovl_under_vertices`` (bottom-side board features in 2D) are
        drawn before the heatmap mesh; the rest after, on top. Alpha
        blending is enabled around the draw so the per-row Transparency
        control fades the geometry without affecting any other batch."""
        prog = self._overlay_program
        prog.bind()
        prog.setUniformValue(self._overlay_u_mvp_loc, self._current_mvp())

        self._ovl_vao.bind()
        self._ovl_pos_vbo.bind()
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._ovl_pos_vbo.release()
        self._ovl_col_vbo.bind()
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 4, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._ovl_col_vbo.release()

        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glDrawArrays(GL.GL_TRIANGLES, first, count)
        GL.glDisable(GL.GL_BLEND)

        GL.glDisableVertexAttribArray(0)
        GL.glDisableVertexAttribArray(1)
        self._ovl_vao.release()
        prog.release()

    def _draw_series_bars(self, first: int, count: int) -> None:
        """Draw a slice of the series-bar triangle batch via the line
        shader. ``first`` and ``count`` are vertex indices; both must be
        multiples of 3 so each draw covers whole triangles."""
        prog = self._line_program
        prog.bind()
        prog.setUniformValue(self._line_u_mvp_loc, self._current_mvp())

        self._sbar_vao.bind()
        self._sbar_pos_vbo.bind()
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._sbar_pos_vbo.release()
        self._sbar_col_vbo.bind()
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._sbar_col_vbo.release()

        GL.glDrawArrays(GL.GL_TRIANGLES, first, count)

        GL.glDisableVertexAttribArray(0)
        GL.glDisableVertexAttribArray(1)
        self._sbar_vao.release()
        prog.release()

    def _draw_board_outline(self) -> None:
        """Draw the board-outline ribbon as flat-coloured triangles via the
        RGBA overlay shader. Pre-triangulated by the caller as a fixed-mm-
        wide ribbon so the line thickness reads boldly on any driver. Alpha
        blending is enabled around the draw so the board-outline row's
        Transparency control fades the ribbon without affecting any other
        batch."""
        prog = self._overlay_program
        prog.bind()
        prog.setUniformValue(self._overlay_u_mvp_loc, self._current_mvp())

        self._bdrl_vao.bind()
        self._bdrl_pos_vbo.bind()
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._bdrl_pos_vbo.release()
        self._bdrl_col_vbo.bind()
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 4, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._bdrl_col_vbo.release()

        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, self._n_bdrl_vertices)
        GL.glDisable(GL.GL_BLEND)

        GL.glDisableVertexAttribArray(0)
        GL.glDisableVertexAttribArray(1)
        self._bdrl_vao.release()
        prog.release()

    def _draw_arrows(self) -> None:
        """Draw the current-arrow batch as GL_LINES via the line shader."""
        prog = self._line_program
        prog.bind()
        prog.setUniformValue(self._line_u_mvp_loc, self._current_mvp())

        self._arrow_vao.bind()
        self._arrow_pos_vbo.bind()
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._arrow_pos_vbo.release()
        self._arrow_col_vbo.bind()
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        self._arrow_col_vbo.release()

        GL.glLineWidth(1.0)
        GL.glDrawArrays(GL.GL_LINES, 0, self._n_arrow_vertices)

        GL.glDisableVertexAttribArray(0)
        GL.glDisableVertexAttribArray(1)
        self._arrow_vao.release()
        prog.release()

    # ------------------------------------------------------------------
    # QPainter overlay (markers + title + legend)
    # ------------------------------------------------------------------

    def _draw_overlays(self) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self._draw_editor_grid(painter)
        self._draw_editor_selection(painter)
        self._draw_primitive_selection(painter)
        self._draw_overlay_labels(painter, on_top=False)
        self._draw_markers(painter)
        # Second label pass: ``on_top`` labels (e.g. via-span text) sit
        # above the marker dots so they stay legible inside the via.
        self._draw_overlay_labels(painter, on_top=True)
        self._draw_measurement_line(painter)
        self._draw_overlay_chip(
            painter, self._overlay_top_left_html, anchor_right=False
        )
        # Top-right corner: structured legend takes precedence over the
        # plain-HTML chip so per-row click handling stays consistent.
        # Reset the hit-test list each paint — only the chip drawn this
        # frame contributes rects.
        self._legend_row_rects = []
        if self._overlay_top_right_legend:
            self._draw_legend_chip(painter)
        else:
            self._draw_overlay_chip(
                painter, self._overlay_top_right_html, anchor_right=True
            )
        painter.end()

    def _draw_editor_grid(self, painter: QPainter) -> None:
        """Faint world-mm grid for editor mode. 2D only — a grid drawn over
        the stacked layers in 3D would just be visual noise. The spacing
        snaps to a 1/2/5×10ⁿ mm value chosen so adjacent lines land roughly
        70 px apart at the current zoom; lines on whole-10ⁿ-mm coordinates
        are drawn slightly brighter as a coarse reference."""
        if not self._editor_mode or self._view_mode == "3d":
            return
        mpp = max(float(self._mm_per_pixel), 1e-9)
        w, h = float(self.width()), float(self.height())
        raw_mm = 70.0 * mpp
        if raw_mm <= 0.0:
            return
        base = 10.0 ** math.floor(math.log10(raw_mm))
        step_mm = base
        for mult in (1.0, 2.0, 5.0, 10.0):
            step_mm = base * mult
            if step_mm / mpp >= 70.0:
                break
        x_min, x_max, y_min, y_max = self.view_range()
        major_every = 5   # every 5th line is the brighter "major" line
        thin = QPen(QColor(120, 150, 210, 45))
        thick = QPen(QColor(140, 170, 230, 90))
        for pen in (thin, thick):
            pen.setWidthF(1.0)
            pen.setCosmetic(True)
        painter.save()
        i0 = math.ceil(x_min / step_mm)
        i = i0
        x = i0 * step_mm
        while x <= x_max:
            px, _ = self.world_to_screen(x, y_min)
            painter.setPen(thick if i % major_every == 0 else thin)
            painter.drawLine(QPointF(px, 0.0), QPointF(px, h))
            i += 1
            x += step_mm
        j0 = math.ceil(y_min / step_mm)
        j = j0
        y = j0 * step_mm
        while y <= y_max:
            _, py = self.world_to_screen(x_min, y)
            painter.setPen(thick if j % major_every == 0 else thin)
            painter.drawLine(QPointF(0.0, py), QPointF(w, py))
            j += 1
            y += step_mm
        painter.restore()

    def _draw_editor_selection(self, painter: QPainter) -> None:
        """Yellow box around the editor-mode component selection — the
        component's world-space bounding box projected to the screen, so
        it tracks pan / zoom (and the camera in 3D). Same yellow + pixel
        thickness as the selected source / sink marker's box."""
        if not self._editor_mode or self._editor_selection_bbox is None:
            return
        x0, y0, x1, y1 = self._editor_selection_bbox
        poly = QPolygonF()
        for wx, wy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            px, py = self.world_to_screen(wx, wy, 0.0)
            if px < -1e8 or py < -1e8:   # a corner is behind the camera
                return
            poly.append(QPointF(px, py))
        painter.save()
        pen = QPen(QColor("#ffff00"))
        pen.setWidthF(_EDITOR_SELECTION_BOX_PX)
        pen.setJoinStyle(Qt.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPolygon(poly)
        painter.restore()

    def _draw_primitive_selection(self, painter: QPainter) -> None:
        """Dashed yellow outline around the click-selected copper primitive.
        World-mm rings (set via :meth:`set_primitive_selection_outline`)
        projected to screen each frame, so the dashes track pan / zoom and
        the 3D camera. Works in any view mode; same yellow + thickness as
        the editor-mode selection box."""
        if self._primitive_selection_rings is None:
            return
        painter.save()
        pen = QPen(QColor("#ffff00"))
        pen.setWidthF(_EDITOR_SELECTION_BOX_PX)
        pen.setStyle(Qt.DashLine)
        pen.setJoinStyle(Qt.MiterJoin)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        for ring in self._primitive_selection_rings:
            if len(ring) < 2:
                continue
            poly = QPolygonF()
            ok = True
            for wx, wy in ring:
                px, py = self.world_to_screen(wx, wy, 0.0)
                if px < -1e8 or py < -1e8:   # behind 3D camera
                    ok = False
                    break
                poly.append(QPointF(px, py))
            if not ok or poly.size() < 2:
                continue
            painter.drawPolygon(poly)
        painter.restore()

    def _draw_measurement_line(self, painter: QPainter) -> None:
        """Draw the host-supplied measurement line (Shift-drag voltage
        probe) as a thin white segment in pixel space, plus a small
        filled circle at the origin endpoint so the anchor point is
        obvious at a glance. World-mm endpoints are projected through
        :meth:`world_to_screen` so the line tracks pan/zoom and the 3D
        camera correctly."""
        if self._measurement_line is None:
            return
        x0_mm, y0_mm, x1_mm, y1_mm = self._measurement_line
        x0_px, y0_px = self.world_to_screen(x0_mm, y0_mm)
        x1_px, y1_px = self.world_to_screen(x1_mm, y1_mm)
        # world_to_screen returns -1e9 sentinels for behind-camera points
        # in 3D — drop the line entirely if either endpoint is invalid.
        if min(x0_px, x1_px) < -1e8 or min(y0_px, y1_px) < -1e8:
            return
        painter.save()
        pen = QPen(QColor("#ffffff"))
        pen.setWidthF(1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(QPointF(x0_px, y0_px), QPointF(x1_px, y1_px))
        # Origin marker: small filled white disc anchored at (x0, y0).
        # 3 px radius reads at any zoom level without obscuring the
        # underlying copper.
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawEllipse(QPointF(x0_px, y0_px), 3.0, 3.0)
        painter.restore()

    def _draw_overlay_labels(self, painter: QPainter,
                              on_top: bool = False) -> None:
        """Draw the overlay text labels (reference designators) as world-
        anchored text that tracks pan / zoom. The font size follows each
        label's world-mm height; labels too small to read at the current
        zoom are skipped (keeps the canvas uncluttered and the painter
        cheap on dense boards).

        ``on_top`` selects which pass to draw: ``False`` for labels that
        sit beneath the marker dots (silkscreen designators), ``True``
        for labels that need to sit above them (via-span text inside the
        via). Labels opt in to the top pass with ``on_top: True`` in the
        label dict; everything else defaults to the under pass.
        """
        if not self._overlay_labels:
            return
        mpp = max(float(self._mm_per_pixel), 1e-9)
        w, h = self.width(), self.height()
        painter.save()
        font = QFont(painter.font())
        for lab in self._overlay_labels:
            if bool(lab.get("on_top", False)) != on_top:
                continue
            text = lab.get("text") or ""
            if not text:
                continue
            wz = float(lab.get("z", 0.0) or 0.0)
            px, py = self.world_to_screen(
                float(lab["x"]), float(lab["y"]), wz)
            if px < -1e8 or py < -1e8:
                continue  # behind the 3D camera
            if px < -200 or px > w + 200 or py < -200 or py > h + 200:
                continue
            size_px = float(lab.get("height_mm", 1.0) or 1.0) / mpp
            if size_px < 5.0:
                continue  # too small to read at this zoom — skip
            font.setPixelSize(int(round(min(size_px, 48.0))))
            painter.setFont(font)
            painter.setPen(QPen(QColor(lab.get("color", "#d0d0d0"))))
            rot = float(lab.get("rotation_deg", 0.0) or 0.0) % 360.0
            centered = bool(lab.get("center", False))
            if centered:
                fm = QFontMetricsF(font)
                tw = fm.horizontalAdvance(text)
                # Cap-height-based vertical centre — keeps the glyph body
                # visually centred on the anchor instead of dropping the
                # baseline onto it.
                th = fm.capHeight() if fm.capHeight() > 0 else fm.ascent()
                dx = -tw * 0.5
                dy = th * 0.5
            else:
                dx = 0.0
                dy = 0.0
            if rot != 0.0:
                painter.save()
                painter.translate(px, py)
                painter.rotate(-rot)  # screen y is down; Altium rotates CCW
                painter.drawText(QPointF(dx, dy), text)
                painter.restore()
            else:
                painter.drawText(QPointF(px + dx, py + dy), text)
        painter.restore()

    def _draw_markers(self, painter: QPainter) -> None:
        if not self._markers:
            return
        mpp = max(float(self._mm_per_pixel), 1e-9)
        for group in self._markers:
            if group.xs.size == 0:
                continue
            fill = QBrush(QColor(group.color))
            pen = QPen(QColor(group.edge_color))
            pen.setWidthF(group.edge_width)
            painter.setBrush(fill)
            painter.setPen(pen)
            default_size = float(group.size)
            zs = group.zs
            wdiams = group.world_diameters_mm
            obrounds = group.world_obrounds
            min_px = float(group.min_pixel_diameter)
            # Per-marker layer-colour ring (drawn as an enlarged glyph
            # behind the marker so the colour peeks out as a band around
            # its edge). Only consulted when both a width and a colour
            # list are supplied.
            ring_colors = group.ring_colors
            ring_w = float(group.ring_width)
            rings_on = ring_w > 0.0 and ring_colors is not None
            for i in range(group.xs.size):
                wx = float(group.xs[i])
                wy = float(group.ys[i])
                wz = float(zs[i]) if zs is not None else 0.0
                # World-space sizing: each marker scales with zoom, but
                # never shrinks below ``min_pixel_diameter`` — keeps
                # vias visible (with intentional overlap) when zoomed
                # out beyond the via's physical footprint.
                if wdiams is not None:
                    size = max(float(wdiams[i]) / mpp, min_px)
                else:
                    size = default_size
                # Skip points outside the viewport — saves QPainter calls
                # in deep zoom and silently drops behind-camera points
                # in 3D (world_to_screen returns the sentinel -1e9).
                px, py = self.world_to_screen(wx, wy, wz)
                if (px < -size or px > self.width() + size
                        or py < -size or py > self.height() + size):
                    continue
                # Slotted-drill marker: draw a capsule sized + oriented to the
                # slot. The long-axis endpoints are projected through the same
                # world→screen transform as the centre, so the on-screen length
                # and angle stay correct under any pan / zoom / y-flip; the
                # short axis (width) scales with zoom, floored at min_px.
                ob = obrounds[i] if obrounds is not None else None
                if ob is not None:
                    length_mm, width_mm, rot_deg, rounded = ob
                    th = math.radians(rot_deg)
                    hx = 0.5 * length_mm * math.cos(th)
                    hy = 0.5 * length_mm * math.sin(th)
                    sx1, sy1 = self.world_to_screen(wx + hx, wy + hy, wz)
                    sx2, sy2 = self.world_to_screen(wx - hx, wy - hy, wz)
                    length_px = math.hypot(sx1 - sx2, sy1 - sy2)
                    width_px = max(float(width_mm) / mpp, min_px)
                    ang = math.atan2(sy1 - sy2, sx1 - sx2)
                    self._draw_obround(painter, px, py, length_px,
                                       width_px, ang, rounded)
                    continue
                rc = ring_colors[i] if rings_on else None
                if rc:
                    # Stroke the SAME-size glyph with a thick layer-colour
                    # pen, then draw the opaque marker on top. A stroke has
                    # uniform perpendicular width on every edge (unlike a
                    # scaled-up fill, which balloons the corners), so the
                    # band reads as an even outline; the marker covers its
                    # inner half, leaving a ~ring_w band of layer colour.
                    # Round joins keep triangle/diamond corners clean.
                    ring_pen = QPen(QColor(rc))
                    ring_pen.setWidthF(2.0 * ring_w)
                    ring_pen.setJoinStyle(Qt.RoundJoin)
                    painter.setPen(ring_pen)
                    painter.setBrush(Qt.NoBrush)
                    self._draw_symbol(painter, px, py, group.symbol, size)
                    painter.setBrush(fill)
                    painter.setPen(pen)
                self._draw_symbol(painter, px, py, group.symbol, size)

    @staticmethod
    def _draw_obround(painter: QPainter, px: float, py: float,
                      length_px: float, width_px: float,
                      angle_rad: float, rounded: bool = True) -> None:
        """Draw a slot marker centred at ``(px, py)`` with the given
        screen-space length, width and orientation. ``rounded`` selects an
        obround / stadium (radius = width / 2, matching Altium's rounded slot
        and the filled overlay) versus a square-cornered rectangle (Altium's
        "Rectangular" hole). Used for slotted drill markers."""
        r = 0.5 * max(width_px, 0.0)
        length_px = max(length_px, width_px)  # never shorter than a circle
        painter.save()
        painter.translate(px, py)
        painter.rotate(math.degrees(angle_rad))
        rect = QRectF(-0.5 * length_px, -r, length_px, 2.0 * r)
        if rounded:
            painter.drawRoundedRect(rect, r, r)
        else:
            painter.drawRect(rect)
        painter.restore()

    @staticmethod
    def _draw_symbol(painter: QPainter, px: float, py: float,
                     symbol: str, size: float) -> None:
        """Draw a single marker centred at (px, py) at the given pixel
        size. Pixel-mode: marker doesn't scale with zoom."""
        r = size * 0.5
        if symbol == "o":
            painter.drawEllipse(QPointF(px, py), r, r)
        elif symbol == "s":
            painter.drawRect(QRectF(px - r, py - r, size, size))
        elif symbol == "d":
            poly = QPolygonF([
                QPointF(px, py - r),
                QPointF(px + r, py),
                QPointF(px, py + r),
                QPointF(px - r, py),
            ])
            painter.drawPolygon(poly)
        elif symbol == "star":
            # 5-pointed star — outer radius r, inner radius r*0.4.
            outer = r
            inner = r * 0.4
            poly = QPolygonF()
            for k in range(10):
                angle = -math.pi / 2.0 + k * math.pi / 5.0
                rad = outer if (k % 2 == 0) else inner
                poly.append(QPointF(px + rad * math.cos(angle),
                                    py + rad * math.sin(angle)))
            painter.drawPolygon(poly)
        elif symbol == "tri_up":
            # Equilateral upward-pointing triangle, apex at top.
            painter.drawPolygon(QPolygonF([
                QPointF(px,             py - r),
                QPointF(px + r * 0.866, py + r * 0.5),
                QPointF(px - r * 0.866, py + r * 0.5),
            ]))
        elif symbol == "tri_down":
            # Equilateral downward-pointing triangle, apex at bottom.
            painter.drawPolygon(QPolygonF([
                QPointF(px,             py + r),
                QPointF(px + r * 0.866, py - r * 0.5),
                QPointF(px - r * 0.866, py - r * 0.5),
            ]))
        elif symbol in ("tri_up_box", "tri_down_box"):
            # Tight selection box around a tri_up / tri_down glyph of the
            # same `size`. The triangle is NOT centred within its `size`
            # square (apex at ±r, base at ∓0.5r), so a plain centred
            # square leaves a gap one side and clips the apex on the
            # other — this draws the triangle's true bounding rectangle
            # instead, plus a hairline pad so the box stroke clears the
            # triangle's own outline.
            pad = 2.0
            half_w = r * 0.866 + pad
            if symbol == "tri_up_box":
                y0, y1 = py - r - pad, py + r * 0.5 + pad
            else:
                y0, y1 = py - r * 0.5 - pad, py + r + pad
            painter.drawRect(QRectF(px - half_w, y0, 2.0 * half_w, y1 - y0))
        elif symbol == "bolt":
            # Lightning bolt — 7-point concave polygon. Geometry lifted
            # from the Material Design ``flash_on`` icon and normalised
            # to ±1 from the marker centre.
            pts = (
                (-0.50, -1.00),
                (-0.20, -0.20),
                ( 0.10, -0.20),
                ( 0.50, -1.00),
                ( 0.20,  0.10),
                ( 0.50,  0.10),
                (-0.20,  1.00),
            )
            painter.drawPolygon(QPolygonF([
                QPointF(px + dx * r, py + dy * r) for dx, dy in pts
            ]))
        elif symbol == "target":
            # Bullseye — outer filled circle in the marker's brush
            # colour, with a small contrasting centre dot stamped on
            # top so it reads as a target / "here" pin at any zoom.
            painter.drawEllipse(QPointF(px, py), r, r)
            pen = painter.pen()
            painter.save()
            painter.setBrush(QBrush(pen.color()))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(px, py), r * 0.35, r * 0.35)
            painter.restore()
        else:
            painter.drawEllipse(QPointF(px, py), r, r)

    def _draw_overlay_chip(self, painter: QPainter, html: str,
                           anchor_right: bool) -> None:
        if not html:
            return
        # Build a QTextDocument so we can measure + render HTML cleanly.
        doc = QTextDocument()
        doc.setDefaultFont(QFont("Segoe UI", 9))
        doc.setHtml(html)
        # Force light text — the dark chip background otherwise eats the
        # default near-black foreground.
        doc.setDefaultStyleSheet("body, p, table, td, span { color: #e6e6e6; }")
        doc.setHtml(html)  # re-set so style sheet applies
        doc.adjustSize()
        size = doc.size()
        margin = 8
        chip_w = size.width() + 12
        chip_h = size.height() + 6
        if anchor_right:
            x = self.width() - chip_w - margin - self._legend_right_inset
        else:
            x = margin
        y = margin
        # Background + border in one drawRect. Setting BOTH pen and
        # brush explicitly here is critical — _draw_markers leaves the
        # painter's brush set to whatever the last marker group used
        # (e.g. the yellow Vias-tab "Go" highlight), and drawRect would
        # otherwise re-fill the chip interior with that leftover brush.
        painter.save()
        painter.setPen(QColor("#666"))
        painter.setBrush(QBrush(QColor(34, 34, 34, 240)))
        painter.drawRect(QRectF(x, y, chip_w, chip_h))
        # Text.
        painter.translate(x + 6, y + 3)
        doc.drawContents(painter)
        painter.restore()

    # Top-right legend chip — manual layout (instead of QTextDocument) so
    # we can compute exact per-row rects for click hit-testing and draw a
    # diagonal slash across rows whose ``hidden`` flag is set. Layout
    # mirrors the table the plain-HTML legend used: glyph swatch column,
    # gap, label column, with uniform row height.
    _LEGEND_MARGIN: float = 8.0     # chip-to-widget-edge padding
    _LEGEND_PAD_X: float = 6.0      # chip interior horizontal padding
    _LEGEND_PAD_Y: float = 3.0      # chip interior vertical padding
    _LEGEND_SWATCH_W: float = 16.0  # glyph column width
    _LEGEND_LABEL_GAP: float = 6.0  # px between glyph and label

    def _draw_legend_chip(self, painter: QPainter) -> None:
        rows = self._overlay_top_right_legend
        if not rows:
            return
        label_font = QFont("Segoe UI", 9)
        glyph_font = QFont("Segoe UI", 11)
        label_fm = QFontMetricsF(label_font)
        glyph_fm = QFontMetricsF(glyph_font)
        row_h = max(label_fm.height(), glyph_fm.height()) + 2.0

        label_w = 0.0
        for r in rows:
            label_w = max(label_w, label_fm.horizontalAdvance(r.label))

        content_w = (self._LEGEND_SWATCH_W + self._LEGEND_LABEL_GAP
                     + label_w)
        chip_w = content_w + 2 * self._LEGEND_PAD_X
        chip_h = row_h * len(rows) + 2 * self._LEGEND_PAD_Y
        x = (self.width() - chip_w - self._LEGEND_MARGIN
             - self._legend_right_inset)
        y = self._LEGEND_MARGIN

        painter.save()
        painter.setPen(QColor("#666"))
        painter.setBrush(QBrush(QColor(34, 34, 34, 240)))
        painter.drawRect(QRectF(x, y, chip_w, chip_h))

        for i, r in enumerate(rows):
            row_y = y + self._LEGEND_PAD_Y + i * row_h
            self._legend_row_rects.append(
                (QRectF(x, row_y, chip_w, row_h), r.key))

            glyph_rect = QRectF(x + self._LEGEND_PAD_X, row_y,
                                self._LEGEND_SWATCH_W, row_h)
            painter.setFont(glyph_font)
            painter.setPen(QColor(r.color))
            painter.drawText(glyph_rect,
                             Qt.AlignVCenter | Qt.AlignHCenter, r.glyph)

            label_rect = QRectF(
                x + self._LEGEND_PAD_X + self._LEGEND_SWATCH_W
                + self._LEGEND_LABEL_GAP,
                row_y, label_w, row_h)
            painter.setFont(label_font)
            painter.setPen(QColor("#e6e6e6"))
            painter.drawText(label_rect,
                             Qt.AlignVCenter | Qt.AlignLeft, r.label)

            if r.hidden:
                # Diagonal slash matching the eye-icon off-state — drawn
                # across the row's interior so both the swatch and label
                # read as "visibility off". Same bright neutral tone as
                # the label text so it stays visible on any swatch colour.
                slash_pen = QPen(QColor("#e6e6e6"))
                slash_pen.setWidthF(1.6)
                slash_pen.setCapStyle(Qt.RoundCap)
                painter.setPen(slash_pen)
                painter.setBrush(Qt.NoBrush)
                m = 2.0
                x0 = x + self._LEGEND_PAD_X - m
                x1 = x + chip_w - self._LEGEND_PAD_X + m
                y0 = row_y + row_h - m
                y1 = row_y + m
                painter.drawLine(QPointF(x0, y0), QPointF(x1, y1))
        painter.restore()

    def _legend_row_key_at(self, pos) -> str | None:
        """Return the key of the legend row under ``pos`` (a QPointF in
        widget pixels), or ``None`` if the point doesn't land on a row."""
        for rect, key in self._legend_row_rects:
            if rect.contains(pos):
                return key
        return None

    # ------------------------------------------------------------------
    # Mouse interaction (matches Altium's PCB view conventions)
    #   Both modes: RightDrag = pan, Wheel = zoom, LeftClick = clicked,
    #               MiddleDrag vertical = exponential zoom
    #   3D mode also: Shift+RightDrag = rotate (orbit around target)
    #   Left-drag (any mode) is just drag-detection so the click-clears-
    #   highlight handler can ignore accidental tiny drags.
    # ------------------------------------------------------------------

    # Pixels of drag per degree of yaw/pitch in 3D rotate mode.
    _ROTATE_PIXELS_PER_DEG: float = 4.0
    # 2D middle-button zoom sensitivity: number of pixels of vertical
    # drag that doubles / halves the visible mm/pixel. Drag UP zooms IN
    # (smaller mm/pixel), drag DOWN zooms OUT.
    _MIDDLE_ZOOM_PIXELS_PER_DOUBLING: float = 150.0

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            # Top-right legend chip: a click on a row toggles that marker
            # category's visibility. Handled before anything else so it
            # never starts a pan / marker-drag gesture.
            row_key = self._legend_row_key_at(ev.position())
            if row_key is not None:
                self.legendRowClicked.emit(row_key)
                # Suppress the matching release so the empty-space click
                # handler doesn't also fire (see _legend_press_consumed).
                self._legend_press_consumed = True
                ev.accept()
                return
            # Editor mode: a left press that lands on a draggable free
            # marker becomes a marker drag rather than a pan / click.
            if (self._editor_mode and self._view_mode == "2d"
                    and self._editor_drag_hit_test is not None):
                wx, wy = self.screen_to_world(ev.position().x(),
                                              ev.position().y())
                try:
                    on_marker = bool(self._editor_drag_hit_test(wx, wy))
                except Exception:
                    on_marker = False
                if on_marker:
                    self._editor_drag_active = True
                    self._press_origin = None
                    self._is_panning = False
                    self._apply_editor_cursor("closed")
                    self.editorDragStarted.emit(wx, wy)
                    ev.accept()
                    return
            self._press_origin = QPointF(ev.position())
            self._press_center = (self._view_center_x, self._view_center_y)
            self._is_panning = False
            ev.accept()
            return
        if ev.button() == Qt.RightButton:
            # Right-button pans (both modes); Shift+Right rotates in 3D.
            self._right_press_origin = QPointF(ev.position())
            self._right_press_center = (
                self._view_center_x, self._view_center_y,
            )
            self._right_press_target = self._cam_target
            self._right_press_yaw = self._cam_yaw_deg
            self._right_press_pitch = self._cam_pitch_deg
            self._right_press_mpp = self._mm_per_pixel
            self._right_press_shift = bool(ev.modifiers() & Qt.ShiftModifier)
            self._right_is_dragging = False
            ev.accept()
            return
        if ev.button() == Qt.MiddleButton:
            # Hold-and-drag zoom in either mode. Vertical drag scales
            # exponentially (drag up = zoom in, drag down = zoom out).
            pos = ev.position()
            self._middle_press_origin = QPointF(pos)
            if self._view_mode == "2d":
                # 2D: pivot around the world point under the press cursor.
                self._middle_press_world = self.screen_to_world(pos.x(), pos.y())
                self._middle_press_mpp = self._mm_per_pixel
            else:
                # 3D: dolly the camera in/out (pivots around the camera
                # target — simplest sensible behaviour without ray-casts).
                self._middle_press_distance = self._cam_distance
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:
        pos = ev.position()
        # Editor-mode free-marker drag — report the cursor world position
        # to the host and skip every pan / hover code path below.
        if self._editor_drag_active:
            wx, wy = self.screen_to_world(pos.x(), pos.y())
            self._last_hover_pixel = (float(pos.x()), float(pos.y()))
            self.editorDragMoved.emit(wx, wy)
            return
        # Hover cursor (priority: ongoing gesture > legend chip hover >
        # editor free-marker hover > default). Skip entirely while a
        # press is in flight so a pan/drag doesn't fight the cursor
        # update.
        if (self._press_origin is None
                and self._right_press_origin is None
                and self._middle_press_origin is None):
            if self._legend_row_key_at(pos) is not None:
                self._apply_editor_cursor("pointing")
            elif (self._editor_mode and self._view_mode == "2d"
                    and self._editor_drag_hit_test is not None):
                hx, hy = self.screen_to_world(pos.x(), pos.y())
                try:
                    over = bool(self._editor_drag_hit_test(hx, hy))
                except Exception:
                    over = False
                self._apply_editor_cursor("open" if over else "default")
            else:
                self._apply_editor_cursor("default")
        # Left-button drag is no longer wired to pan. We only track its
        # drag-distance so the click-clears-highlight handler can ignore
        # accidental movements.
        if self._press_origin is not None:
            dx_px = pos.x() - self._press_origin.x()
            dy_px = pos.y() - self._press_origin.y()
            if not self._is_panning and (
                abs(dx_px) > self._CLICK_DRAG_THRESHOLD_PX
                or abs(dy_px) > self._CLICK_DRAG_THRESHOLD_PX
            ):
                self._is_panning = True

        # --- Right-button drag ---
        # 2D: pan the orthographic view.
        # 3D: pan the camera target (no shift) or rotate (Shift held).
        if self._right_press_origin is not None:
            dx_px = pos.x() - self._right_press_origin.x()
            dy_px = pos.y() - self._right_press_origin.y()
            if not self._right_is_dragging and (
                abs(dx_px) > self._CLICK_DRAG_THRESHOLD_PX
                or abs(dy_px) > self._CLICK_DRAG_THRESHOLD_PX
            ):
                self._right_is_dragging = True
            if self._right_is_dragging and self._view_mode == "2d":
                cx0, cy0 = self._right_press_center
                mpp = self._right_press_mpp
                new_cx = cx0 - dx_px * mpp
                new_cy = cy0 + dy_px * mpp
                self.set_view_center_scale(new_cx, new_cy, mpp)
            elif self._right_is_dragging and self._view_mode == "3d":
                if self._right_press_shift:
                    # Rotate. Yaw is INVERTED relative to the raw dx so
                    # the model orbits with the drag (drag-right makes
                    # the right-hand side of the board come toward the
                    # camera, matching Altium / SolidWorks convention).
                    # Pitch follows the drag direction directly: drag
                    # down → camera tilts to look more top-down.
                    self._cam_yaw_deg = (self._right_press_yaw
                                          - dx_px / self._ROTATE_PIXELS_PER_DEG)
                    new_pitch = (self._right_press_pitch
                                  + dy_px / self._ROTATE_PIXELS_PER_DEG)
                    # Clamp so we never flip past straight-up or straight-
                    # down (avoids gimbal-style camera roll surprises).
                    self._cam_pitch_deg = max(-89.0, min(89.0, new_pitch))
                else:
                    # Pan: shift the camera target in the view plane.
                    # mm per screen pixel at the target depth ≈
                    # 2 * dist * tan(fov/2) / widget_height
                    h_px = max(1, self.height())
                    mm_per_px = (2.0 * self._cam_distance * math.tan(
                        math.radians(self._cam_fov_deg) * 0.5)) / h_px
                    yaw = math.radians(self._cam_yaw_deg)
                    pitch = math.radians(self._cam_pitch_deg)
                    # Right and up basis vectors in world space.
                    right = (math.cos(yaw), math.sin(yaw), 0.0)
                    up = (-math.sin(pitch) * math.sin(yaw),
                           math.sin(pitch) * math.cos(yaw),
                           math.cos(pitch))
                    tx0, ty0, tz0 = self._right_press_target
                    self._cam_target = (
                        tx0 - dx_px * mm_per_px * right[0]
                            + dy_px * mm_per_px * up[0],
                        ty0 - dx_px * mm_per_px * right[1]
                            + dy_px * mm_per_px * up[1],
                        tz0 - dx_px * mm_per_px * right[2]
                            + dy_px * mm_per_px * up[2],
                    )
                self.viewChanged.emit()
                self.update()

        # --- Middle-button drag = exponential zoom (both modes) ---
        # Vertical drag from the press origin determines the zoom
        # factor; negative dy (drag UP) zooms in, positive zooms out.
        if self._middle_press_origin is not None:
            dy_px = pos.y() - self._middle_press_origin.y()
            factor = pow(2.0,
                         dy_px / self._MIDDLE_ZOOM_PIXELS_PER_DOUBLING)
            if self._view_mode == "2d":
                # Pivot around the world point under the press cursor.
                new_mpp = self._middle_press_mpp * factor
                new_mpp = max(min(new_mpp, 1e6), 1e-9)
                wx0, wy0 = self._middle_press_world
                anchor_px = self._middle_press_origin.x()
                anchor_py = self._middle_press_origin.y()
                new_cx = wx0 - (anchor_px - self.width() * 0.5) * new_mpp
                new_cy = wy0 + (anchor_py - self.height() * 0.5) * new_mpp
                self.set_view_center_scale(new_cx, new_cy, new_mpp)
            else:  # 3D
                self._cam_distance = self._dolly_cam_distance(
                    self._middle_press_distance, factor)
                self.viewChanged.emit()
                self.update()

        # Always emit hover (caller throttles + skips during drag).
        self._last_hover_pixel = (float(pos.x()), float(pos.y()))
        wx, wy = self.screen_to_world(pos.x(), pos.y())
        inside = (0 <= pos.x() < self.width()
                  and 0 <= pos.y() < self.height())
        self.mouseHoveredAt.emit(wx, wy, inside)

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            if self._legend_press_consumed:
                # Matching release for a legend-row click — swallow it so
                # the host's empty-space ``clicked`` handler doesn't fire.
                self._legend_press_consumed = False
                ev.accept()
                return
            if self._editor_drag_active:
                self._editor_drag_active = False
                wx, wy = self.screen_to_world(ev.position().x(),
                                              ev.position().y())
                self._apply_editor_cursor("open")
                self.editorDragReleased.emit(wx, wy)
                ev.accept()
                return
            was_panning = self._is_panning
            self._press_origin = None
            self._is_panning = False
            if not was_panning:
                wx, wy = self.screen_to_world(ev.position().x(),
                                                ev.position().y())
                self.clicked.emit(wx, wy)
            ev.accept()
            return
        if ev.button() == Qt.RightButton:
            self._right_press_origin = None
            self._right_is_dragging = False
            ev.accept()
            return
        if ev.button() == Qt.MiddleButton:
            self._middle_press_origin = None
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def contextMenuEvent(self, ev) -> None:
        # Swallow the default right-click context menu — right-button
        # drag is the pan gesture in both 2D and 3D modes, so a popup
        # would only get in the way.
        ev.accept()

    def wheelEvent(self, ev) -> None:
        delta = ev.angleDelta().y()
        if delta == 0:
            return
        # 120 = one wheel click; 1.2x zoom factor per click.
        factor = pow(1.0 / 1.2, delta / 120.0)
        if self._view_mode == "3d":
            # 3D: dolly the camera in/out. See _dolly_cam_distance for
            # why we don't just multiply cam_distance directly.
            self._cam_distance = self._dolly_cam_distance(
                self._cam_distance, factor)
            self.viewChanged.emit()
            self.update()
            ev.accept()
            return
        # 2D: zoom around the cursor — keep the world point under the
        # mouse fixed across the scale change.
        pos = ev.position()
        wx_before, wy_before = self.screen_to_world(pos.x(), pos.y())
        new_mpp = self._mm_per_pixel * factor
        new_mpp = max(min(new_mpp, 1e6), 1e-9)
        new_cx = wx_before - (pos.x() - self.width() * 0.5) * new_mpp
        new_cy = wy_before + (pos.y() - self.height() * 0.5) * new_mpp
        self.set_view_center_scale(new_cx, new_cy, new_mpp)
        ev.accept()
