# Blog Agent Evolution — Mermaid Diagrams
# All 4 versions: individual graphs + evolution comparison + reducer subgraph

# ─────────────────────────────────────────────────────────────────────────────
# DIAGRAM 1 — V1: Basic Blog Agent
# ─────────────────────────────────────────────────────────────────────────────

```mermaid
flowchart TD
    S([__start__]) --> GS

    GS["generate_sections\n──────────────\nLLM → 3 section titles\nStructured output: Sections\nReturns: sections[]"]

    GS -->|Send per section| W1[write_section]
    GS -->|Send per section| W2[write_section]
    GS -->|Send per section| W3[write_section]

    W1 --> CB
    W2 --> CB
    W3 --> CB

    CB["compile_blog\n──────────────\nJoin all sections\nPrint to terminal"]

    CB --> E([__end__])

    style S fill:374151,color:fff
    style E fill:374151,color:fff
    style GS fill:1E3A5F,color:fff
    style W1 fill:7C3AED,color:fff
    style W2 fill:7C3AED,color:fff
    style W3 fill:7C3AED,color:fff
    style CB fill:15803D,color:fff
```

# ─────────────────────────────────────────────────────────────────────────────
# DIAGRAM 2 — V2: Improved Prompting
# ─────────────────────────────────────────────────────────────────────────────

```mermaid
flowchart TD
    S([__start__]) --> ORC

    ORC["orchestrator\n──────────────\nRich system prompt\nReturns: Plan\n  blog_title, audience,\n  tone, tasks[]"]

    ORC -->|fanout — Send per Task| W1[worker]
    ORC -->|fanout — Send per Task| W2[worker]
    ORC -->|fanout — Send per Task| WN[worker ...]

    W1 --> R
    W2 --> R
    WN --> R

    R["reducer\n──────────────\nSort by task.id\nJoin sections\nSave to .md file"]

    R --> E([__end__])

    style S fill:374151,color:fff
    style E fill:374151,color:fff
    style ORC fill:1E3A5F,color:fff
    style W1 fill:7C3AED,color:fff
    style W2 fill:7C3AED,color:fff
    style WN fill:7C3AED,color:fff
    style R fill:15803D,color:fff
```

# ─────────────────────────────────────────────────────────────────────────────
# DIAGRAM 3 — V3: Research-Powered (Tavily)
# ─────────────────────────────────────────────────────────────────────────────

```mermaid
flowchart TD
    S([__start__]) --> RT

    RT["router_node\n──────────────\nClassifies topic:\nclosed_book / hybrid / open_book\nSets: mode, queries"]

    RT -->|needs_research = true| RS
    RT -->|needs_research = false| ORC

    RS["research_node\n──────────────\nTavily web search\nLLM cleans + deduplicates\nReturns: EvidenceItem[]"]

    RS --> ORC

    ORC["orchestrator_node\n──────────────\nEvidence-aware planning\nThree-mode prompt\nReturns: Plan"]

    ORC -->|fanout — Send per Task| W1[worker_node]
    ORC -->|fanout — Send per Task| W2[worker_node]
    ORC -->|fanout — Send per Task| WN[worker_node ...]

    W1 --> RED
    W2 --> RED
    WN --> RED

    RED["reducer_node\n──────────────\nSort by task.id\nJoin sections\nSave to .md file\nGrounding policy\nScope guard"]

    RED --> E([__end__])

    style S fill:374151,color:fff
    style E fill:374151,color:fff
    style RT fill:0F766E,color:fff
    style RS fill:0284C7,color:fff
    style ORC fill:1E3A5F,color:fff
    style W1 fill:7C3AED,color:fff
    style W2 fill:7C3AED,color:fff
    style WN fill:7C3AED,color:fff
    style RED fill:15803D,color:fff
```

# ─────────────────────────────────────────────────────────────────────────────
# DIAGRAM 4 — V4: Images + Streamlit UI (Main Graph)
# ─────────────────────────────────────────────────────────────────────────────

```mermaid
flowchart TD
    S([__start__]) --> RT

    RT["router_node\n──────────────\nmode + recency_days\nas_of date-aware"]

    RT -->|needs_research = true| RS
    RT -->|needs_research = false| ORC

    RS["research_node\n──────────────\nTavily search\nDate-filtered evidence\nopen_book: 7-day cutoff"]

    RS --> ORC

    ORC["orchestrator_node\n──────────────\nEvidence-aware planning\nForces news_roundup\nfor open_book"]

    ORC -->|fanout — Send per Task| W1[worker_node]
    ORC -->|fanout — Send per Task| W2[worker_node]
    ORC -->|fanout — Send per Task| WN[worker_node ...]

    W1 --> RED
    W2 --> RED
    WN --> RED

    subgraph RED [reducer subgraph]
        direction TB
        MC["merge_content\nSort + Join"] --> DI["decide_images\nLLM places placeholders"] --> GI["generate_and_place_images\nGemini 2.5 Flash → PNG"]
    end

    RED --> E([__end__])

    style S fill:374151,color:fff
    style E fill:374151,color:fff
    style RT fill:0F766E,color:fff
    style RS fill:0284C7,color:fff
    style ORC fill:1E3A5F,color:fff
    style W1 fill:7C3AED,color:fff
    style W2 fill:7C3AED,color:fff
    style WN fill:7C3AED,color:fff
    style MC fill:15803D,color:fff
    style DI fill:D97706,color:fff
    style GI fill:DC2626,color:fff
```

# ─────────────────────────────────────────────────────────────────────────────
# DIAGRAM 5 — REDUCER SUBGRAPH (V4 only, detailed)
# ─────────────────────────────────────────────────────────────────────────────

```mermaid
flowchart TD
    S([__start__\nreceives sections as tuples]) --> MC

    MC["merge_content\n────────────────────────────\nInput:  List of tuple[task_id, section_md]\nAction: sort by task_id\n        join with double newlines\n        prepend # BlogTitle\nOutput: merged_md stored in State"]

    MC --> DI

    DI["decide_images\n────────────────────────────\nInput:  merged_md + plan\nAction: LLM reads full blog\n        decides IF images help\n        inserts up to 3 placeholders\n        [[IMAGE_1]] [[IMAGE_2]] [[IMAGE_3]]\n        writes Gemini prompt per image\nOutput: md_with_placeholders\n        image_specs list"]

    DI --> GI

    GI["generate_and_place_images\n────────────────────────────\nFor each ImageSpec:\n  → file exists? skip generation\n  → send prompt to Gemini 2.5 Flash\n  → receive raw PNG bytes\n  → save to images/filename.png\n  → replace [[IMAGE_N]] with\n    ![alt](images/filename.png)\n  → on failure: error block\nSave final blog to .md file\nOutput: final markdown in State"]

    GI --> E([__end__\nsaved .md + images/ folder])

    style S fill:374151,color:fff
    style E fill:374151,color:fff
    style MC fill:15803D,color:fff
    style DI fill:0284C7,color:fff
    style GI fill:7C3AED,color:fff
```

# ─────────────────────────────────────────────────────────────────────────────
# DIAGRAM 6 — EVOLUTION COMPARISON (all 4 side by side)
# ─────────────────────────────────────────────────────────────────────────────

```mermaid
flowchart LR
    subgraph V1 ["V1 — Basic"]
        direction TB
        v1s([start]) --> v1g[generate_sections]
        v1g --> v1w[write_section ×3]
        v1w --> v1c[compile_blog]
        v1c --> v1e([end])
    end

    subgraph V2 ["V2 — Better Prompts"]
        direction TB
        v2s([start]) --> v2o[orchestrator]
        v2o --> v2w[worker ×N]
        v2w --> v2r[reducer]
        v2r --> v2e([end])
    end

    subgraph V3 ["V3 — Web Research"]
        direction TB
        v3s([start]) --> v3r[router]
        v3r --> v3rs[research?]
        v3rs --> v3o[orchestrator]
        v3o --> v3w[worker ×N]
        v3w --> v3rd[reducer]
        v3rd --> v3e([end])
    end

    subgraph V4 ["V4 — Images + UI"]
        direction TB
        v4s([start]) --> v4r[router]
        v4r --> v4rs[research?]
        v4rs --> v4o[orchestrator]
        v4o --> v4w[worker ×N]
        v4w --> v4sub[reducer subgraph\nmerge → images → place]
        v4sub --> v4e([end])
    end

    V1 -->|"Rich Task model\nBetter prompts\nPlan object"| V2
    V2 -->|"Router node\nTavily search\nEvidence grounding"| V3
    V3 -->|"Image planning\nGemini generation\nStreamlit UI"| V4

    style v1g fill:1E3A5F,color:fff
    style v1w fill:7C3AED,color:fff
    style v1c fill:15803D,color:fff
    style v2o fill:1E3A5F,color:fff
    style v2w fill:7C3AED,color:fff
    style v2r fill:15803D,color:fff
    style v3r fill:0F766E,color:fff
    style v3rs fill:0284C7,color:fff
    style v3o fill:1E3A5F,color:fff
    style v3w fill:7C3AED,color:fff
    style v3rd fill:15803D,color:fff
    style v4r fill:0F766E,color:fff
    style v4rs fill:0284C7,color:fff
    style v4o fill:1E3A5F,color:fff
    style v4w fill:7C3AED,color:fff
    style v4sub fill:DC2626,color:fff
```
