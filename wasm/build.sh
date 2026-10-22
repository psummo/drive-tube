#!/bin/bash
# Ricompila il decoder H.264 in WebAssembly e lo copia in drivetube/web/h264.wasm.
# Serve solo clang/lld (>= 15), cmake, ninja, make, git e python3: niente emscripten.
# Uso: wasm/build.sh   (dalla cartella drive-tube; lavora in wasm/.build)
set -e
R=$(cd "$(dirname "$0")/.." && pwd)
W=$R/wasm/.build
mkdir -p "$W" && cd "$W"
CLANG_RES=$(clang -print-resource-dir)
LLVM_VER=$(clang -dumpversion)

# 1) funzioni di supporto del compilatore per wasm32 (compiler-rt builtins)
if [ ! -f rt/libclang_rt.builtins-wasm32.a ]; then
  [ -d llvm ] || git clone -q --depth 1 --filter=blob:none --sparse --branch "llvmorg-$LLVM_VER" https://github.com/llvm/llvm-project.git llvm
  (cd llvm && git sparse-checkout set compiler-rt/lib/builtins)
  mkdir -p rt && cd rt
  for f in ../llvm/compiler-rt/lib/builtins/*.c; do
    n=$(basename "$f" .c)
    case $n in enable_execute_stack|emutls|gcc_personality_v0|clear_cache|atomic*|os_version_check|cpu_model|trampoline_setup|eprintf|crtbegin|*xf*|*xc3|trunc*bf2) continue;; esac
    clang --target=wasm32-wasi -O2 -ffreestanding -fno-builtin -DCRT_HAS_128BIT -c "$f" -o "$n.o"
  done
  llvm-ar rcs libclang_rt.builtins-wasm32.a ./*.o
  cd ..
fi
# clang cerca le builtins nella sua cartella di risorse
for d in wasi wasip1; do
  [ -f "$CLANG_RES/lib/$d/libclang_rt.builtins-wasm32.a" ] || sudo install -D rt/libclang_rt.builtins-wasm32.a "$CLANG_RES/lib/$d/libclang_rt.builtins-wasm32.a"
done

# 2) libreria C per WebAssembly (wasi-libc)
if [ ! -f sysroot/lib/wasm32-wasip1/libc.a ]; then
  [ -d wasi-libc ] || git clone -q --depth 1 https://github.com/WebAssembly/wasi-libc.git
  cmake -S wasi-libc -B wasi-libc/build -G Ninja -DCMAKE_C_COMPILER=clang -DCMAKE_AR="$(command -v llvm-ar)" \
    -DCMAKE_NM="$(command -v llvm-nm)" -DCMAKE_RANLIB="$(command -v llvm-ranlib)" -DTARGET_TRIPLE=wasm32-wasip1 \
    -DMALLOC=dlmalloc -DSETJMP=OFF -DBUILD_TESTS=OFF -DBUILD_SHARED=OFF \
    -DBUILTINS_LIB="$W/rt/libclang_rt.builtins-wasm32.a" -DCMAKE_INSTALL_PREFIX="$W/sysroot"
  ninja -C wasi-libc/build install
fi

# 3) FFmpeg con il solo decoder e parser H.264
CC="clang --target=wasm32-wasip1 --sysroot=$W/sysroot"
if [ ! -f ff/lib/libavcodec.a ]; then
  [ -d ffmpeg ] || git clone -q --depth 1 --branch n7.1 https://github.com/FFmpeg/FFmpeg.git ffmpeg
  rm -rf ffbuild && mkdir ffbuild && cd ffbuild
  ../ffmpeg/configure --prefix="$W/ff" --enable-cross-compile --target-os=none --arch=wasm32 \
    --cc="$CC" --ld="$CC" --ar=llvm-ar --ranlib=llvm-ranlib --nm=llvm-nm \
    --extra-cflags="-O3 -msimd128 -D_WASI_EMULATED_SIGNAL" \
    --disable-everything --enable-decoder=h264 --enable-parser=h264 \
    --disable-programs --disable-doc --disable-network --disable-pthreads --disable-w32threads --disable-os2threads \
    --disable-avdevice --disable-avformat --disable-swresample --disable-swscale --disable-avfilter \
    --disable-asm --disable-inline-asm --disable-x86asm --disable-debug --disable-runtime-cpudetect --disable-autodetect
  make -j"$(nproc)" && make install
  cd ..
fi

# 4) wrapper + link
$CC -mexec-model=reactor -O3 -msimd128 "$R/wasm/h264dec.c" -I ff/include -L ff/lib -lavcodec -lavutil -lm \
  -lwasi-emulated-process-clocks -lwasi-emulated-signal \
  -Wl,--strip-all -Wl,--gc-sections -Wl,-z,stack-size=1048576 -o h264.wasm
ls -l h264.wasm

# 5) copio il decoder tra i file del frontend, servito al browser come /h264.wasm
cp h264.wasm "$R/drivetube/web/h264.wasm"
echo "copiato in $R/drivetube/web/h264.wasm"
