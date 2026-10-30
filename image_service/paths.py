from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
COMFY_ROOT = PLUGIN_ROOT.parents[1]
POSE_LIBRARY = PLUGIN_ROOT / "image_service/pose_library"
POSE_ASSET_ROOT = COMFY_ROOT / "custom_nodes/ComfyUI_VNCCS_Utils/web"
