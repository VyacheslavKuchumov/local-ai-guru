# Offline development environments: Windows, WSL2, Docker, and build caches

**Research date:** 2026-10-28. URLs below are primary documentation. Remote requirements and documented tool behavior were checked on this date; this repository has not been run on the target Windows machine. All storage numbers are planning estimates, not measurements.

## What must be prepared

Target: Windows on Ryzen 5 5600X, 16 GB RAM, RTX 3060 12 GB. Keep the host installation limited to Windows, virtualization/WSL prerequisites, and the chosen container engine. Use Linux containers under WSL2 for reproducible Linux builds. A Linux image is not a complete offline development kit: it does not inherently contain future project dependencies, package registries, compiler downloads, build plugins, toolchains, documentation, or persistent state.

Docker Desktop's current Windows guidance lists supported serviced Windows editions/builds, WSL 2.1.5 or later for the WSL backend, the WSL2 feature, BIOS/UEFI virtualization and at least 8 GB RAM. The target has 16 GB, but concurrent Windows, WSL, Docker and builds still share that budget; allocate conservatively and validate peak memory locally. Docker documents Linux-container workflows through WSL integration. WSL2 requires both the Windows Subsystem for Linux and Virtual Machine Platform features. Installing/updating WSL and Docker Desktop, enabling virtualization, drivers, and any Windows features requires a connected preparation phase and may require administrator access. Do not assume those installers or updates are available offline.

For each supported sample project, prepare and retain:

- source tree, lockfiles, pinned tool versions, and build configuration;
- compiler/runtime and native build prerequisites (Rust toolchain/target, Node runtime, JDK, C/C++ toolchain or system libraries where required);
- every registry artifact, Git dependency, plugin, wrapper distribution and compiler bridge needed by the locked graph;
- native binaries for the exact OS, architecture, libc/ABI and toolchain combination;
- offline package indexes/metadata where the package manager needs them, plus documentation that can be read locally;
- generated build outputs only if explicitly needed to avoid rebuilding; they are target- and tool-version-specific and should not be treated as dependency packs.

Use a connected staging environment to resolve and fetch the dependency graph, then export a manifest (versions, source URLs, checksums/licenses where applicable, target triples, retrieval date) with the cache pack. Test using a **fresh disposable project and fresh empty cache locations** after the staging network is physically disconnected. A successful build from a warm developer cache does not prove portability.

## Offline behavior by example stack

| Stack | Prepared inputs and offline command shape | Common cold-restore failures |
| --- | --- | --- |
| Rust / Cargo | Preserve `Cargo.lock`; prefetch for the exact target or vendor crates and Git dependencies (`cargo vendor` plus source replacement config). Verify with `cargo build --frozen --target <triple>` and `cargo test --frozen --target <triple>`; `--frozen` combines locked and offline behavior. Include rustup toolchain components and target standard library. | Missing registry index/metadata, Git source, target stdlib, linker/native system package, build-script download, or mismatched lockfile. Cargo notes offline resolution is restricted to locally available packages. |
| Vue / Node | Preserve `package-lock.json`, `.npmrc` options, exact Node/npm versions and a complete npm cache pack or prepared `node_modules` for the target. Try `npm ci --offline --cache <empty-restored-cache>` on a fresh copy. Include native optional packages for Windows/Linux as appropriate. | Lockfile alone identifies versions but does not provide tarballs. npm lifecycle scripts may fetch tools/assets, optional native packages can be platform-specific, and omitted peer-dependency flags can change install shape. `npm ci` deletes existing `node_modules`; cache misses fail offline. |
| Scala / sbt / JDK / ZIO | Preserve `build.sbt`, `project/` plugin definitions, lock/revision metadata where used, pinned sbt/JDK and wrapper launcher. Stage every Maven/Ivy artifact and metadata, sbt plugin, compiler bridge and wrapper distribution; test with sbt offline mode (e.g. `sbt -offline test`) after relocating to an empty cache. | sbt plugins resolve separately from application dependencies; wrapper may need to download sbt; missing POM/Ivy metadata or compiler bridge can trigger resolution. JDK build and native classifiers are OS/architecture-specific. ZIO project dependencies follow this same Scala/sbt preparation requirement. |

Cargo sources: [Cargo FAQ: offline](https://doc.rust-lang.org/cargo/faq.html), [cargo vendor](https://doc.rust-lang.org/cargo/commands/cargo-vendor.html), [Cargo dependencies and lockfile](https://doc.rust-lang.org/cargo/guide/dependencies.html). npm sources: [npm ci](https://docs.npmjs.com/cli/commands/npm-ci/), [npm cache](https://docs.npmjs.com/cli/cache/), [npm config: offline](https://docs.npmjs.com/cli/using-npm/config/#offline). Scala sources: [sbt offline mode](https://www.scala-sbt.org/1.x/docs/Offline.html), [sbt launcher](https://www.scala-sbt.org/1.x/docs/Launcher.html), [sbt dependency management](https://www.scala-sbt.org/1.x/docs/Library-Dependencies.html). These examples are proposed acceptance targets, not tested results.

## Export, import, persistence, and target differences

- **WSL distribution:** Microsoft documents `wsl --export <Distro> <file>` to snapshot a distribution (tar by default; WSL2 also supports VHDX) and `wsl --import <Name> <InstallLocation> <file> --version 2` to restore it. This captures that distro's filesystem, but does not install Windows features, Docker Desktop, host GPU drivers, firmware virtualization, or necessarily Docker Desktop's separate engine data distribution. Export/import can require substantial temporary free space. Treat export/import as same-architecture WSL restore; validate kernel/WSL version, UID/default-user setup, systemd/configuration and GPU interop after import.
- **Docker images:** `docker image save` creates an archive of requested images including parent layers; `docker load` restores those images to an engine. It does not archive named volumes, bind-mounted host files, container writable layers as an independently reusable data backup, build caches, or the engine installation. Export the exact `linux/amd64` images for this target, not a different platform variant. Record digests and verify loaded image IDs.
- **Volumes and caches:** Docker named volumes persist outside container lifecycle and are separate from images. Back them up independently (Docker documents tar-through-helper-container). Bind-mounted project/cache folders must be backed up as ordinary files. Do not copy live WSL/Docker virtual disk files as a substitute for a consistent export. Define exactly which state is required: package caches may be disposable if included in an external dependency pack; user data and indexes need their own backup and restore contract.
- **Windows vs Linux:** Windows host tools and Linux container tools have different executable formats, path rules, line endings, case sensitivity, symlinks, permissions, native dependencies and package artifacts. A Windows npm native module or Rust target is not a Linux artifact. Use separate cache packs keyed by OS, CPU architecture, ABI/libc, and toolchain version. WSL2 is a Linux kernel environment but remains hosted by Windows; Docker Desktop and WSL upgrades/driver integration are external prerequisites.
- **Cold vs warm restore:** A warm restore starts with populated registries/build caches and can hide missing artifacts. Cold acceptance starts from a clean disposable WSL distro or clean engine, empty user package caches and a copied fresh project, with host networking physically disconnected (not merely a proxy or DNS failure). A second run with preserved caches measures the warm path. Report both elapsed time, bytes restored, network attempts, and build/test outcomes.

Sources: [Microsoft WSL basic commands: export/import](https://learn.microsoft.com/windows/wsl/basic-commands), [Microsoft WSL FAQ: backup and transfer](https://learn.microsoft.com/windows/wsl/faq), [Docker Desktop WSL2 backend](https://docs.docker.com/desktop/features/wsl/), [Docker Desktop Windows install requirements](https://docs.docker.com/desktop/setup/install/windows-install/), [Docker image save/load](https://docs.docker.com/reference/cli/docker/image/save/), [Docker volumes and backup/restore](https://docs.docker.com/engine/storage/volumes/), [Docker storage concepts](https://docs.docker.com/engine/storage/). Retrieved/checked 2026-10-28.

## Storage budget (planning ranges, not measured)

Ranges below are deliberately broad. They depend on selected base images, installed compiler versions, project graphs, compression, duplicate artifacts, target-native packages and build configuration. They exclude model weights, knowledge corpora/indexes and user datasets.

| Component | Initial planning range | Counting rule |
| --- | ---: | --- |
| Windows engine/app plus WSL kernel/features and base distro | 5–15 GB installed disk | Separate host/WSL footprint; Docker Desktop storage location and distro VHDX growth need measurement. |
| Linux development images, expanded unique layers | 8–25 GB | Count de-duplicated layers once in engine storage, even when shared by several tags/images. Include build-stage images only if retained. |
| Compressed image archives for transfer | 3–12 GB | Archive size is not the expanded size; layers may compress differently. Keep transfer archive separate from imported engine layers until archive cleanup. |
| Offline dependency packs (Cargo + npm + Maven/Ivy/sbt + toolchain installers/docs) | 5–20 GB | Conservative planning range for several modest sample projects, not a full mirror. Measure actual graph; caches overlap with artifacts already embedded in images unless deliberately externalized. |
| Build outputs and per-project caches | 5–20 GB | `target/`, `node_modules`, Scala compiler/build caches and generated outputs vary widely. Keep outside image layers where practical and identify if disposable. |
| Update/restore coexistence and free-space reserve | 10–20 GB free | Temporary archive + extracted/imported state and side-by-side update can coexist. Do not count the reserve as steady-state consumed data; require the budget before restore/update. |

Do not sum every row blindly: image archives coexist temporarily with expanded layers; cache artifacts may duplicate image contents; WSL distro and Docker engine disk can be separate VHDX files; filesystem sparse allocation and Docker shared layers make apparent size differ from unique physical usage. A practical first-pass steady working footprint is roughly **25–60 GB**, plus a separate **10–20 GB free reserve** during import/update. This is a planning envelope only; report actual logical and allocated sizes separately.

## Evidence-producing checks (proposed; not run here)

1. Record Windows edition/build, CPU/RAM, BIOS virtualization status, WSL version/kernel, distro version and mode (`wsl --version`, `wsl --status`, `wsl -l -v`); record Docker Desktop and engine versions, selected backend, image-store setting and configured disk location.
2. On a clean, connected staging machine, run each sample's exact build/test once; export lockfiles, resolved dependency inventory, tool versions, native target identifiers, and cache-pack byte sizes. Inspect scripts and plugin/wrapper declarations for hidden network fetches.
3. Archive the WSL distro and Docker image set separately; record compressed archive sizes and checksums. Import into a clean disposable environment, confirm images/digests, and independently restore one named-volume backup and bind-mounted test fixture.
4. Disconnect external networking or enforce an OS/container boundary that denies external egress. Use fresh Rust/Cargo, npm, sbt/Ivy/Coursier caches and fresh project copies. Run the table's build/test commands; capture stdout/stderr and process/network-attempt evidence. Require zero successful external egress. Separately flag unintended attempts at non-permitted external egress, even when blocked or satisfied by a cache. Permit documented local loopback communication (for example, HTTP test servers bound to `localhost`/`127.0.0.1`), and distinguish it from external egress. This is a proposed check and has not been run.
5. Repeat with warm caches. Compare cold/warm time, peak RAM, free disk before/after, allocated WSL/Docker disk growth, image archive size versus imported unique layer usage, and build-output growth. Repeat after a toolchain update to quantify temporary coexistence space.
6. Run a restore on the intended Windows edition with the physical network absent from first launch through successful tests. Verify required docs open locally, then test a cold reboot and volume persistence. Keep Windows-native and Linux-native test results distinct.

## Failure modes to plan for

- Offline mode can only use artifacts/index metadata already present; a lockfile pins versions but is not a cache.
- Package lifecycle/build scripts may attempt downloads outside the package manager's own offline controls.
- Private registries, expiring credentials, Git submodules, Git dependencies, sbt plugins, wrapper distributions, rustup components or native tool downloads may be missed during staging.
- Platform-specific optional dependencies, CPU targets, libc/ABI mismatch, or host path/permission semantics can invalidate a cache pack.
- Image archive imports do not restore persistent application state; WSL export does not recreate Windows-side prerequisites or Docker's separately managed backend data.
- Staging with warm caches, enabled networking, or shared machine caches can yield false success; a cold disposable test is mandatory.
- Low disk space can cause failed WSL import, Docker load/build, package extraction, or Windows updates when archives and expanded state coexist.
- With 16 GB RAM, parallel builds or large dependency compilation may compete with Windows and inference workloads; actual memory ceilings and swap behavior require target measurements.

## Unresolved questions for the design owner

- Which Windows edition/build and Docker Desktop install mode/backend are intended, and is Docker Desktop's licensing acceptable for the target deployment?
- Are Docker Desktop/WSL and toolchain updates expected to happen during connected staging only, and what exact versions will be pinned?
- Which sample repositories and representative locked dependency graphs should define the offline acceptance pack, including native packages and documentation?
- Is the target allowed to store archives/caches on an external drive, and what restore-time and free-disk limits should be accepted?
- Which user-owned state (named volumes, indexes, project outputs) must survive restore, and what is the required backup cadence?
