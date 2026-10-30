# Local AI MCP Tools — Agent-Facing Tool Descriptions

## Purpose

This MCP server exposes a local language model as a low-cost auxiliary worker for a primary reasoning agent.

The local model is intended to reduce context size, perform high-volume preprocessing, and handle tasks where approximate or intermediate reasoning is sufficient. It is **not** intended to replace the primary reasoning model for final judgments, subtle semantic interpretation, security-critical conclusions, or high-stakes decisions.

### Core Delegation Principle

Use these tools when the task can be decomposed into a **cheap, parallelizable, high-volume, or information-compressing subtask**.

Prefer the primary model when the task requires:

- final judgment or diagnosis;
- subtle semantic interpretation;
- long-range reasoning across many independent facts;
- security-critical conclusions;
- correctness guarantees;
- adversarial reasoning;
- deciding whether the local model's own conclusion is trustworthy.

The local model's output should normally be treated as **evidence, candidates, or an intermediate representation**, not as authoritative truth.

---

# 1. `summarize`

## Description

Summarize a document, text block, command output, conversation, log, source file, or other textual material into a compact representation while preserving the information most relevant to the requested objective.

This tool is primarily a **context reduction tool**. Use it when the primary agent would benefit from understanding the overall content without consuming the full original input in its context window.

The summary should preserve important facts, relationships, warnings, anomalies, identifiers, and quantitative information when they are relevant to the requested objective. It should remove repetition, boilerplate, and low-value detail.

## When to Use

Use `summarize` when:

- the input is large relative to the amount of information the primary agent actually needs;
- the content is repetitive or verbose;
- the agent needs an overview before deciding what to inspect next;
- a command produces a very large amount of output;
- a long document needs to be reduced before deeper analysis;
- multiple independent inputs need compact summaries before comparison.

Typical examples:

- summarize a large `dmesg` output;
- summarize a long build log;
- summarize a source file before deciding whether it is relevant;
- summarize a lengthy issue discussion;
- summarize a large JSON response while preserving important fields;
- summarize each file in a directory before selecting files for deep analysis.

## Do Not Use When

Do not use `summarize` as the sole basis for:

- a final technical diagnosis;
- a security vulnerability determination;
- legal, financial, medical, or other high-stakes conclusions;
- interpreting subtle semantics where omitted context could change the answer;
- extracting exact values when every value matters;
- replacing the original source when precise wording or exact ordering is important.

If the primary agent needs exact evidence, inspect the original input after using the summary.

## Recommended Output Behavior

The result should be concise but information-dense. Preserve:

1. the main subject and purpose;
2. important facts and measurements;
3. significant events in chronological order when applicable;
4. anomalies and errors;
5. identifiers such as file paths, function names, line numbers, error codes, object IDs, and timestamps when useful;
6. unresolved questions;
7. explicit uncertainty when the source itself is ambiguous.

Do not invent facts that are not present in the input.

## Agent Guidance

Use this tool early when the input is large and the next action depends on understanding its general structure.

If the summary identifies suspicious or important regions, use the original source or a more targeted tool for verification.

---

# 2. `classify`

## Description

Classify one or more input items into categories supplied by the caller. The classification should be consistent, concise, and focused on the requested classification criteria.

This tool is intended for **fast routing, filtering, triage, labeling, and prioritization**, not for authoritative interpretation.

The caller should provide explicit categories whenever possible. When categories are supplied, do not invent additional categories unless the tool contract explicitly allows an `unknown` or `other` class.

## When to Use

Use `classify` when:

- many items need the same decision rule applied;
- the primary agent needs to filter a large candidate set;
- data must be routed to different processing paths;
- items need priority labels such as `high`, `medium`, and `low`;
- logs, errors, files, or code fragments need coarse categorization;
- a later tool should only receive a selected subset.

Typical examples:

- classify compiler errors by probable root cause;
- classify log entries as `normal`, `warning`, or `failure`;
- classify files as `relevant`, `possibly_relevant`, or `irrelevant` to a debugging task;
- classify crash reports by subsystem;
- classify code fragments by resource-management pattern;
- classify candidate findings by confidence or urgency.

## Do Not Use When

Do not rely on `classify` for:

- final vulnerability confirmation;
- proving causality;
- nuanced semantic judgments;
- cases where category boundaries are inherently ambiguous and consequences are high;
- decisions where a false negative is unacceptable.

When in doubt, prefer an `unknown` or `needs_review` category over a confident but unsupported classification.

## Recommended Output Behavior

For each item, return:

- the selected category;
- a short rationale when useful;
- a confidence estimate when supported by the interface;
- any ambiguity or missing-information indicator.

The rationale should explain the classification using observable properties of the input rather than unsupported speculation.

## Agent Guidance

Treat classification as a **routing signal**. A high-confidence classification may justify skipping low-value work, but a low-confidence or security-sensitive classification should usually be escalated for direct inspection by the primary model.

---

# 3. `extract`

## Description

Extract explicitly requested structured information from unstructured or semi-structured input.

This tool should perform **information extraction rather than open-ended reasoning**. The caller should specify the fields, entities, patterns, or facts to extract whenever possible.

Use this tool to convert large amounts of text into a compact structured representation that can be consumed by the primary agent or another tool.

## When to Use

Use `extract` when:

- the desired information has a recognizable structure;
- the primary agent needs specific fields rather than a general summary;
- many documents or logs contain repeated patterns;
- text must be converted into machine-readable records;
- identifiers, timestamps, symbols, paths, function names, versions, error codes, or configuration values need to be collected.

Typical examples:

- extract all error messages and their timestamps from a build log;
- extract function names and source locations from compiler diagnostics;
- extract ELF symbols, section names, and suspicious permissions from text output;
- extract IP addresses, ports, protocols, and connection states from network logs;
- extract TODO/FIXME comments and associated file locations;
- extract all stack frames from crash reports;
- extract configuration keys and values from documentation or generated output.

## Do Not Use When

Do not use `extract` for conclusions that are not explicitly represented in the source.

For example, do not ask it to extract "the root cause" unless the source explicitly states or strongly and directly identifies one. Root-cause inference is a reasoning task and should generally be performed by the primary agent.

Do not use extraction as a substitute for parsing when a deterministic parser is available and correctness is important.

## Recommended Output Behavior

Return structured data matching the requested schema as closely as possible.

Rules:

- preserve exact values when requested;
- preserve source locations when available;
- distinguish missing values from empty values;
- do not silently normalize ambiguous data;
- do not invent fields that cannot be supported by the input;
- report malformed or ambiguous records explicitly.

## Agent Guidance

Prefer `extract` over `summarize` when the primary agent already knows exactly which fields it needs.

For highly deterministic formats, the agent should prefer a dedicated parser or shell tool when available. Use `extract` for semi-structured or heterogeneous inputs where deterministic parsing would require disproportionate effort.

---

# 4. `batch_analyze`

## Description

Run the same lightweight analysis task independently across a collection of inputs and return a compact per-item result plus optional aggregate statistics.

This tool is designed for **high-volume parallel preprocessing**. Each item should be analyzed independently whenever possible. The tool should avoid expensive cross-item reasoning unless explicitly requested.

## When to Use

Use `batch_analyze` when:

- dozens, hundreds, or thousands of similar inputs need screening;
- the same question must be applied repeatedly;
- the primary agent only needs a small result per item;
- candidate generation should happen before deeper reasoning;
- the task can be parallelized naturally.

Typical examples:

- scan hundreds of source files for suspicious resource lifetimes;
- analyze many log files and report unusual patterns;
- inspect every function in a module for a particular anti-pattern;
- rank a large set of documents by relevance to a question;
- identify likely duplicate or near-duplicate records;
- pre-screen a directory for files that deserve deeper analysis.

## Do Not Use When

Do not use `batch_analyze` when the task fundamentally requires:

- understanding relationships among all items;
- global architectural reasoning;
- cross-file causality that cannot be decomposed safely;
- a single definitive conclusion based on the entire dataset;
- subtle comparisons where independent analysis may hide important interactions.

In such cases, use batch analysis only as a first-pass candidate generator and let the primary model perform the cross-item reasoning afterward.

## Recommended Output Behavior

For each input item, return a small structured result containing, where applicable:

- item identifier;
- status;
- findings or candidate findings;
- confidence;
- concise rationale;
- relevant source locations.

The tool should also report aggregate information such as:

- number of items processed;
- number of items with findings;
- number of failures or skipped items;
- distribution of classifications when useful.

Do not return full prose analysis for every item unless explicitly requested. The default should optimize for downstream machine consumption and context efficiency.

## Agent Guidance

This is one of the most valuable tools for token reduction. Prefer it when the alternative would be sending many similar items individually to the primary model.

Use high-recall settings for security or debugging triage, then send shortlisted items to the primary model for verification.

---

# 5. `code_review_precheck`

## Description

Perform a lightweight first-pass review of source code to identify **candidate issues, suspicious patterns, missing checks, resource-lifetime hazards, and areas that deserve deeper review**.

This tool is a pre-review or triage stage. It must not claim that a vulnerability, bug, or correctness defect is proven unless the evidence is explicit and the interface guarantees such a conclusion.

The intended purpose is to reduce the amount of code that the primary model needs to inspect deeply.

## When to Use

Use `code_review_precheck` when:

- a large amount of source code needs initial screening;
- the primary agent wants candidate locations before detailed reasoning;
- a patch contains many files or functions;
- common bug patterns can be screened cheaply;
- the user asks for a broad review and a first-pass triage is useful.

Useful screening targets include:

- ownership and lifetime issues;
- use-after-free candidates;
- double-free candidates;
- null or invalid pointer usage;
- unchecked return values;
- integer overflow or truncation candidates;
- missing bounds checks;
- unsafe string or buffer handling;
- lock/unlock asymmetry;
- resource leaks;
- error-path cleanup problems;
- suspicious concurrency patterns;
- suspicious FFI or ABI boundaries;
- potentially unsafe casts;
- missing synchronization;
- inconsistent error handling;
- suspicious privilege or trust-boundary transitions.

## Do Not Use When

Do not treat this tool as a replacement for:

- a full security audit;
- formal verification;
- dynamic testing;
- sanitizer instrumentation;
- static analysis with deterministic guarantees;
- exploitability analysis;
- final vulnerability disclosure decisions.

Do not assume that the absence of findings means the code is safe.

## Recommended Output Behavior

Return candidate findings with:

- severity estimate;
- confidence;
- category;
- file and line information where available;
- concise explanation of the suspicious pattern;
- relevant surrounding symbols or functions;
- suggested verification method.

Explicitly distinguish:

- `candidate`: suspicious and worth inspecting;
- `likely_issue`: evidence is relatively strong but requires verification;
- `not_issue`: pattern was considered and appears benign, when the interface supports this;
- `uncertain`: insufficient context.

Avoid presenting speculative findings as confirmed vulnerabilities.

## Agent Guidance

This tool should normally be used as **high-recall triage**.

A good downstream workflow is:

1. run the precheck over a broad code region;
2. collect candidate findings;
3. inspect the most important candidates using the primary model and deterministic tools;
4. verify with source context, tests, sanitizers, debugger traces, or other evidence when appropriate;
5. only then make the final judgment.

For security-sensitive work, missing a true issue may be more costly than producing extra candidates. Prefer recall over aggressive filtering unless the caller explicitly requests otherwise.

---

# 6. `compress_context`

## Description

Transform a large body of context into a compact, loss-aware representation optimized for consumption by the primary reasoning model.

Unlike `summarize`, which primarily explains content to a human reader, `compress_context` should optimize for **downstream reasoning efficiency**. It should preserve decision-relevant facts, dependencies, evidence, identifiers, contradictions, unresolved questions, and provenance while aggressively removing redundant natural language.

This tool is particularly useful immediately before sending information to a token-limited or expensive primary model.

## When to Use

Use `compress_context` when:

- the context is too large or expensive to send directly to the primary model;
- multiple summaries, logs, tool outputs, or documents contain duplicated information;
- the primary model needs evidence rather than prose;
- the same identifiers, paths, facts, and observations recur across many inputs;
- prior tool results have accumulated into an unnecessarily large context;
- the agent wants to preserve reasoning-relevant state while discarding verbosity.

Typical examples:

- compress a long debugging session before asking the primary model for a diagnosis;
- compress outputs from multiple shell commands;
- merge several local-model summaries into a single evidence pack;
- reduce a large incident timeline while preserving timestamps and causal evidence;
- compact the state of a multi-step investigation.

## Do Not Use When

Do not use this tool when:

- exact wording is itself evidence;
- the primary model must inspect all source material directly;
- omitting a seemingly minor detail could invalidate the reasoning;
- legal or contractual text requires exact preservation;
- the data is already small enough that compression provides little benefit.

Never assume that compression is lossless unless the interface explicitly guarantees it.

## Recommended Output Behavior

Favor a structured, evidence-oriented representation.

Preserve, when applicable:

- facts and observations;
- source/provenance;
- timestamps and ordering;
- identifiers and locations;
- numerical values and units;
- constraints;
- contradictions;
- hypotheses already proposed by other tools;
- unresolved questions;
- confidence and uncertainty;
- relationships between observations.

Remove:

- repetitive prose;
- boilerplate;
- duplicated explanations;
- stylistic filler;
- redundant restatements of the same fact.

Where information is discarded aggressively, say so explicitly and indicate what kind of information may have been omitted.

## Agent Guidance

Use this tool when the primary constraint is **context size**, not lack of reasoning ability.

For critical investigations, retain pointers to the original evidence so the primary agent can retrieve or inspect the original material when needed.

A strong design is to produce a compressed evidence pack containing both a compact representation and references back to the original sources.

---

# 7. `ask`

## Description

Send a direct natural-language task to the local language model and return its response.

This is the **general-purpose escape hatch** for tasks that do not fit the specialized tools. Because it is less constrained than the other tools, it should be treated as a lower-level primitive rather than the default delegation mechanism.

The primary agent should prefer the specialized tools whenever the intended operation clearly matches one of them.

## When to Use

Use `ask` when:

- the task is genuinely open-ended but still suitable for a small local model;
- a specialized tool does not fit the operation;
- the primary agent wants a cheap secondary opinion;
- a local model is useful for generating candidate approaches or interpretations;
- the task is exploratory and errors are cheap to recover from.

Typical examples:

- ask for possible explanations of a non-critical error;
- generate candidate shell commands for later verification;
- brainstorm possible categories before using `classify`;
- ask for an alternative interpretation of a document;
- generate a rough first-pass explanation that the primary model will verify.

## Do Not Use When

Do not use `ask` as the default substitute for the primary model when the task requires high-quality reasoning.

Do not rely on `ask` alone for:

- final security conclusions;
- authoritative root-cause analysis;
- high-stakes decisions;
- subtle code correctness judgments;
- exact factual claims that require verification;
- tasks where hallucination is expensive.

Do not recursively delegate the entire task to `ask` when a more specialized tool would produce a smaller and more reliable result.

## Recommended Output Behavior

The response may be natural language, but should remain concise and explicit about uncertainty.

When possible, request:

- candidate answers rather than unsupported certainty;
- concise reasoning;
- assumptions;
- confidence;
- evidence or observations used;
- unresolved ambiguities.

The local model should not pretend to have performed actions, accessed files, executed commands, or verified facts that were not actually provided or performed through available tools.

## Agent Guidance

Treat `ask` as a **secondary local opinion**, not as an authoritative oracle.

A useful pattern is:

1. ask the local model for candidate explanations;
2. let the primary model evaluate those candidates;
3. use deterministic tools or direct inspection to verify the most important claims.

When a specialized tool exists for the task, prefer the specialized tool because its narrower contract makes delegation behavior easier for the primary agent to reason about.

---

# Cross-Tool Delegation Policy

## Prefer Specialized Tools Over `ask`

When several tools could perform a task, select the most constrained tool that matches the intent:

- overview of a large input → `summarize`
- assign labels or route items → `classify`
- retrieve explicit fields → `extract`
- repeatedly analyze many independent items → `batch_analyze`
- perform a broad first-pass source review → `code_review_precheck`
- reduce accumulated context for the primary model → `compress_context`
- open-ended local-model assistance → `ask`

## Confidence Is Not Correctness

A confidence score expresses the local model's internal assessment, not proof of correctness.

For important findings, the primary agent should independently evaluate the evidence.

## Preserve Provenance

Whenever feasible, outputs should contain references to the original input, such as:

- file path;
- line number;
- record identifier;
- timestamp;
- document section;
- source item index.

This allows the primary agent to move from compressed evidence back to authoritative source material.

## Optimize for Information Density

The purpose of local delegation is not merely to produce cheaper prose. It is to reduce the amount of information that the primary model must process while preserving the information required for good decisions.

A good local-model result should therefore be:

- compact;
- structured;
- provenance-aware;
- explicit about uncertainty;
- easy for another model to consume programmatically;
- optimized for the next reasoning step.

## Escalation Rule

When the local model determines that a task requires subtle reasoning, insufficient context, or a level of certainty it cannot provide, it should return an explicit indication that the task should be escalated to the primary model rather than inventing confidence.

The preferred behavior is:

> identify → compress → shortlist → escalate → verify

rather than:

> identify → guess → declare a conclusion

