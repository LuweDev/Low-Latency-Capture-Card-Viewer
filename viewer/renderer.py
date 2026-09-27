"""Video widgets.

GLVideoWidget uploads each frame to a texture once and does all scaling on the
GPU in a fragment shader, so the per-frame CPU cost is a single ~6 MB copy
regardless of window size or scaling mode. SoftwareVideoWidget is a QPainter
fallback for machines without OpenGL 3.3 (e.g. some Remote Desktop sessions).
"""

import math
import time

import numpy as np
from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtOpenGL import (QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture,
                              QOpenGLVertexArrayObject)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

SCALING_MODES = [
    ("none", "None (1:1 pixels)"),
    ("nearest", "Nearest Neighbour"),
    ("bilinear", "Bilinear"),
    ("sharp_bilinear", "Sharp Bilinear"),
    ("bicubic", "Bicubic"),
    ("lanczos", "Lanczos-3"),
    ("integer", "Integer Scaling"),
]
SCALING_LABELS = dict(SCALING_MODES)

# Below this output/source ratio the source-space kernels (bilinear, bicubic,
# Lanczos) start skipping source pixels and shimmer, so trilinear filtering
# over mipmaps is used instead.
MIPMAP_BELOW_SCALE = 0.5

GL_COLOR_BUFFER_BIT = 0x4000
GL_TRIANGLE_STRIP = 0x0005
GL_TEXTURE_2D = 0x0DE1
GL_UNPACK_ALIGNMENT = 0x0CF5
GL_BGR = 0x80E0
GL_UNSIGNED_BYTE = 0x1401


def target_rect(mode, src_w, src_h, dst_w, dst_h):
    """Where the video goes inside a dst_w x dst_h surface, in device pixels.

    Returns (x, y, w, h) with (x, y) the top-left corner. The rect may extend
    past the surface (1:1 mode on a window smaller than the source).
    """
    if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        return 0, 0, 0, 0
    fit = min(dst_w / src_w, dst_h / src_h)
    if mode == "none":
        scale = 1.0
    elif mode == "integer" and fit >= 1.0:
        scale = float(math.floor(fit + 1e-9))
    else:
        scale = fit
    w = max(1, round(src_w * scale))
    h = max(1, round(src_h * scale))
    return (dst_w - w) // 2, (dst_h - h) // 2, w, h


def effective_filter(mode, scale):
    """The filter actually applied for a mode at a given output/source scale."""
    if mode in ("none", "nearest"):
        return "nearest"
    if mode == "integer":
        return "nearest" if scale >= 1.0 else ("trilinear" if scale < MIPMAP_BELOW_SCALE else "bilinear")
    if scale < MIPMAP_BELOW_SCALE:
        return "trilinear"
    if mode == "sharp_bilinear" and scale < 2.0:
        return "bilinear"  # sharp bilinear only differs from bilinear at >= 2x
    return mode


class RenderStats:
    """Presented-frame rate and how old frames are when drawn."""

    def __init__(self):
        self.display_fps = 0.0
        self.frame_age_ms = None
        self.skipped = 0      # frames captured but replaced before they were drawn
        self._frames = 0
        self._window_start = time.perf_counter()

    def frame_presented(self, captured_at):
        now = time.perf_counter()
        age = (now - captured_at) * 1000.0
        # Exponential moving average so the overlay reading is stable.
        self.frame_age_ms = age if self.frame_age_ms is None else self.frame_age_ms * 0.9 + age * 0.1
        self._frames += 1
        elapsed = now - self._window_start
        if elapsed >= 1.0:
            self.display_fps = self._frames / elapsed
            self._frames = 0
            self._window_start = now

    def reset_if_idle(self):
        if time.perf_counter() - self._window_start > 2.0:
            self.display_fps = 0.0
            self.frame_age_ms = None
            self._frames = 0
            self._window_start = time.perf_counter()


_VERTEX_SHADER = """
#version 330 core
out vec2 v_uv;
void main() {
    // Full-viewport quad from gl_VertexID, drawn as a 4-vertex triangle strip.
    vec2 p = vec2(float(gl_VertexID & 1), float(gl_VertexID >> 1));
    v_uv = vec2(p.x, 1.0 - p.y);  // texture row 0 is the top of the image
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

_FRAGMENT_SHADERS = {
    # Nearest, bilinear and trilinear: the texture unit does the work.
    "plain": """
#version 330 core
in vec2 v_uv;
out vec4 frag_color;
uniform sampler2D u_tex;
void main() {
    frag_color = vec4(texture(u_tex, v_uv).rgb, 1.0);
}
""",
    # Sharp bilinear: nearest-neighbour prescale to the largest integer
    # multiple, then bilinear for the remainder, done in one bilinear tap by
    # snapping the sample position towards texel centres.
    "sharp_bilinear": """
#version 330 core
in vec2 v_uv;
out vec4 frag_color;
uniform sampler2D u_tex;
uniform vec2 u_out_size;
void main() {
    vec2 tex_size = vec2(textureSize(u_tex, 0));
    vec2 prescale = max(floor(u_out_size / tex_size), vec2(1.0));
    vec2 texel = v_uv * tex_size;
    vec2 texel_floor = floor(texel);
    vec2 center_dist = fract(texel) - 0.5;
    vec2 region = 0.5 - 0.5 / prescale;
    vec2 f = (center_dist - clamp(center_dist, -region, region)) * prescale + 0.5;
    frag_color = vec4(texture(u_tex, (texel_floor + f) / tex_size).rgb, 1.0);
}
""",
    # Catmull-Rom bicubic, 4x4 taps.
    "bicubic": """
#version 330 core
in vec2 v_uv;
out vec4 frag_color;
uniform sampler2D u_tex;
vec4 weights(float t) {
    return vec4(t * (-0.5 + t * (1.0 - 0.5 * t)),
                1.0 + t * t * (-2.5 + 1.5 * t),
                t * (0.5 + t * (2.0 - 1.5 * t)),
                t * t * (-0.5 + 0.5 * t));
}
void main() {
    ivec2 size = textureSize(u_tex, 0);
    vec2 pos = v_uv * vec2(size) - 0.5;
    vec2 base = floor(pos);
    vec2 f = pos - base;
    vec4 wx = weights(f.x);
    vec4 wy = weights(f.y);
    ivec2 b = ivec2(base);
    ivec2 max_coord = size - 1;
    vec3 color = vec3(0.0);
    for (int j = 0; j < 4; ++j) {
        vec3 row = vec3(0.0);
        for (int i = 0; i < 4; ++i) {
            ivec2 c = clamp(b + ivec2(i - 1, j - 1), ivec2(0), max_coord);
            row += texelFetch(u_tex, c, 0).rgb * wx[i];
        }
        color += row * wy[j];
    }
    frag_color = vec4(clamp(color, 0.0, 1.0), 1.0);
}
""",
    # Lanczos with a = 3, 6x6 taps, weights normalised to sum to 1.
    "lanczos": """
#version 330 core
in vec2 v_uv;
out vec4 frag_color;
uniform sampler2D u_tex;
const float PI = 3.14159265358979;
float lanczos3(float x) {
    x = abs(x);
    if (x < 1e-5) return 1.0;
    if (x >= 3.0) return 0.0;
    float px = PI * x;
    return 3.0 * sin(px) * sin(px / 3.0) / (px * px);
}
void main() {
    ivec2 size = textureSize(u_tex, 0);
    vec2 pos = v_uv * vec2(size) - 0.5;
    vec2 base = floor(pos);
    vec2 f = pos - base;
    float wx[6];
    float wy[6];
    float sum_x = 0.0;
    float sum_y = 0.0;
    for (int i = 0; i < 6; ++i) {
        wx[i] = lanczos3(float(i - 2) - f.x);
        wy[i] = lanczos3(float(i - 2) - f.y);
        sum_x += wx[i];
        sum_y += wy[i];
    }
    ivec2 b = ivec2(base);
    ivec2 max_coord = size - 1;
    vec3 color = vec3(0.0);
    for (int j = 0; j < 6; ++j) {
        vec3 row = vec3(0.0);
        for (int i = 0; i < 6; ++i) {
            ivec2 c = clamp(b + ivec2(i - 2, j - 2), ivec2(0), max_coord);
            row += texelFetch(u_tex, c, 0).rgb * wx[i];
        }
        color += row * wy[j];
    }
    frag_color = vec4(clamp(color / (sum_x * sum_y), 0.0, 1.0), 1.0);
}
""",
}

_PROGRAM_FOR_FILTER = {
    "nearest": "plain",
    "bilinear": "plain",
    "trilinear": "plain",
    "sharp_bilinear": "sharp_bilinear",
    "bicubic": "bicubic",
    "lanczos": "lanczos",
}


class SurfaceState:
    """Video surface state shared by every renderer (widget-based or not)."""

    # While frames arrive this often, overlay changes wait for the next frame.
    OVERLAY_PIGGYBACK_SECONDS = 0.1

    def _init_state(self, slot, overlay):
        self.slot = slot
        self.overlay = overlay
        self.mode = "bilinear"
        self.allow_tearing = False
        self.stats = RenderStats()
        self.source_size = (0, 0)
        self.output_rect = (0, 0, 0, 0)
        self.active_filter = ""
        # Device pixels hidden off-screen (top, bottom, left, right); used by
        # borderless fullscreen, whose window overhangs the monitor edge.
        self.insets = (0, 0, 0, 0)
        self._seq = -1
        self._last_frame_at = 0.0

    def refresh_overlay(self):
        """Make an overlay change visible.

        Presenting just for an overlay change puts an extra, repeated frame
        between two video frames; with v-sync that can push the next real frame
        a whole refresh late. It showed up as a hitch every 0.5 s while the
        info panel was updating. So while video is flowing, the change simply
        rides along with the next frame (at most one frame later).
        """
        if time.perf_counter() - self._last_frame_at > self.OVERLAY_PIGGYBACK_SECONDS:
            self.update()

    def layout_video(self, out_w, out_h):
        """Video rect (x, y, w, h) in device pixels, within the visible area."""
        top, bottom, left, right = self.insets
        src_w, src_h = self.source_size
        x, y, w, h = target_rect(self.mode, src_w, src_h, out_w - left - right, out_h - top - bottom)
        self.output_rect = (x + left, y + top, w, h)
        return self.output_rect

    def _take_frame(self):
        item = self.slot.take(self._seq)
        if item is None:
            return None
        frame, seq, captured_at = item
        if self._seq >= 0 and seq > self._seq + 1:
            self.stats.skipped += seq - self._seq - 1
        self._seq = seq
        self._last_frame_at = time.perf_counter()
        self.stats.frame_presented(captured_at)
        return frame

    def reset_source(self):
        self.slot.clear()
        self.source_size = (0, 0)
        self.update()


class _VideoSurfaceMixin(SurfaceState):
    """Helpers for renderers that are Qt widgets (OpenGL, software)."""

    def _init_surface(self, slot, overlay):
        self._init_state(slot, overlay)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setMinimumSize(160, 90)

    def device_size(self):
        dpr = self.devicePixelRatioF()
        return max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr))

    def _paint_overlay(self):
        if self.overlay.active():
            top, bottom, left, right = self.insets
            dpr = self.devicePixelRatioF()
            visible = self.rect().adjusted(math.ceil(left / dpr), math.ceil(top / dpr),
                                           -math.ceil(right / dpr), -math.ceil(bottom / dpr))
            painter = QPainter(self)
            self.overlay.paint(painter, visible)
            painter.end()


class GLVideoWidget(_VideoSurfaceMixin, QOpenGLWidget):
    renderer_name = "OpenGL"
    gl_failed = Signal(str)

    def __init__(self, slot, overlay, parent=None):
        super().__init__(parent)
        self._init_surface(slot, overlay)
        self._gl = None
        self._texture = None
        self._programs = {}
        self._vao = None
        self._mipmaps_stale = True
        self._failed = False

    def initializeGL(self):
        context = self.context()
        fmt = context.format()
        if (fmt.majorVersion(), fmt.minorVersion()) < (3, 3):
            self._fail(f"OpenGL 3.3 is required, got {fmt.majorVersion()}.{fmt.minorVersion()}")
            return
        self._gl = context.functions()
        for name, source in _FRAGMENT_SHADERS.items():
            program = QOpenGLShaderProgram(self)
            if not (program.addShaderFromSourceCode(QOpenGLShader.Vertex, _VERTEX_SHADER)
                    and program.addShaderFromSourceCode(QOpenGLShader.Fragment, source)
                    and program.link()):
                self._fail(f"Shader '{name}' failed to build: {program.log()}")
                return
            self._programs[name] = program
        self._vao = QOpenGLVertexArrayObject(self)
        self._vao.create()
        context.aboutToBeDestroyed.connect(self._release_gl)

    def _fail(self, reason):
        self._failed = True
        print(f"OpenGL renderer unavailable: {reason}")
        QTimer.singleShot(0, lambda: self.gl_failed.emit(reason))

    def _release_gl(self):
        self.makeCurrent()
        if self._texture is not None:
            self._texture.destroy()
            self._texture = None
        self.doneCurrent()

    def paintGL(self):
        if self._failed:
            return
        gl = self._gl
        out_w, out_h = self.device_size()
        gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        gl.glClear(GL_COLOR_BUFFER_BIT)

        frame = self._take_frame()
        if frame is not None:
            self._upload(frame)

        if self._texture is not None and self.source_size != (0, 0):
            src_w, src_h = self.source_size
            x, y, w, h = self.layout_video(out_w, out_h)
            flt = effective_filter(self.mode, min(w / src_w, h / src_h))
            self.active_filter = flt
            self._set_filter(flt)

            program = self._programs[_PROGRAM_FOR_FILTER[flt]]
            program.bind()
            program.setUniformValue1i(program.uniformLocation("u_tex"), 0)
            location = program.uniformLocation("u_out_size")
            if location >= 0:
                program.setUniformValue(location, float(w), float(h))
            self._texture.bind(0)
            gl.glViewport(x, out_h - y - h, w, h)  # GL's origin is bottom-left
            self._vao.bind()
            gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
            # QPainter (overlays) needs the default VAO and program back.
            self._vao.release()
            program.release()
            gl.glViewport(0, 0, out_w, out_h)

        self._paint_overlay()

    def _upload(self, frame):
        h, w = frame.shape[:2]
        if self._texture is None or self.source_size != (w, h):
            if self._texture is not None:
                self._texture.destroy()
            texture = QOpenGLTexture(QOpenGLTexture.Target2D)
            texture.setFormat(QOpenGLTexture.RGB8_UNorm)
            texture.setSize(w, h)
            texture.setMipLevels(texture.maximumMipLevels())
            texture.allocateStorage(QOpenGLTexture.BGR, QOpenGLTexture.UInt8)
            texture.setWrapMode(QOpenGLTexture.ClampToEdge)
            self._texture = texture
            self.source_size = (w, h)
        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)
        self._texture.bind()
        self._gl.glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        # OpenCV frames are BGR; GL swizzles on upload, so no cvtColor pass.
        self._gl.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, GL_BGR, GL_UNSIGNED_BYTE, frame)
        # Restore the default: QPainter's glyph cache uploads assume it.
        self._gl.glPixelStorei(GL_UNPACK_ALIGNMENT, 4)
        self._mipmaps_stale = True

    def _set_filter(self, flt):
        texture = self._texture
        if flt == "trilinear":
            if self._mipmaps_stale:
                texture.generateMipMaps()
                self._mipmaps_stale = False
            texture.setMinMagFilters(QOpenGLTexture.LinearMipMapLinear, QOpenGLTexture.Linear)
        elif flt in ("bilinear", "sharp_bilinear"):
            texture.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
        else:
            # Nearest, and the texelFetch-based kernels (filtering unused).
            texture.setMinMagFilters(QOpenGLTexture.Nearest, QOpenGLTexture.Nearest)

    def grab_frame_image(self):
        return self.grabFramebuffer()


class SoftwareVideoWidget(_VideoSurfaceMixin, QWidget):
    """QPainter fallback. Bicubic/Lanczos/sharp bilinear become bilinear here."""

    renderer_name = "Software (QPainter)"

    def __init__(self, slot, overlay, parent=None):
        super().__init__(parent)
        self._init_surface(slot, overlay)
        self._frame = None
        self._image = None

    def paintEvent(self, event):
        frame = self._take_frame()
        if frame is not None:
            h, w = frame.shape[:2]
            self._frame = np.ascontiguousarray(frame)  # keeps the QImage's buffer alive
            self._image = QImage(self._frame.data, w, h, 3 * w, QImage.Format_BGR888)
            self.source_size = (w, h)

        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.black)
        if self._image is not None:
            src_w, src_h = self.source_size
            x, y, w, h = self.layout_video(*self.device_size())
            flt = effective_filter(self.mode, min(w / src_w, h / src_h))
            smooth = flt not in ("nearest",)
            self.active_filter = "bilinear" if smooth else "nearest"
            painter.setRenderHint(QPainter.SmoothPixmapTransform, smooth)
            dpr = self.devicePixelRatioF()
            painter.drawImage(QRectF(x / dpr, y / dpr, w / dpr, h / dpr), self._image)
        painter.end()
        self._paint_overlay()

    def grab_frame_image(self):
        return self.grab().toImage()
