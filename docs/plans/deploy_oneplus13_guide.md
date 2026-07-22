# Deployment Guide: OnePlus 13 (Adreno 830 GPU + OpenCL Acceleration)

This guide explains the deployment steps, compiler configuration, and driver integration to set up `kindle-butch-gen` with hardware-accelerated LLM translation and OCR on the **OnePlus 13** (Snapdragon 8 Elite / Adreno 830).

---

## 1. Hardware & Driver Architecture

The **Snapdragon 8 Elite** features the **Adreno 830** GPU, which natively supports OpenCL 3.0 and Vulkan 1.3. However, since Termux and the PRoot Ubuntu container run as unprivileged userspace environments, they do not have direct access to physical PCIe or memory mappings of the GPU.

To bridge this gap, we use the following architectural components:

1.  **KGSL Device (`/dev/kgsl`)**: 
    The Kernel Graphics Support Layer (KGSL) is the kernel-space device node through which Adreno drivers communicate. We bind-mount `/dev/kgsl` into the Ubuntu PRoot container so the userspace driver can submit draw/inference commands to the kernel.
2.  **Shared System Libraries**: 
    The GPU vendor driver resides in the Android partition `/vendor/lib64/`. By bind-mounting `/vendor` and `/system` into the PRoot filesystem, the container gains access to the identical `libOpenCL.so` and `libq3d_adreno.so` binaries that Android apps run on.
3.  **OpenCL ICD Loader**: 
    The Installable Client Driver (ICD) loader (`ocl-icd`) directs the OpenCL library client calls to the appropriate vendor-specific driver. Creating `/etc/OpenCL/vendors/adreno.icd` pointing to `/vendor/lib64/libOpenCL.so` tells `ocl-icd` to load the Qualcomm hardware driver.

---

## 2. Compilation and Dependencies

### llama.cpp with Native OpenCL Backend
`llama.cpp` includes a native OpenCL backend optimized specifically for Qualcomm Adreno GPUs. When compiling `llama.cpp` inside Ubuntu:
1.  We install `opencl-headers` and `ocl-icd-opencl-dev`.
2.  We configure CMake with `-DGGML_OPENCL=ON` to build the native OpenCL backend.
3.  CMake will automatically enable `GGML_OPENCL_USE_ADRENO_KERNELS` to compile and run optimized shaders/kernels written specifically for Adreno GPU hardware.
4.  Binaries compiled with native OpenCL offload matrix operations directly to the Adreno GPU when `-ngl 99` (number of layers offloaded) is passed to `llama-server`.

### OCR (Marker) and PyTorch
Marker relies on PyTorch. Since CUDA is not available on Android Adreno hardware:
*   We use PyTorch compiled for **CPU with OpenMP** support.
*   The Oryon CPU cores on Snapdragon 8 Elite are extremely fast for floating-point math, so CPU-based PyTorch inference for layout analysis is highly performant.

---

## 3. Quick Deployment Steps

The deployment script [`deploy.sh`](file:///data/data/com.termux/files/home/kindle-butch-gen/deploy.sh) (formerly `deploy_oneplus13.sh`, evolved in TASK-32 into a self-sufficient one-curl installer for any device, with graceful CPU-only fallback on non-Adreno hardware) automates the entire process. Here is how to run it:

1.  **Run the script in Termux**:
    ```bash
    chmod +x deploy.sh
    ./deploy.sh
    ```
2.  **Verify OpenCL Recognition**:
    Enter the GPU container:
    ```bash
    ~/ubuntu-gpu.sh
    ```
    Inside Ubuntu, run:
    ```bash
    clinfo
    ```
    You should see the **Qualcomm Adreno** GPU listed as a compute device.

3.  **Run Translation Server**:
    Launch the LLM translation model on the GPU (offloading all layers):
    ```bash
    llama-server -m models/hy-mt2/model.gguf -c 2048 --port 8081 -ngl 99
    ```

---

## 4. Troubleshooting & Notes

*   **Permission Denied on `/dev/kgsl`**:
    On some customized Android ROMs, access to `/dev/kgsl` is restricted to certain GIDs. If `clinfo` fails or complains about permissions, check the GID of `/dev/kgsl` on host Android (`ls -l /dev/kgsl`) and add that group ID to your Termux user.
*   **Termux Wake Lock**:
    When running heavy compilations or inference, Android's battery optimizer might put Termux to sleep. Always run `termux-wake-lock` before starting a long run or server process.
*   **Mykyta Speaker Start Silence**:
    *(Note for future custom configurations)*: When using `uk_UA-ukrainian_tts-medium` with Speaker 1 (Mykyta), there may be up to 1-2 seconds of initial silence in generated wav chunks. Speaker 2 (Tetiana) has no such issue and is selected as default.
