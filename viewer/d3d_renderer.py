"""Direct3D 11 renderer.

Presents through a DXGI flip-model swap chain on the main window itself, the
same way games do. In borderless fullscreen that lets Windows hand the frames
straight to the display ("independent flip") instead of compositing them,
which is what G-Sync / FreeSync need, and it removes the compositor's frame of
queueing. When the monitor is in HDR mode the output is HDR10, since Windows
has to composite (colour-convert) any SDR swap chain on an HDR desktop.

Frames arrive as packed 24-bit BGR, which D3D has no texture format for, so
they are uploaded as a single-channel texture three times as wide and turned
into RGBA by a small shader pass on the GPU. Scaling then works exactly like
the OpenGL renderer (same filters, same maths), and overlays are blended in
the same swap chain so they don't break direct presentation.

The D3D11/DXGI interfaces are called through their vtables with ctypes, so
no extra packages are needed. Vtable indices include IUnknown's 3 methods.
"""

import ctypes
import math
import time
from ctypes import (POINTER, Structure, WINFUNCTYPE, byref, c_char_p, c_float, c_int, c_long,
                    c_size_t, c_ubyte, c_uint, c_ulong, c_void_p, sizeof)
from ctypes import wintypes

import numpy as np
from comtypes import GUID
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from .renderer import SurfaceState, effective_filter

# --- constants -------------------------------------------------------------------

DXGI_FORMAT_R10G10B10A2_UNORM = 24
DXGI_FORMAT_R8G8B8A8_UNORM = 28
DXGI_FORMAT_R8_UNORM = 61
DXGI_FORMAT_B8G8R8A8_UNORM = 87
DXGI_USAGE_RENDER_TARGET_OUTPUT = 0x20
DXGI_SCALING_STRETCH = 0
DXGI_SWAP_EFFECT_FLIP_DISCARD = 4
DXGI_ALPHA_MODE_IGNORE = 3
DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING = 2048
DXGI_PRESENT_ALLOW_TEARING = 0x200
DXGI_FEATURE_PRESENT_ALLOW_TEARING = 0
DXGI_MWA_NO_ALT_ENTER = 2
DXGI_COLOR_SPACE_HDR10 = 12               # RGB_FULL_G2084_NONE_P2020
QDC_ONLY_ACTIVE_PATHS = 2
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL = 11
DXGI_ERROR_DEVICE_REMOVED = -2005270523   # 0x887A0005
DXGI_ERROR_DEVICE_RESET = -2005270521     # 0x887A0007

D3D_DRIVER_TYPE_HARDWARE = 1
D3D11_SDK_VERSION = 7
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
D3D11_USAGE_DEFAULT = 0
D3D11_USAGE_IMMUTABLE = 1
D3D11_USAGE_STAGING = 3
D3D11_BIND_CONSTANT_BUFFER = 0x4
D3D11_BIND_SHADER_RESOURCE = 0x8
D3D11_BIND_RENDER_TARGET = 0x20
D3D11_CPU_ACCESS_READ = 0x20000
D3D11_RESOURCE_MISC_GENERATE_MIPS = 0x1
D3D11_MAP_READ = 1
D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP = 5
D3D11_FILTER_POINT = 0x0
D3D11_FILTER_LINEAR = 0x14            # MIN_MAG_LINEAR_MIP_POINT
D3D11_FILTER_TRILINEAR = 0x15         # MIN_MAG_MIP_LINEAR
D3D11_TEXTURE_ADDRESS_CLAMP = 3
D3D11_COMPARISON_NEVER = 1
D3D11_BLEND_ONE = 2
D3D11_BLEND_INV_SRC_ALPHA = 6
D3D11_BLEND_OP_ADD = 1
D3D11_FILL_SOLID = 3
D3D11_CULL_NONE = 1
D3DCOMPILE_OPTIMIZATION_LEVEL3 = 1 << 15

IID_ID3D11Texture2D = GUID("{6F15AAF2-D208-4E89-9AB4-489535D34F9C}")
IID_IDXGIDevice1 = GUID("{77DB970F-6276-48BA-BA28-070143B4392C}")
IID_IDXGIFactory2 = GUID("{50C83A1C-E072-4C48-87B0-3630FA36A6D0}")
IID_IDXGIFactory5 = GUID("{7632E1F5-EE65-4DCA-87FD-84CD75F8838D}")
IID_IDXGISwapChain3 = GUID("{94D99BDB-F1F8-4AB0-B236-7DA0170EDAB1}")
IID_IDXGIOutput6 = GUID("{068346E8-AAEC-4B84-ADD7-137F513F77A1}")

# --- structures -----------------------------------------------------------------


class DXGI_SAMPLE_DESC(Structure):
    _fields_ = [("Count", c_uint), ("Quality", c_uint)]


class DXGI_SWAP_CHAIN_DESC1(Structure):
    _fields_ = [("Width", c_uint), ("Height", c_uint), ("Format", c_uint), ("Stereo", c_int),
                ("SampleDesc", DXGI_SAMPLE_DESC), ("BufferUsage", c_uint), ("BufferCount", c_uint),
                ("Scaling", c_uint), ("SwapEffect", c_uint), ("AlphaMode", c_uint), ("Flags", c_uint)]


class D3D11_TEXTURE2D_DESC(Structure):
    _fields_ = [("Width", c_uint), ("Height", c_uint), ("MipLevels", c_uint), ("ArraySize", c_uint),
                ("Format", c_uint), ("SampleDesc", DXGI_SAMPLE_DESC), ("Usage", c_uint),
                ("BindFlags", c_uint), ("CPUAccessFlags", c_uint), ("MiscFlags", c_uint)]


class D3D11_SUBRESOURCE_DATA(Structure):
    _fields_ = [("pSysMem", c_void_p), ("SysMemPitch", c_uint), ("SysMemSlicePitch", c_uint)]


class D3D11_BUFFER_DESC(Structure):
    _fields_ = [("ByteWidth", c_uint), ("Usage", c_uint), ("BindFlags", c_uint),
                ("CPUAccessFlags", c_uint), ("MiscFlags", c_uint), ("StructureByteStride", c_uint)]


class D3D11_VIEWPORT(Structure):
    _fields_ = [("TopLeftX", c_float), ("TopLeftY", c_float), ("Width", c_float),
                ("Height", c_float), ("MinDepth", c_float), ("MaxDepth", c_float)]


class D3D11_SAMPLER_DESC(Structure):
    _fields_ = [("Filter", c_uint), ("AddressU", c_uint), ("AddressV", c_uint), ("AddressW", c_uint),
                ("MipLODBias", c_float), ("MaxAnisotropy", c_uint), ("ComparisonFunc", c_uint),
                ("BorderColor", c_float * 4), ("MinLOD", c_float), ("MaxLOD", c_float)]


class D3D11_RENDER_TARGET_BLEND_DESC(Structure):
    _fields_ = [("BlendEnable", c_int), ("SrcBlend", c_uint), ("DestBlend", c_uint),
                ("BlendOp", c_uint), ("SrcBlendAlpha", c_uint), ("DestBlendAlpha", c_uint),
                ("BlendOpAlpha", c_uint), ("RenderTargetWriteMask", c_ubyte)]


class D3D11_BLEND_DESC(Structure):
    _fields_ = [("AlphaToCoverageEnable", c_int), ("IndependentBlendEnable", c_int),
                ("RenderTarget", D3D11_RENDER_TARGET_BLEND_DESC * 8)]


class D3D11_RASTERIZER_DESC(Structure):
    _fields_ = [("FillMode", c_uint), ("CullMode", c_uint), ("FrontCounterClockwise", c_int),
                ("DepthBias", c_int), ("DepthBiasClamp", c_float), ("SlopeScaledDepthBias", c_float),
                ("DepthClipEnable", c_int), ("ScissorEnable", c_int), ("MultisampleEnable", c_int),
                ("AntialiasedLineEnable", c_int)]


class D3D11_MAPPED_SUBRESOURCE(Structure):
    _fields_ = [("pData", c_void_p), ("RowPitch", c_uint), ("DepthPitch", c_uint)]


# --- minimal COM access ------------------------------------------------------------


class Com:
    """A COM interface pointer; methods are called by vtable index."""

    _prototypes = {}

    def __init__(self, address, name="COM object"):
        if not address:
            raise OSError(f"{name}: null interface pointer")
        self.ptr = address
        self.name = name

    def call(self, index, restype, argtypes, *args):
        key = (index, restype, tuple(argtypes))
        proto = self._prototypes.get(key)
        if proto is None:
            proto = self._prototypes[key] = WINFUNCTYPE(restype, c_void_p, *argtypes)
        vtable = ctypes.cast(c_void_p(self.ptr), POINTER(POINTER(c_void_p)))[0]
        return proto(vtable[index])(self.ptr, *args)

    def hr(self, index, argtypes, *args, what=""):
        result = self.call(index, c_long, argtypes, *args)
        if result < 0:
            raise OSError(result, f"{self.name}{'.' + what if what else ''} failed "
                                  f"(HRESULT 0x{result & 0xFFFFFFFF:08X})")
        return result

    def query(self, iid, name):
        out = c_void_p()
        self.hr(0, [POINTER(GUID), POINTER(c_void_p)], byref(iid), byref(out), what="QueryInterface")
        return Com(out.value, name)

    def release(self):
        if self.ptr:
            self.call(2, c_ulong, [])
            self.ptr = None

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass


def _out_call(obj, index, argtypes_before, *args, name, what):
    """Call a method whose last parameter is an interface out-pointer."""
    out = c_void_p()
    obj.hr(index, list(argtypes_before) + [POINTER(c_void_p)], *args, byref(out), what=what)
    return Com(out.value, name)


# --- shaders -------------------------------------------------------------------

_HLSL_COMMON = """
cbuffer Params : register(b0) { float2 out_size; float2 tex_size; };
SamplerState samp : register(s0);
struct VSOut { float4 pos : SV_Position; float2 uv : TEXCOORD0; };
"""

_HLSL = {
    "vs": ("vs_4_0", """
VSOut main(uint id : SV_VertexID) {
    // Full-viewport quad from the vertex index, drawn as a 4-vertex strip.
    float2 p = float2(id & 1, id >> 1);
    VSOut o;
    o.pos = float4(p * 2.0 - 1.0, 0.0, 1.0);
    o.uv = float2(p.x, 1.0 - p.y);
    return o;
}"""),
    # Packed BGR bytes (a R8 texture 3x as wide) -> RGBA.
    "unpack": ("ps_4_0", """
Texture2D<float> raw : register(t0);
float4 main(VSOut i) : SV_Target {
    int2 p = int2(i.pos.xy);
    int x = p.x * 3;
    float b = raw.Load(int3(x, p.y, 0));
    float g = raw.Load(int3(x + 1, p.y, 0));
    float r = raw.Load(int3(x + 2, p.y, 0));
    return float4(r, g, b, 1.0);
}"""),
    "plain": ("ps_4_0", """
Texture2D tex : register(t0);
float4 main(VSOut i) : SV_Target { return float4(tex.Sample(samp, i.uv).rgb, 1.0); }"""),
    # Premultiplied overlay image; out_size.x carries its opacity.
    "overlay": ("ps_4_0", """
Texture2D tex : register(t0);
float4 main(VSOut i) : SV_Target { return tex.Sample(samp, i.uv) * out_size.x; }"""),
    # SDR canvas -> HDR10 (PQ, BT.2020); out_size.x is the SDR white level in
    # nits. Uses the piecewise sRGB curve, as Windows does for SDR content.
    "hdr10": ("ps_4_0", """
Texture2D canvas : register(t0);
float3 pq(float3 nits) {
    float3 y = pow(saturate(nits / 10000.0), 0.1593017578125);
    return pow((0.8359375 + 18.8515625 * y) / (1.0 + 18.6875 * y), 78.84375);
}
float4 main(VSOut i) : SV_Target {
    float3 c = canvas.Load(int3(int2(i.pos.xy), 0)).rgb;
    float3 lin = c <= 0.04045 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4);
    float3x3 bt709_to_bt2020 = { 0.6274040, 0.3292820, 0.0433136,
                                 0.0690970, 0.9195400, 0.0113612,
                                 0.0163916, 0.0880132, 0.8955950 };
    return float4(pq(mul(bt709_to_bt2020, lin) * out_size.x), 1.0);
}"""),
    "sharp_bilinear": ("ps_4_0", """
Texture2D tex : register(t0);
float4 main(VSOut i) : SV_Target {
    float2 prescale = max(floor(out_size / tex_size), 1.0);
    float2 texel = i.uv * tex_size;
    float2 texel_floor = floor(texel);
    float2 center_dist = frac(texel) - 0.5;
    float2 region = 0.5 - 0.5 / prescale;
    float2 f = (center_dist - clamp(center_dist, -region, region)) * prescale + 0.5;
    return float4(tex.Sample(samp, (texel_floor + f) / tex_size).rgb, 1.0);
}"""),
    "bicubic": ("ps_4_0", """
Texture2D tex : register(t0);
float4 weights(float t) {
    return float4(t * (-0.5 + t * (1.0 - 0.5 * t)),
                  1.0 + t * t * (-2.5 + 1.5 * t),
                  t * (0.5 + t * (2.0 - 1.5 * t)),
                  t * t * (-0.5 + 0.5 * t));
}
float4 main(VSOut i) : SV_Target {
    float2 pos = i.uv * tex_size - 0.5;
    float2 base = floor(pos);
    float2 f = pos - base;
    float4 wx = weights(f.x);
    float4 wy = weights(f.y);
    int2 b = int2(base);
    int2 max_coord = int2(tex_size) - 1;
    float3 color = 0.0;
    [unroll] for (int j = 0; j < 4; ++j) {
        float3 row = 0.0;
        [unroll] for (int k = 0; k < 4; ++k) {
            int2 c = clamp(b + int2(k - 1, j - 1), int2(0, 0), max_coord);
            row += tex.Load(int3(c, 0)).rgb * wx[k];
        }
        color += row * wy[j];
    }
    return float4(saturate(color), 1.0);
}"""),
    "lanczos": ("ps_4_0", """
Texture2D tex : register(t0);
static const float PI = 3.14159265358979;
float lanczos3(float x) {
    x = abs(x);
    if (x < 1e-5) return 1.0;
    if (x >= 3.0) return 0.0;
    float px = PI * x;
    return 3.0 * sin(px) * sin(px / 3.0) / (px * px);
}
float4 main(VSOut i) : SV_Target {
    float2 pos = i.uv * tex_size - 0.5;
    float2 base = floor(pos);
    float2 f = pos - base;
    float wx[6];
    float wy[6];
    float sum_x = 0.0;
    float sum_y = 0.0;
    [unroll] for (int n = 0; n < 6; ++n) {
        wx[n] = lanczos3(float(n - 2) - f.x);
        wy[n] = lanczos3(float(n - 2) - f.y);
        sum_x += wx[n];
        sum_y += wy[n];
    }
    int2 b = int2(base);
    int2 max_coord = int2(tex_size) - 1;
    float3 color = 0.0;
    [unroll] for (int j = 0; j < 6; ++j) {
        float3 row = 0.0;
        [unroll] for (int k = 0; k < 6; ++k) {
            int2 c = clamp(b + int2(k - 2, j - 2), int2(0, 0), max_coord);
            row += tex.Load(int3(c, 0)).rgb * wx[k];
        }
        color += row * wy[j];
    }
    return float4(saturate(color / (sum_x * sum_y)), 1.0);
}"""),
}

_SHADER_FOR_FILTER = {"nearest": "plain", "bilinear": "plain", "trilinear": "plain",
                      "sharp_bilinear": "sharp_bilinear", "bicubic": "bicubic", "lanczos": "lanczos"}
_SAMPLER_FOR_FILTER = {"nearest": "point", "bilinear": "linear", "trilinear": "trilinear",
                       "sharp_bilinear": "linear", "bicubic": "point", "lanczos": "point"}


def _compile(name, target, source):
    compiler = ctypes.WinDLL("d3dcompiler_47.dll")
    compiler.D3DCompile.restype = c_long
    compiler.D3DCompile.argtypes = [c_char_p, c_size_t, c_char_p, c_void_p, c_void_p, c_char_p,
                                    c_char_p, c_uint, c_uint, POINTER(c_void_p), POINTER(c_void_p)]
    text = (_HLSL_COMMON + source).encode()
    code, errors = c_void_p(), c_void_p()
    hr = compiler.D3DCompile(text, len(text), name.encode(), None, None, b"main", target.encode(),
                             D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, byref(code), byref(errors))
    if hr < 0:
        message = ""
        if errors.value:
            blob = Com(errors.value, "error blob")
            message = ctypes.string_at(blob.call(3, c_void_p, []), blob.call(4, c_size_t, []))
            message = message.decode(errors="replace")
        raise OSError(hr, f"Shader '{name}' failed to compile: {message}")
    blob = Com(code.value, "shader blob")
    return ctypes.string_at(blob.call(3, c_void_p, []), blob.call(4, c_size_t, []))


# --- HDR output ------------------------------------------------------------------

class DISPLAYCONFIG_PATH_INFO(Structure):
    _fields_ = [("sourceAdapterId", ctypes.c_longlong), ("sourceId", c_uint), ("sourceModeIdx", c_uint),
                ("sourceStatus", c_uint), ("targetAdapterId", ctypes.c_longlong), ("targetId", c_uint),
                ("targetModeIdx", c_uint), ("outputTechnology", c_uint), ("rotation", c_uint),
                ("scaling", c_uint), ("refreshNumerator", c_uint), ("refreshDenominator", c_uint),
                ("scanLineOrdering", c_uint), ("targetAvailable", c_int), ("targetStatus", c_uint),
                ("flags", c_uint)]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(Structure):
    _fields_ = [("type", c_uint), ("size", c_uint), ("adapterId", ctypes.c_longlong), ("id", c_uint)]


class DISPLAYCONFIG_SOURCE_DEVICE_NAME(Structure):
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER), ("viewGdiDeviceName", ctypes.c_wchar * 32)]


class DISPLAYCONFIG_SDR_WHITE_LEVEL(Structure):
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER), ("SDRWhiteLevel", c_ulong)]


class DXGI_OUTPUT_DESC1(Structure):
    _fields_ = [("DeviceName", ctypes.c_wchar * 32), ("DesktopCoordinates", wintypes.RECT),
                ("AttachedToDesktop", c_int), ("Rotation", c_uint), ("Monitor", c_void_p),
                ("BitsPerColor", c_uint), ("ColorSpace", c_uint), ("RedPrimary", c_float * 2),
                ("GreenPrimary", c_float * 2), ("BluePrimary", c_float * 2), ("WhitePoint", c_float * 2),
                ("MinLuminance", c_float), ("MaxLuminance", c_float), ("MaxFullFrameLuminance", c_float)]


def sdr_white_nits(gdi_device_name):
    """Windows' "SDR content brightness" for a monitor, in nits (None if unknown).

    SDR content shown on an HDR desktop is mapped to this brightness; using the
    same value makes the video look exactly as it would if Windows converted it.
    """
    user32 = ctypes.windll.user32
    paths_n, modes_n = c_uint(), c_uint()
    if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, byref(paths_n), byref(modes_n)):
        return None
    paths = (DISPLAYCONFIG_PATH_INFO * paths_n.value)()
    modes = (ctypes.c_byte * (64 * max(1, modes_n.value)))()
    if user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, byref(paths_n), paths, byref(modes_n), modes, None):
        return None
    for path in paths[:paths_n.value]:
        name = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
        name.header = DISPLAYCONFIG_DEVICE_INFO_HEADER(DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME,
                                                       sizeof(name), path.sourceAdapterId, path.sourceId)
        if user32.DisplayConfigGetDeviceInfo(byref(name)) or name.viewGdiDeviceName != gdi_device_name:
            continue
        level = DISPLAYCONFIG_SDR_WHITE_LEVEL()
        level.header = DISPLAYCONFIG_DEVICE_INFO_HEADER(DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL,
                                                        sizeof(level), path.targetAdapterId, path.targetId)
        if user32.DisplayConfigGetDeviceInfo(byref(level)) == 0 and level.SDRWhiteLevel:
            return level.SDRWhiteLevel / 1000.0 * 80.0
    return None


# --- the renderer ----------------------------------------------------------------

MAX_FAILURES = 3            # render failures within MAX_FAILURE_WINDOW seconds ...
MAX_FAILURE_WINDOW = 30.0   # ... before giving up on Direct3D for this session


class _D3D:
    """Device and pipeline state, plus the swap chain of the window it presents to."""

    def __init__(self):
        d3d11 = ctypes.WinDLL("d3d11.dll")
        d3d11.D3D11CreateDevice.restype = c_long
        device, context = c_void_p(), c_void_p()
        feature_level = c_uint()
        hr = d3d11.D3D11CreateDevice(None, D3D_DRIVER_TYPE_HARDWARE, None,
                                     D3D11_CREATE_DEVICE_BGRA_SUPPORT, None, 0, D3D11_SDK_VERSION,
                                     byref(device), byref(feature_level), byref(context))
        if hr < 0:
            raise OSError(hr, f"D3D11CreateDevice failed (0x{hr & 0xFFFFFFFF:08X})")
        self.device = Com(device.value, "ID3D11Device")
        self.context = Com(context.value, "ID3D11DeviceContext")

        dxgi_device = self.device.query(IID_IDXGIDevice1, "IDXGIDevice1")
        # Queue at most one frame: a new frame is shown as soon as possible
        # rather than waiting behind older ones.
        dxgi_device.hr(12, [c_uint], 1, what="SetMaximumFrameLatency")
        adapter = _out_call(dxgi_device, 7, [], name="IDXGIAdapter", what="GetAdapter")
        factory = c_void_p()
        adapter.hr(6, [POINTER(GUID), POINTER(c_void_p)], byref(IID_IDXGIFactory2), byref(factory),
                   what="GetParent")
        self.factory = Com(factory.value, "IDXGIFactory2")
        self.tearing_supported = self._check_tearing()

        self._shaders = {}
        for name, (target, source) in _HLSL.items():
            code = _compile(name, target, source)
            index = 12 if target.startswith("vs") else 15   # CreateVertexShader / CreatePixelShader
            self._shaders[name] = _out_call(self.device, index, [c_void_p, c_size_t, c_void_p],
                                            code, len(code), None, name=f"shader {name}",
                                            what="CreateShader")

        self._samplers = {name: self._sampler(flt) for name, flt in
                          (("point", D3D11_FILTER_POINT), ("linear", D3D11_FILTER_LINEAR),
                           ("trilinear", D3D11_FILTER_TRILINEAR))}
        blend = D3D11_BLEND_DESC()
        target = blend.RenderTarget[0]
        target.BlendEnable = 1
        target.SrcBlend = target.SrcBlendAlpha = D3D11_BLEND_ONE        # premultiplied alpha
        target.DestBlend = target.DestBlendAlpha = D3D11_BLEND_INV_SRC_ALPHA
        target.BlendOp = target.BlendOpAlpha = D3D11_BLEND_OP_ADD
        target.RenderTargetWriteMask = 0xF
        self._blend = _out_call(self.device, 20, [POINTER(D3D11_BLEND_DESC)], byref(blend),
                                name="blend state", what="CreateBlendState")
        raster = D3D11_RASTERIZER_DESC(FillMode=D3D11_FILL_SOLID, CullMode=D3D11_CULL_NONE,
                                       DepthClipEnable=1)
        self._raster = _out_call(self.device, 22, [POINTER(D3D11_RASTERIZER_DESC)], byref(raster),
                                 name="rasterizer state", what="CreateRasterizerState")
        cb = D3D11_BUFFER_DESC(ByteWidth=16, Usage=D3D11_USAGE_DEFAULT, BindFlags=D3D11_BIND_CONSTANT_BUFFER)
        self._params = _out_call(self.device, 3, [POINTER(D3D11_BUFFER_DESC), c_void_p], byref(cb), None,
                                 name="constant buffer", what="CreateBuffer")
        self._params_value = None

        self.src_size = (0, 0)
        self._raw = self._raw_srv = self._rgba = self._rgba_srv = self._rgba_rtv = None
        self._mips_stale = True
        self._overlays = {}   # layer key -> (texture, srv)

        self.swapchain = None
        self._rtv = self._backbuffer = None
        self._canvas = self._canvas_rtv = self._canvas_srv = None
        self.hdr = False
        self.white_nits = 0.0
        self.monitor = ""
        self.size = (0, 0)
        self.hwnd = None

    def _check_tearing(self):
        try:
            factory5 = self.factory.query(IID_IDXGIFactory5, "IDXGIFactory5")
        except OSError:
            return False
        allowed = c_int(0)
        try:
            factory5.hr(28, [c_uint, c_void_p, c_uint], DXGI_FEATURE_PRESENT_ALLOW_TEARING,
                        byref(allowed), sizeof(allowed), what="CheckFeatureSupport")
        except OSError:
            return False
        return bool(allowed.value)

    def _sampler(self, flt):
        desc = D3D11_SAMPLER_DESC(Filter=flt, AddressU=D3D11_TEXTURE_ADDRESS_CLAMP,
                                  AddressV=D3D11_TEXTURE_ADDRESS_CLAMP,
                                  AddressW=D3D11_TEXTURE_ADDRESS_CLAMP,
                                  ComparisonFunc=D3D11_COMPARISON_NEVER, MaxLOD=3.4e38)
        return _out_call(self.device, 23, [POINTER(D3D11_SAMPLER_DESC)], byref(desc),
                         name="sampler", what="CreateSamplerState")

    # --- swap chain ----------------------------------------------------------------

    def attach(self, hwnd, hdr=None):
        """(Re)create the swap chain for a window.

        hdr=None: pick HDR10 output if the window's monitor is in HDR mode.
        """
        self._release_swapchain()
        self.hwnd = hwnd
        if hdr is None:
            hdr = self._monitor_is_hdr(hwnd)
        width, height = client_size(hwnd)
        desc = DXGI_SWAP_CHAIN_DESC1(
            Width=width, Height=height,
            Format=DXGI_FORMAT_R10G10B10A2_UNORM if hdr else DXGI_FORMAT_B8G8R8A8_UNORM,
            SampleDesc=DXGI_SAMPLE_DESC(1, 0), BufferUsage=DXGI_USAGE_RENDER_TARGET_OUTPUT,
            BufferCount=2, Scaling=DXGI_SCALING_STRETCH, SwapEffect=DXGI_SWAP_EFFECT_FLIP_DISCARD,
            AlphaMode=DXGI_ALPHA_MODE_IGNORE,
            Flags=DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING if self.tearing_supported else 0)
        self._swap_flags = desc.Flags
        self.swapchain = _out_call(
            self.factory, 15, [c_void_p, c_void_p, POINTER(DXGI_SWAP_CHAIN_DESC1), c_void_p, c_void_p],
            self.device.ptr, hwnd, byref(desc), None, None,
            name="IDXGISwapChain1", what="CreateSwapChainForHwnd")
        # Alt+Enter must not switch to exclusive fullscreen behind Qt's back.
        self.factory.hr(8, [c_void_p, c_uint], hwnd, DXGI_MWA_NO_ALT_ENTER, what="MakeWindowAssociation")
        self.hdr = False
        if hdr:
            try:
                chain3 = self.swapchain.query(IID_IDXGISwapChain3, "IDXGISwapChain3")
                chain3.hr(38, [c_uint], DXGI_COLOR_SPACE_HDR10, what="SetColorSpace1")
                self.hdr = True
            except OSError as e:
                print(f"HDR output unavailable, using SDR: {e}")
                return self.attach(hwnd, hdr=False)
        self.size = (width, height)
        self._make_targets()
        return self.hdr

    def _monitor_is_hdr(self, hwnd):
        """True if the monitor showing the window is in HDR mode; also reads
        its SDR white level."""
        self.white_nits = 0.0
        monitor = ctypes.windll.user32.MonitorFromWindow(c_void_p(hwnd), 2)   # MONITOR_DEFAULTTONEAREST
        try:
            adapter = _out_call(self.device.query(IID_IDXGIDevice1, "IDXGIDevice1"), 7, [],
                                name="IDXGIAdapter", what="GetAdapter")
            index = 0
            while True:
                out = c_void_p()
                if adapter.call(7, c_long, [c_uint, POINTER(c_void_p)], index, byref(out)) < 0:
                    return False
                output = Com(out.value, "IDXGIOutput").query(IID_IDXGIOutput6, "IDXGIOutput6")
                desc = DXGI_OUTPUT_DESC1()
                output.hr(27, [POINTER(DXGI_OUTPUT_DESC1)], byref(desc), what="GetDesc1")
                if desc.Monitor == monitor:
                    self.monitor = desc.DeviceName
                    if desc.ColorSpace != DXGI_COLOR_SPACE_HDR10:
                        return False
                    self.white_nits = sdr_white_nits(desc.DeviceName) or 200.0
                    return True
                index += 1
        except OSError as e:
            print(f"Could not query the monitor's colour mode: {e}")
            return False

    def output_changed(self):
        """True if the window's monitor switched HDR on/off (or it moved to a
        monitor in the other mode), so the swap chain needs recreating."""
        if self.hwnd is None:
            return False
        white = self.white_nits
        now_hdr = self._monitor_is_hdr(self.hwnd)
        changed = now_hdr != self.hdr or (now_hdr and abs(self.white_nits - white) > 0.5)
        if not changed:
            self.white_nits = white
        return changed

    def _make_targets(self):
        buffer = c_void_p()
        self.swapchain.hr(9, [c_uint, POINTER(GUID), POINTER(c_void_p)], 0,
                          byref(IID_ID3D11Texture2D), byref(buffer), what="GetBuffer")
        self._backbuffer = Com(buffer.value, "back buffer")
        self._rtv = _out_call(self.device, 9, [c_void_p, c_void_p], self._backbuffer.ptr, None,
                              name="back buffer RTV", what="CreateRenderTargetView")
        self._canvas = self._canvas_rtv = self._canvas_srv = None
        if self.hdr:
            # HDR: compose the SDR picture on an ordinary 8-bit canvas, then
            # convert it to HDR10 in one final pass.
            width, height = self.size
            desc = D3D11_TEXTURE2D_DESC(Width=width, Height=height, MipLevels=1, ArraySize=1,
                                        Format=DXGI_FORMAT_R8G8B8A8_UNORM, SampleDesc=DXGI_SAMPLE_DESC(1, 0),
                                        Usage=D3D11_USAGE_DEFAULT,
                                        BindFlags=D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET)
            self._canvas = self._texture(desc, None, "SDR canvas")
            self._canvas_srv = self._srv(self._canvas)
            self._canvas_rtv = _out_call(self.device, 9, [c_void_p, c_void_p], self._canvas.ptr, None,
                                         name="canvas RTV", what="CreateRenderTargetView")

    def resize(self):
        width, height = client_size(self.hwnd)
        if (width, height) == self.size or self.swapchain is None:
            return
        self._unbind_targets()
        self._rtv = self._backbuffer = None
        self._canvas = self._canvas_rtv = self._canvas_srv = None
        self.swapchain.hr(13, [c_uint, c_uint, c_uint, c_uint, c_uint], 0, width, height, 0,
                          self._swap_flags, what="ResizeBuffers")
        self.size = (width, height)
        self._make_targets()

    def _release_swapchain(self):
        if self.swapchain is not None:
            self._unbind_targets()
        self._rtv = self._backbuffer = None
        self._canvas = self._canvas_rtv = self._canvas_srv = None
        self.swapchain = None

    def _unbind_targets(self):
        self.context.call(33, None, [c_uint, c_void_p, c_void_p], 0, None, None)

    def close(self):
        """Release everything, swap chain first, while the window still exists."""
        self._release_swapchain()
        self._overlays.clear()
        self._raw = self._raw_srv = self._rgba = self._rgba_srv = self._rgba_rtv = None
        self._shaders.clear()
        self._samplers.clear()
        self._blend = self._raster = self._params = None
        self.context.call(110, None, [])   # ClearState: drop all remaining bindings
        self.context.call(111, None, [])   # Flush
        self.context = self.device = self.factory = None

    # --- frames --------------------------------------------------------------------

    def upload(self, frame):
        height, width = frame.shape[:2]
        if (width, height) != self.src_size:
            self._create_source_textures(width, height)
        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)
        self.context.call(48, None, [c_void_p, c_uint, c_void_p, c_void_p, c_uint, c_uint],
                          self._raw.ptr, 0, None, frame.ctypes.data, width * 3, 0)
        # Unpack BGR bytes -> RGBA on the GPU.
        self._set_target(self._rgba_rtv, 0, 0, width, height)
        self._draw("unpack", self._raw_srv, "point")
        self._unbind_targets()
        self._mips_stale = True

    def _create_source_textures(self, width, height):
        self._raw = self._raw_srv = self._rgba = self._rgba_srv = self._rgba_rtv = None
        raw = D3D11_TEXTURE2D_DESC(Width=width * 3, Height=height, MipLevels=1, ArraySize=1,
                                   Format=DXGI_FORMAT_R8_UNORM, SampleDesc=DXGI_SAMPLE_DESC(1, 0),
                                   Usage=D3D11_USAGE_DEFAULT, BindFlags=D3D11_BIND_SHADER_RESOURCE)
        self._raw = self._texture(raw, None, "raw frame")
        self._raw_srv = self._srv(self._raw)
        rgba = D3D11_TEXTURE2D_DESC(Width=width, Height=height, MipLevels=0, ArraySize=1,
                                    Format=DXGI_FORMAT_R8G8B8A8_UNORM, SampleDesc=DXGI_SAMPLE_DESC(1, 0),
                                    Usage=D3D11_USAGE_DEFAULT,
                                    BindFlags=D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET,
                                    MiscFlags=D3D11_RESOURCE_MISC_GENERATE_MIPS)
        self._rgba = self._texture(rgba, None, "frame")
        self._rgba_srv = self._srv(self._rgba)
        self._rgba_rtv = _out_call(self.device, 9, [c_void_p, c_void_p], self._rgba.ptr, None,
                                   name="frame RTV", what="CreateRenderTargetView")
        self.src_size = (width, height)

    def _texture(self, desc, initial, name):
        return _out_call(self.device, 5, [POINTER(D3D11_TEXTURE2D_DESC), c_void_p], byref(desc),
                         initial, name=name, what="CreateTexture2D")

    def _srv(self, texture):
        return _out_call(self.device, 7, [c_void_p, c_void_p], texture.ptr, None,
                         name="SRV", what="CreateShaderResourceView")

    # --- drawing -------------------------------------------------------------------

    def _set_target(self, rtv, x, y, w, h):
        views = (c_void_p * 1)(rtv.ptr)
        self.context.call(33, None, [c_uint, POINTER(c_void_p), c_void_p], 1, views, None)
        viewport = D3D11_VIEWPORT(x, y, w, h, 0.0, 1.0)
        self.context.call(44, None, [c_uint, POINTER(D3D11_VIEWPORT)], 1, byref(viewport))

    def _set_params(self, a, b, c, d):
        value = (float(a), float(b), float(c), float(d))
        if value != self._params_value:
            data = (c_float * 4)(*value)
            self.context.call(48, None, [c_void_p, c_uint, c_void_p, c_void_p, c_uint, c_uint],
                              self._params.ptr, 0, None, data, 0, 0)
            self._params_value = value

    def _draw(self, shader, srv, sampler):
        ctx = self.context
        ctx.call(24, None, [c_uint], D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP)
        ctx.call(43, None, [c_void_p], self._raster.ptr)
        ctx.call(11, None, [c_void_p, c_void_p, c_uint], self._shaders["vs"].ptr, None, 0)
        ctx.call(9, None, [c_void_p, c_void_p, c_uint], self._shaders[shader].ptr, None, 0)
        slots = [c_uint, c_uint, POINTER(c_void_p)]
        ctx.call(8, None, slots, 0, 1, (c_void_p * 1)(srv.ptr))                        # PSSetShaderResources
        ctx.call(10, None, slots, 0, 1, (c_void_p * 1)(self._samplers[sampler].ptr))   # PSSetSamplers
        ctx.call(16, None, slots, 0, 1, (c_void_p * 1)(self._params.ptr))              # PSSetConstantBuffers
        ctx.call(13, None, [c_uint, c_uint], 4, 0)                                     # Draw
        # Unbind the input so the texture can be written again next frame.
        ctx.call(8, None, slots, 0, 1, (c_void_p * 1)(None))

    def render(self, video_rect, flt, layers):
        """Draw the current frame (without presenting).

        video_rect is (x, y, w, h) in back-buffer pixels; layers is a list of
        (key, QImage, x, y, opacity) overlay images in back-buffer pixels.
        """
        ctx = self.context
        width, height = self.size
        target = self._canvas_rtv if self.hdr else self._rtv
        black = (c_float * 4)(0.0, 0.0, 0.0, 1.0)
        ctx.call(50, None, [c_void_p, POINTER(c_float * 4)], target.ptr, byref(black))
        x, y, w, h = video_rect
        if self._rgba_srv is not None and w > 0 and h > 0:
            if flt == "trilinear" and self._mips_stale:
                ctx.call(54, None, [c_void_p], self._rgba_srv.ptr)   # GenerateMips
                self._mips_stale = False
            self._set_params(w, h, *self.src_size)
            self._set_target(target, x, y, w, h)
            self._draw(_SHADER_FOR_FILTER[flt], self._rgba_srv, _SAMPLER_FOR_FILTER[flt])

        used = set()
        if layers:
            ctx.call(35, None, [c_void_p, c_void_p, c_uint], self._blend.ptr, None, 0xFFFFFFFF)
            for key, image, lx, ly, opacity in layers:
                srv = self._overlay_srv(key, image)
                used.add(key)
                self._set_params(opacity, 0, 0, 0)
                self._set_target(target, lx, ly, image.width(), image.height())
                self._draw("overlay", srv, "point")
            ctx.call(35, None, [c_void_p, c_void_p, c_uint], None, None, 0xFFFFFFFF)
        for key in [k for k in self._overlays if k not in used]:
            del self._overlays[key]   # drop textures of boxes no longer shown

        if self.hdr:
            self._set_params(self.white_nits, 0, 0, 0)
            self._set_target(self._rtv, 0, 0, width, height)
            self._draw("hdr10", self._canvas_srv, "point")
        self._set_target(self._rtv, 0, 0, width, height)

    def _overlay_srv(self, key, image):
        entry = self._overlays.get(key)
        if entry is None:
            pixels = ctypes.create_string_buffer(bytes(image.constBits()),
                                                 image.bytesPerLine() * image.height())
            data = D3D11_SUBRESOURCE_DATA(ctypes.addressof(pixels), image.bytesPerLine(), 0)
            desc = D3D11_TEXTURE2D_DESC(Width=image.width(), Height=image.height(), MipLevels=1,
                                        ArraySize=1, Format=DXGI_FORMAT_B8G8R8A8_UNORM,
                                        SampleDesc=DXGI_SAMPLE_DESC(1, 0), Usage=D3D11_USAGE_IMMUTABLE,
                                        BindFlags=D3D11_BIND_SHADER_RESOURCE)
            texture = self._texture(desc, ctypes.addressof(data), "overlay")
            entry = self._overlays[key] = (texture, self._srv(texture))
        return entry[1]

    def present(self, allow_tearing):
        """Returns False if the device was lost and must be recreated."""
        if allow_tearing and self.tearing_supported:
            sync, flags = 0, DXGI_PRESENT_ALLOW_TEARING
        else:
            sync, flags = 1, 0
        result = self.swapchain.call(8, c_long, [c_uint, c_uint], sync, flags)
        if result in (DXGI_ERROR_DEVICE_REMOVED, DXGI_ERROR_DEVICE_RESET):
            return False
        if result < 0:
            raise OSError(result, f"Present failed (HRESULT 0x{result & 0xFFFFFFFF:08X})")
        return True

    def read_back_buffer(self):
        """Copy the rendered picture to a QImage (for tests and screenshots).
        In HDR mode this is the SDR canvas, before the HDR10 conversion."""
        width, height = self.size
        desc = D3D11_TEXTURE2D_DESC(Width=width, Height=height, MipLevels=1, ArraySize=1,
                                    Format=DXGI_FORMAT_R8G8B8A8_UNORM if self.hdr else DXGI_FORMAT_B8G8R8A8_UNORM,
                                    SampleDesc=DXGI_SAMPLE_DESC(1, 0),
                                    Usage=D3D11_USAGE_STAGING, CPUAccessFlags=D3D11_CPU_ACCESS_READ)
        staging = self._texture(desc, None, "staging")
        source = self._canvas if self.hdr else self._backbuffer
        self.context.call(47, None, [c_void_p, c_void_p], staging.ptr, source.ptr)
        mapped = D3D11_MAPPED_SUBRESOURCE()
        self.context.hr(14, [c_void_p, c_uint, c_uint, c_uint, POINTER(D3D11_MAPPED_SUBRESOURCE)],
                        staging.ptr, 0, D3D11_MAP_READ, 0, byref(mapped), what="Map")
        try:
            data = ctypes.string_at(mapped.pData, mapped.RowPitch * height)
        finally:
            self.context.call(15, None, [c_void_p, c_uint], staging.ptr, 0)
        array = np.frombuffer(data, np.uint8).reshape(height, mapped.RowPitch)[:, :width * 4]
        fmt = QImage.Format_RGBA8888 if self.hdr else QImage.Format_ARGB32
        return QImage(np.ascontiguousarray(array).data, width, height, width * 4, fmt).copy()


def client_size(hwnd):
    rect = wintypes.RECT()
    ctypes.windll.user32.GetClientRect(c_void_p(hwnd), byref(rect))
    return max(1, rect.right - rect.left), max(1, rect.bottom - rect.top)


class D3DSurface(QObject, SurfaceState):
    """Direct3D renderer that presents into its host window's own client area.

    The swap chain lives on the top-level window itself, like a game's, rather
    than on a child window: display drivers (G-Sync, their indicator) look at
    the window's own presentation. The host must be a native window that Qt
    doesn't paint (see MainWindow.use_native_surface) and forwards its paint,
    resize and window-handle events here.
    """

    renderer_name = "Direct3D 11"
    init_failed = Signal(str)

    def __init__(self, host, slot, overlay):
        super().__init__(host)
        self._init_state(slot, overlay)
        self.host = host
        self._d3d = _D3D()   # raises if Direct3D 11 is unavailable
        self._attached = False
        self._failed = False
        self._failures = []       # times of recent render failures
        self._retry_at = 0.0      # after a failure, don't rebuild before this
        print(f"Direct3D 11 renderer ready (tearing/VRR presents "
              f"{'supported' if self._d3d.tearing_supported else 'not supported'})")

    # --- things the rest of the app asks of any renderer ------------------------------

    @property
    def tearing_supported(self):
        return bool(self._d3d and self._d3d.tearing_supported)

    @property
    def hdr(self):
        return bool(self._d3d and self._d3d.hdr)

    @property
    def white_nits(self):
        return self._d3d.white_nits if self._d3d else 0.0

    def device_size(self):
        if self._d3d is not None and self._attached:
            return self._d3d.size
        dpr = self.host.devicePixelRatioF()
        return max(1, round(self.host.width() * dpr)), max(1, round(self.host.height() * dpr))

    def update(self):
        self.host.update()

    def repaint(self):
        self.render()

    def grab_frame_image(self):
        self.render(present=False)
        if self._d3d is None or not self._attached:
            return QImage()
        image = self._d3d.read_back_buffer()
        self._d3d.present(self.allow_tearing)
        return image

    # --- events forwarded by the host window ------------------------------------------

    def window_handle_changed(self):
        """Qt reports a (possible) new native window, e.g. after a window flag
        change. It usually keeps the same window and reports twice, so only
        rebuild the swap chain if the handle really changed: needless swap
        chain churn is what tools that hook Present (frame limiters, overlays)
        cope with worst."""
        if self._d3d is not None and self._attached and int(self.host.winId()) != self._d3d.hwnd:
            self._attach()

    def check_output(self):
        """Recreate the swap chain if HDR was switched on/off or the window
        moved to a monitor in the other mode. Cheap; called periodically."""
        if self._d3d is not None and self._attached and self._d3d.output_changed():
            print("Monitor colour mode changed; recreating the swap chain")
            self._attach()
            self.update()

    def _attach(self):
        try:
            hdr = self._d3d.attach(int(self.host.winId()))
            self._attached = True
            mode = f"HDR10 output, SDR white {self._d3d.white_nits:.0f} nits" if hdr else "SDR output"
            print(f"Direct3D swap chain {self._d3d.size[0]}x{self._d3d.size[1]} on {self._d3d.monitor or 'monitor'}: {mode}")
        except OSError as e:
            self._fail(f"Could not create the swap chain: {e}")

    def _fail(self, reason):
        self.failure_reason = reason
        print(f"Direct3D 11 renderer failed: {reason}")
        self._failed = True
        self._attached = False
        self.init_failed.emit(reason)

    def render(self, present=True):
        if self._failed or not self.host.isVisible():
            return
        if time.monotonic() < self._retry_at:
            # Waiting before rebuilding the device. Still collect the frame:
            # the capture thread only signals the next one once this one has
            # been taken, so skipping it would stop all further paints.
            self._take_frame()
            return
        if not self._attached:
            self._attach()
            if not self._attached:
                return
        d3d = self._d3d
        try:
            d3d.resize()
            frame = self._take_frame()
            if frame is not None:
                d3d.upload(frame)
                self.source_size = d3d.src_size
            layers = self._overlay_layers()
            if self.source_size != (0, 0) and d3d.src_size != (0, 0):
                src_w, src_h = self.source_size
                x, y, w, h = self.layout_video(*d3d.size)
                flt = effective_filter(self.mode, min(w / src_w, h / src_h))
                self.active_filter = flt
                d3d.render((x, y, w, h), flt, layers)
            else:
                d3d.render((0, 0, 0, 0), "nearest", layers)
            if present and not d3d.present(self.allow_tearing):
                raise OSError("device lost")
        except OSError as e:
            # Device lost (driver update/crash, GPU reset): rebuild everything,
            # but not in a tight loop. A crash inside Present that keeps
            # coming back (seen with a frame limiter hooked into the app)
            # would otherwise freeze the window: give up on Direct3D instead.
            now = time.monotonic()
            self._failures = [t for t in self._failures if now - t < MAX_FAILURE_WINDOW] + [now]
            print(f"Direct3D render error ({e}); failure {len(self._failures)} in the last "
                  f"{MAX_FAILURE_WINDOW:.0f} s")
            try:
                self._d3d.close()
            except Exception:
                pass
            self._d3d = None
            self._attached = False
            self.source_size = (0, 0)
            if len(self._failures) >= MAX_FAILURES:
                self._fail("Direct3D kept failing. If an overlay or frame limiter (e.g. FramePacer, "
                           "RTSS) is hooked into this app, exclude it or use the OpenGL renderer.")
                return
            self._retry_at = now + 1.0
            try:
                self._d3d = _D3D()
            except OSError as e2:
                self._fail(str(e2))

    def _overlay_layers(self):
        if not self.overlay.active():
            return []
        dpr = self.host.devicePixelRatioF()
        top, bottom, left, right = self.insets
        visible = self.host.rect().adjusted(math.ceil(left / dpr), math.ceil(top / dpr),
                                            -math.ceil(right / dpr), -math.ceil(bottom / dpr))
        return [(layer.key, layer.image, round(layer.top_left.x() * dpr), round(layer.top_left.y() * dpr),
                 layer.opacity)
                for layer in self.overlay.layers(visible, dpr)]

    def shutdown(self):
        """Release the swap chain and device (before the host window goes away)."""
        d3d, self._d3d = self._d3d, None
        self._failed = True   # never re-create once shut down
        self._attached = False
        if d3d is not None:
            d3d.close()
