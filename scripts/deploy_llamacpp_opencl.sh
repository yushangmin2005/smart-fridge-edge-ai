#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-firecar-pi}"
LLAMA_CPP_TAG="${LLAMA_CPP_TAG:-b9773}"
VLM_REMOTE_DIR="${VLM_REMOTE_DIR:-}"
VLM_OPENCL_ACTIVATE="${VLM_OPENCL_ACTIVATE:-0}"

ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" \
  "LLAMA_CPP_TAG='$LLAMA_CPP_TAG' VLM_REMOTE_DIR='$VLM_REMOTE_DIR' VLM_OPENCL_ACTIVATE='$VLM_OPENCL_ACTIVATE' bash -s" <<'REMOTE'
set -euo pipefail

if [ -z "${VLM_REMOTE_DIR:-}" ]; then
  VLM_REMOTE_DIR="$HOME/vlm-inference"
fi

if [ "$(uname -m)" != "aarch64" ]; then
  echo "OpenCL runtime script is only validated for firecar-pi aarch64." >&2
  exit 2
fi

if ! command -v cmake >/dev/null || ! command -v ninja >/dev/null || ! command -v g++ >/dev/null; then
  echo "Missing build tools: cmake, ninja, and g++ are required." >&2
  exit 3
fi

if [ ! -f /usr/include/CL/cl.h ]; then
  echo "OpenCL headers not found at /usr/include/CL/cl.h." >&2
  exit 4
fi

if ! ldconfig -p 2>/dev/null | grep -q 'libOpenCL\.so'; then
  echo "OpenCL loader library libOpenCL.so was not found." >&2
  exit 5
fi

available_kb="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
if [ "$available_kb" -lt 786432 ]; then
  echo "Not enough free disk under $HOME; need at least 768 MiB for OpenCL source build." >&2
  exit 6
fi

mkdir -p "$VLM_REMOTE_DIR"/{runtime,tmp}

src_archive="$VLM_REMOTE_DIR/tmp/llama.cpp-${LLAMA_CPP_TAG}.tar.gz"
src_dir="$VLM_REMOTE_DIR/tmp/llama.cpp-${LLAMA_CPP_TAG}-opencl"
build_dir="$VLM_REMOTE_DIR/tmp/llama.cpp-${LLAMA_CPP_TAG}-opencl-build"
runtime_dir="$VLM_REMOTE_DIR/runtime/llama-${LLAMA_CPP_TAG}-opencl"

rm -rf "$src_dir" "$build_dir" "$runtime_dir"

curl -fL --retry 3 --connect-timeout 20 \
  -o "$src_archive" \
  "https://github.com/ggml-org/llama.cpp/archive/refs/tags/${LLAMA_CPP_TAG}.tar.gz"

mkdir -p "$src_dir"
tar -xzf "$src_archive" -C "$src_dir" --strip-components=1
rm -f "$src_archive"

# firecar-pi ships CMake 3.16. The server UI asset script only needs the
# newer CMake path when building/downloading the web UI, which is disabled
# for this headless deployment.
sed -i 's/cmake_minimum_required(VERSION 3.18)/cmake_minimum_required(VERSION 3.16)/' \
  "$src_dir/scripts/ui-assets.cmake"

# b9773's OpenCL backend has an unconditional QCOM large-buffer fallback
# that references OpenCL 3.0 symbols even when the target version is lower.
# Mali-T860 on firecar-pi exposes OpenCL 1.2/2.2 headers, so keep that
# fallback out of the compile when targeting OpenCL < 3.0.
perl -0pi -e 's@    if \(err != CL_SUCCESS && backend_ctx->adreno_use_large_buffer\) \{\n        cl_mem_properties props\[\] = \{ 0x41A6 /\* CL_LARGE_BUFFER_QCOM \*/, 1, 0 \};\n        mem = clCreateBufferWithProperties\(backend_ctx->context, props, CL_MEM_READ_WRITE, size, NULL, &err\);\n    \}\n@#if CL_TARGET_OPENCL_VERSION >= 300\n    if (err != CL_SUCCESS && backend_ctx->adreno_use_large_buffer) {\n        cl_mem_properties props[] = { 0x41A6 /* CL_LARGE_BUFFER_QCOM */, 1, 0 };\n        mem = clCreateBufferWithProperties(backend_ctx->context, props, CL_MEM_READ_WRITE, size, NULL, &err);\n    }\n#endif\n@' \
  "$src_dir/ggml/src/ggml-opencl/ggml-opencl.cpp"

cmake -S "$src_dir" -B "$build_dir" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_EXAMPLES=OFF \
  -DLLAMA_BUILD_SERVER=ON \
  -DLLAMA_BUILD_TOOLS=ON \
  -DLLAMA_BUILD_APP=OFF \
  -DLLAMA_BUILD_UI=OFF \
  -DLLAMA_USE_PREBUILT_UI=OFF \
  -DGGML_NATIVE=OFF \
  -DGGML_OPENCL=ON \
  -DGGML_OPENCL_USE_ADRENO_KERNELS=OFF \
  -DGGML_OPENCL_TARGET_VERSION=220

cmake --build "$build_dir" --config Release --target llama-server llama-mtmd-cli -j "${VLM_BUILD_JOBS:-1}"

mkdir -p "$runtime_dir"
cp "$build_dir/bin/llama-server" "$runtime_dir/"
cp "$build_dir/bin/llama-mtmd-cli" "$runtime_dir/"
find "$build_dir" -type f \( -name '*.so' -o -name '*.so.*' \) -exec cp -P {} "$runtime_dir/" \; 2>/dev/null || true
[ -f "$src_dir/LICENSE" ] && cp "$src_dir/LICENSE" "$runtime_dir/LICENSE"

rm -rf "$src_dir" "$build_dir"

if [ "$VLM_OPENCL_ACTIVATE" = "1" ]; then
  ln -sfn "$runtime_dir" "$VLM_REMOTE_DIR/runtime/current"
fi

export LD_LIBRARY_PATH="$runtime_dir:${LD_LIBRARY_PATH:-}"
"$runtime_dir/llama-server" --version
"$runtime_dir/llama-server" --list-devices || true
echo "opencl_runtime=$runtime_dir"
if [ "$VLM_OPENCL_ACTIVATE" = "1" ]; then
  echo "activated=1 current=$(readlink -f "$VLM_REMOTE_DIR/runtime/current")"
else
  echo "activated=0 current=$(readlink -f "$VLM_REMOTE_DIR/runtime/current" 2>/dev/null || true)"
fi
REMOTE
