# Profilers: setup, permissions, pitfalls

`measure profile` picks a profiler from the command (`--lang auto`) and always writes `PREFIX.folded` (collapsed stacks: `frame;frame;leaf count`). When a profiler is missing or blocked, measure prints the fix from this page and exits 1. It never falls back to guessing.

| Language | Mechanism | Weight unit |
|---|---|---|
| Python | `py-spy record --format raw --subprocesses --function` | samples |
| Native (C, C++, Rust, anything else) | `perf record -g` + `perf script` | samples |
| Node | `NODE_OPTIONS=--cpu-prof` | µs |
| Go | `go test ... -cpuprofile` + `go tool pprof -traces` | µs |
| JVM and others | export collapsed stacks, then `--from FILE` | as exported |

## py-spy

Install:
```
pip install py-spy          # or: pipx install py-spy / uv tool install py-spy
```
- **Containers.** py-spy reads the target's memory and needs `CAP_SYS_PTRACE`: `docker run --cap-add SYS_PTRACE ...`. In Kubernetes, add `SYS_PTRACE` to `securityContext.capabilities.add`.
- **Linux ptrace scope.** With `kernel.yama.ptrace_scope` at 2 or 3, py-spy fails even on its own children. Fix: `sudo sysctl -w kernel.yama.ptrace_scope=1`.
- **macOS.** py-spy needs root: run measure with `sudo`. System Integrity Protection blocks profiling the system Python; use a Homebrew, pyenv or uv Python.
- **`--native`** (measure flag, passed through) includes C/C++/Cython frames, for numpy, pandas, torch and similar. Without it, time inside C extensions is attributed to the Python line that called them.
- **`--subprocesses`** is always on, so `uv run`, `pytest` and multiprocessing workers are sampled. measure strips PIDs from the `process "..."` root frames so that before/after profiles line up.
- **`--function`** is always on: frames are labeled `function (file:first_line)`, so a function is one row in `hotspots` instead of one row per line number.
- py-spy can exit with status 1 after a successful run. measure records the workload's real exit status itself and only reports exit 4 when the workload failed.

## perf

Install:
```
sudo apt install linux-tools-common linux-tools-$(uname -r)   # Debian/Ubuntu
sudo dnf install perf                                          # Fedora
sudo pacman -S perf                                            # Arch
```
- **`kernel.perf_event_paranoid`.** At 2, perf can only sample user space. At 3 or higher (some distros), unprivileged perf is blocked entirely. Fix:
  ```
  sudo sysctl -w kernel.perf_event_paranoid=1
  echo 'kernel.perf_event_paranoid=1' | sudo tee /etc/sysctl.d/99-perf.conf   # persist
  ```
- **Frame pointers vs DWARF.** The default `--callgraph fp` is cheap but needs frame pointers; without them stacks are truncated to one or two frames. Either rebuild with frame pointers or use `--callgraph dwarf` (bigger `perf.data`, slower `perf script`).
  - Rust: `RUSTFLAGS="-C force-frame-pointers=yes" cargo build --release`
  - C/C++: `-O2 -fno-omit-frame-pointer` (also `-mno-omit-leaf-frame-pointer` on GCC)
- **Symbols in release builds.** Profile the optimized build, but keep symbols so frames have names:
  ```toml
  # Cargo.toml
  [profile.release]
  debug = true
  ```
  C/C++: add `-g` alongside `-O2`. Stripped binaries show `[unknown]` frames; measure keeps them so their share stays visible.
- **macOS:** there is no perf. Native profiling is unsupported in measure v1. Export collapsed stacks from another tool (for example Instruments plus a stackcollapse script) and use `measure profile --from FILE`.
- **VMs and containers:** hardware counters may be missing, so perf falls back to `cpu-clock`. That is fine for sampling.

## Go

- Profile benchmarks, not ad-hoc binaries: `measure profile -- go test -run '^$' -bench BenchmarkName ./pkg`. measure appends `-cpuprofile PREFIX.pprof` and reads it with `go tool pprof -traces`.
- `-cpuprofile` works with **exactly one package**: `./...` fails. Name the package.
- Use `-benchtime 2s` (or more) so the profile has enough samples.
- Plain binaries (`go run`, `./server`) are profiled through perf, because Go has no environment-variable switch for CPU profiling. Build with the default toolchain; Go keeps frame pointers on amd64 and arm64.
- `go tool pprof` runs with `CGO_ENABLED=0`, so machines without a C compiler can still read profiles.
- Inlined frames are labeled `(inline)` by pprof; measure strips the suffix so each function is one row.

## Node

- measure sets `NODE_OPTIONS="--cpu-prof --cpu-prof-dir=PREFIX.cpuprof.d"` and collapses the largest `.cpuprofile` written. This needs Node 12 or later.
- `NODE_OPTIONS` is inherited by child processes, so `npm run`, `npx` and worker processes are profiled too; each writes its own file, and the largest one is usually the workload. Collapse a different one with `--from PREFIX.cpuprof.d/<file>.cpuprofile`.
- Frames are `function file:line`; anonymous functions show as `(anonymous)`. `(garbage collector)` and `(program)` (native/VM time) are kept. `(idle)` is dropped.
- For the inspector or Chrome DevTools: record there, save the `.cpuprofile`, then `measure profile --from file.cpuprofile -o PREFIX`.
- Benchmark with `NODE_ENV=production`.

## JVM

Use async-profiler and export collapsed stacks:
```
asprof -d 30 -e cpu -o collapsed -f /tmp/app.collapsed <pid>
# or as an agent:
java -agentpath:/path/to/libasyncProfiler.so=start,event=cpu,file=/tmp/app.collapsed,collapsed -jar app.jar
measure profile --from /tmp/app.collapsed -o .measure/prof-base-1
```
Warm the JIT before measuring: give the compare a `--warmup` that covers JIT compilation, or benchmark through JMH and `--parse` its score.

## ML and GPU workloads

- CPU profilers do not see GPU kernels. GPU time appears as synchronization waits in the CPU profile (`cudaStreamSynchronize`, `torch.cuda.synchronize`, `.item()`, `.cpu()`), which say *that* the CPU waited, not *why*.
- Compare on the program's own throughput line instead of wall time:
  ```
  measure compare --a-cwd .measure/base --b-cwd . --parse 'tok/s: ([\d.]+)' --higher-better --until-conclusive -- python3 bench.py
  ```
- Inside the program, call `torch.cuda.synchronize()` before reading the clock on each side of the timed region. CUDA launches are asynchronous, so without it you measure launch time, not compute time.
- Exclude the first iterations (CUDA context, cuDNN autotune, compilation) from the printed metric, or the warmup dominates.
- Pin the GPU clocks if you can (`nvidia-smi -lgc`), and check the GPU is otherwise idle (`nvidia-smi`).
- GPU kernel profilers (nsys, ncu) are out of scope for measure v1.

## Workload hygiene for every language

- Benchmark the optimized build (Iron rule 8): `--release`, `-O2`, `NODE_ENV=production`, no debugger, no coverage, no `PYTHONDEVMODE`.
- Profile and compare the same command with the same input file (absolute path).
- Runs under 200 ms are dominated by process startup: lengthen the input, or print an internal metric and use `--parse`.
