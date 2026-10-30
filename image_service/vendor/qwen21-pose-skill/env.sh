# Source this file from Bash before running the bundled CLI tools.
export POSELAB="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export COMFY="$(cd -- "$POSELAB/../../../../.." && pwd)"
export COMFYUI_URL="${COMFYUI_URL:-http://127.0.0.1:8188}"
export COMFYUI_INPUT="${COMFYUI_INPUT:-$COMFY/input}"
export COMFYUI_OUTPUT="${COMFYUI_OUTPUT:-$COMFY/output}"
export VNCCS_POSELIB="${VNCCS_POSELIB:-$POSELAB/../../pose_library}"
