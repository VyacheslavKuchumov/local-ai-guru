# Collaboration

- Org is the user's primary contact. Reply to the user in the language they use (Russian, English, or German).
- Planner and subordinate agents communicate with one another and with Org in English. User-facing documents may be in Russian.
- Org consolidates the user's ideas and sends coherent task contracts to Planner. Planner owns decomposition, delegation, integration, and verification.
- Planner runs the latest available Sol model with high reasoning. Execution agents run the latest available Luna with medium reasoning; analytical/planning agents run Sol high. Do not silently substitute model families.
- Initial Herdr layout: Org, Planner, R&D with four Luna medium agents. UX/UI and Testing may be provisioned when needed. Add departments and adjust agent counts when justified. Keep Org's tab dedicated to the user conversation.
- Use live Herdr IDs and explicit pane targets; inherited caller IDs may point at another workspace. Preserve user focus for background work.
- Give each writer an explicit file scope. Serialize changes to shared files. Do not duplicate another agent's active task or build.
- Do not launch more agents merely to fill a department. Follow the user's current authorization and chosen isolation mechanism.

# Product constraints

- Personal, non-commercial project (user clarification, 2026-10-28). Commercial licensing is not a selection requirement; check each artifact's actual permitted-use terms.
- Windows target: Ryzen 5 5600X, 16 GB RAM, RTX 3060 with 12 GB VRAM.
- Target user reports Ubuntu 24.04.5 LTS in WSL. Synthetic runner tests pass; pinned llama.cpp b11968 CUDA 12.8 launches and lists the RTX 3060. Model inference, WSL version and isolation remain unverified. Evaluate WSL2-hosted runtime/harness first; WSL alone is not an offline or filesystem boundary. Evidence: docs/research/target-inventory.md.
- Use llama.cpp for local inference. Evaluate open-source Hugging Face GGUF models against the target hardware and actual tasks.
- Expected load: one or at most two people. Start with one loaded model and a bounded inference queue; evaluate concurrency against measured memory and latency.
- Total product disk budget: target 100 GB decimal, including product WSL overhead, models, environments, original knowledge packs, indexes, restore artifacts and update/test headroom; existing Windows OS excluded. Feasibility is unverified. Compare large models sequentially within peak budget; the user now runs target commands manually and supplies output.
- Model selection must include current Qwen and Gemma candidates. Prefer Unsloth GGUF conversions where available; verify base provenance, complete projector footprint and runtime compatibility. Compare quantizations on real tasks; Q8 fit is unverified.
- The installed product must work offline by default. Network enablement requires an explicit user command. No VPN dependency or automatic host VPN changes.
- Model API agents used during development are separate from the offline product; never describe their cloud inference as local.
- Enforce configurable network, filesystem, and tool allowlists outside the model. Identify what is enforced by the harness versus the operating system/container.
- Prepare development environments, dependency caches, documentation, and all runtime assets before offline use. A Docker image alone does not ensure a project's dependencies are available offline.
- Preserve source URL, edition/revision, license, and retrieval date for knowledge packs. Allow direct reading of the source independently of generated answers.
- Do not commit credentials, downloaded models, corpora, indexes, Docker disk images, or caches.
- No large downloads or system-wide changes without task-specific authorization.

# Current ownership

- Org: README.md, AGENTS.md, .gitignore, Git/GitHub publication. Coordinate through Herdr directly; the user removed the economical-orchestration skill on 2026-10-28.
- Planner: ARCHITECTURE.md and PLAN.md for the initial design task.
- R&D agents: idle until a bounded contract assigns their scope.
