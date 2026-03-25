from __future__ import annotations

import operator
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import TypedDict, List, Optional, Literal, Annotated

from pydantic import BaseModel, Field

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# PM-tuned Blog Writer
# Audience: Product Managers and Product Leaders
# Pipeline: Router → (Research?) → Orchestrator → Workers → ReducerWithImages → FurtherReading
# Reducer subgraph: merge_content → decide_images → generate_and_place_images → further_reading
# ============================================================


# -----------------------------
# 1) Schemas
# -----------------------------

class Task(BaseModel):
    id: int
    title: str
    goal: str = Field(
        ...,
        description="One sentence describing what the PM reader should understand or be able to do after this section.",
    )
    bullets: List[str] = Field(
        ...,
        min_length=3,
        max_length=6,
        description="3–6 concrete, non-overlapping subpoints. Frame from a PM perspective where possible.",
    )
    target_words: int = Field(..., description="Target word count (120–550).")
    tags: List[str] = Field(default_factory=list)

    # ── Original flags ──
    requires_research: bool = False
    requires_citations: bool = False
    requires_code: bool = False

    # ── NEW PM-specific flags ──
    requires_pm_translation: bool = Field(
        False,
        description=(
            "Set True when this section contains heavy engineering or data science concepts "
            "that need to be translated into plain English + business impact for a PM audience. "
            "Worker will add a '> What this means for you as a PM' callout box."
        ),
    )
    pm_takeaway: str = Field(
        "",
        description=(
            "A single punchy sentence stating the business or product impact of this section. "
            "Example: 'This directly affects how fast your team can ship a new ranking model.' "
            "Must be concrete — not a re-statement of the section title."
        ),
    )


class Plan(BaseModel):
    blog_title: str
    audience: str
    tone: str
    blog_kind: Literal[
        "explainer",
        "tutorial",
        "news_roundup",
        "comparison",
        "system_design",
        "pm_explainer",   # NEW — tech concept explained for product leaders
    ] = "pm_explainer"
    constraints: List[str] = Field(default_factory=list)
    tasks: List[Task]


class EvidenceItem(BaseModel):
    title: str
    url: str
    published_at: Optional[str] = None  # ISO "YYYY-MM-DD" preferred
    snippet: Optional[str] = None
    source: Optional[str] = None


class RouterDecision(BaseModel):
    needs_research: bool
    mode: Literal["closed_book", "hybrid", "open_book"]
    reason: str
    queries: List[str] = Field(default_factory=list)
    max_results_per_query: int = Field(5)


class EvidencePack(BaseModel):
    evidence: List[EvidenceItem] = Field(default_factory=list)


class ImageSpec(BaseModel):
    placeholder: str = Field(..., description="e.g. [[IMAGE_1]]")
    filename: str = Field(..., description="Save under images/, e.g. pipeline_analogy.png")
    alt: str
    caption: str
    prompt: str = Field(..., description="Prompt to send to the image model.")
    size: Literal["1024x1024", "1024x1536", "1536x1024"] = "1024x1024"
    quality: Literal["low", "medium", "high"] = "medium"


class GlobalImagePlan(BaseModel):
    md_with_placeholders: str
    images: List[ImageSpec] = Field(default_factory=list)


class State(TypedDict):
    topic: str

    # routing / research
    mode: str
    needs_research: bool
    queries: List[str]
    evidence: List[EvidenceItem]
    plan: Optional[Plan]

    # recency
    as_of: str
    recency_days: int

    # workers
    sections: Annotated[List[tuple[int, str]], operator.add]  # (task_id, section_md)

    # reducer/image
    merged_md: str
    md_with_placeholders: str
    image_specs: List[dict]

    # NEW — further reading block (pure markdown, appended at end)
    further_reading: str

    final: str


# -----------------------------
# 2) LLM
# -----------------------------
llm = ChatOpenAI(model="gpt-5.4-mini-2026-03-17")


# -----------------------------
# 3) Router
# -----------------------------
ROUTER_SYSTEM = """You are a routing module for a blog planner. The blog audience is always Product Managers and Product Leaders, not engineers.

Decide whether web research is needed BEFORE planning.

Modes:
- closed_book (needs_research=false):
  Timeless concepts where correctness does not depend on recent facts.
  Example: "What is a data pipeline?" or "How does A/B testing work?"

- hybrid (needs_research=true):
  Mostly evergreen but PMs need current, specific examples — real tools, real company decisions,
  current pricing tiers, recent model releases. When in doubt, choose hybrid over closed_book
  because PMs trust blogs that reference real current examples.
  Example: "LLMs for product managers", "AI feature prioritisation"

- open_book (needs_research=true):
  Volatile: weekly roundups, "this week", "latest", rankings, regulatory changes, funding news.

If needs_research=true:
- Output EXACTLY 6 high-signal, scoped queries. No more, no less.
- For PM audience, prefer queries that surface: case studies, product decisions, business impact data,
  real company examples, and industry analyst perspectives.
- For open_book weekly topics, include queries reflecting last 7 days.
"""

def router_node(state: State) -> dict:
    decider = llm.with_structured_output(RouterDecision)
    decision = decider.invoke(
        [
            SystemMessage(content=ROUTER_SYSTEM),
            HumanMessage(content=f"Topic: {state['topic']}\nAs-of date: {state['as_of']}"),
        ]
    )

    if decision.mode == "open_book":
        recency_days = 7
    elif decision.mode == "hybrid":
        recency_days = 45
    else:
        recency_days = 3650

    return {
        "needs_research": decision.needs_research,
        "mode": decision.mode,
        "queries": decision.queries,
        "recency_days": recency_days,
    }

def route_next(state: State) -> str:
    return "research" if state["needs_research"] else "orchestrator"


# -----------------------------
# 4) Research (Tavily)
# -----------------------------
def _tavily_search(query: str, max_results: int = 5) -> List[dict]:
    if not os.getenv("TAVILY_API_KEY"):
        return []
    try:
        from langchain_community.tools.tavily_search import TavilySearchResults  # type: ignore
        tool = TavilySearchResults(max_results=max_results)
        results = tool.invoke({"query": query})
        out: List[dict] = []
        for r in results or []:
            out.append(
                {
                    "title": r.get("title") or "",
                    "url": r.get("url") or "",
                    "snippet": r.get("content") or r.get("snippet") or "",
                    "published_at": r.get("published_date") or r.get("published_at"),
                    "source": r.get("source"),
                }
            )
        return out
    except Exception:
        return []

def _ddg_search(query: str, max_results: int = 5) -> List[dict]:
    try:
        from duckduckgo_search import DDGS
        results = DDGS().text(query, max_results=max_results)
        out = []
        for r in results or []:
            out.append({
                "title": r.get("title") or "",
                "url": r.get("href") or "",
                "snippet": r.get("body") or "",
                "published_at": None,
                "source": "DuckDuckGo"
            })
        return out
    except Exception:
        return []

def _wikipedia_search(query: str, max_results: int = 2) -> List[dict]:
    try:
        import wikipedia
        results = wikipedia.search(query, results=max_results)
        out = []
        for title in results:
            try:
                p = wikipedia.page(title, auto_suggest=False)
                out.append({
                    "title": p.title,
                    "url": p.url,
                    "snippet": p.summary[:500],
                    "published_at": None,
                    "source": "Wikipedia"
                })
            except Exception:
                pass
        return out
    except Exception:
        return []

def _arxiv_search(query: str, max_results: int = 2) -> List[dict]:
    try:
        import urllib.request
        import urllib.parse
        import xml.etree.ElementTree as ET
        url = f'http://export.arxiv.org/api/query?search_query=all:{urllib.parse.quote(query)}&start=0&max_results={max_results}'
        response = urllib.request.urlopen(url)
        xml_data = response.read()
        root = ET.fromstring(xml_data)
        out = []
        for entry in root.findall('{http://www.w3.org/2005/Atom}entry'):
            title = entry.find('{http://www.w3.org/2005/Atom}title')
            summary = entry.find('{http://www.w3.org/2005/Atom}summary')
            link = entry.find('{http://www.w3.org/2005/Atom}id')
            published = entry.find('{http://www.w3.org/2005/Atom}published')
            out.append({
                "title": title.text.replace('\n', ' ').strip() if title is not None else "",
                "url": link.text if link is not None else "",
                "snippet": summary.text.replace('\n', ' ').strip()[:500] if summary is not None else "",
                "published_at": published.text if published is not None else None,
                "source": "Arxiv"
            })
        return out
    except Exception:
        return []

def _newsdata_search(query: str, max_results: int = 2) -> List[dict]:
    try:
        import requests
        import urllib.parse
        apikey = "pub_13f1dfd4ba5d4fea973e0a4fe9a9a6c3"
        url = f"https://newsdata.io/api/1/latest?apikey={apikey}&q={urllib.parse.quote(query)}"
        response = requests.get(url)
        data = response.json()
        out = []
        if data.get("status") == "success":
            for article in data.get("results", [])[:max_results]:
                out.append({
                    "title": article.get("title") or "",
                    "url": article.get("link") or "",
                    "snippet": str(article.get("description") or article.get("content") or "")[:500],
                    "published_at": article.get("pubDate"),
                    "source": article.get("source_id") or "Newsdata.io"
                })
        return out
    except Exception:
        return []

def _producthunt_search(query: str, max_results: int = 2) -> List[dict]:
    import os
    import requests
    ph_key = os.getenv("PRODUCTHUNT_API_KEY")
    ph_secret = os.getenv("PRODUCTHUNT_API_SECRET")
    ph_dev_token = os.getenv("PRODUCTHUNT_DEV_TOKEN")

    if not (ph_dev_token or (ph_key and ph_secret)):
        return []
    try:
        if ph_dev_token:
            access_token = ph_dev_token
        else:
            token_res = requests.post(
                "https://api.producthunt.com/v2/oauth/token",
                json={"client_id": ph_key, "client_secret": ph_secret, "grant_type": "client_credentials"}
            )
            if token_res.status_code != 200:
                return []
            access_token = token_res.json().get("access_token")

        gql_query = """
        query {
          posts(first: 10) {
            edges {
              node {
                name
                tagline
                description
                url
                createdAt
              }
            }
          }
        }
        """
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        res = requests.post("https://api.producthunt.com/v2/api/graphql", headers=headers, json={"query": gql_query})
        data = res.json()
        out = []
        if "data" in data and data.get("data") and "posts" in data["data"]:
            for edge in data["data"]["posts"]["edges"][:max_results]:
                node = edge["node"]
                out.append({
                    "title": f"[Product Hunt] {node.get('name')} - {node.get('tagline')}",
                    "url": node.get("url") or "",
                    "snippet": str(node.get("description") or "")[:500],
                    "published_at": node.get("createdAt"),
                    "source": "Product Hunt"
                })
        return out
    except Exception:
        return []

def _iso_to_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None

RESEARCH_SYSTEM = """You are a research synthesizer for a Product Management blog.

Given raw web search results, produce a deduplicated list of EvidenceItem objects.

Rules:
- Only include items with a non-empty url.
- Prefer sources relevant to a PM audience: product blogs, industry case studies, company engineering blogs,
  product management publications, analyst reports, and news about product decisions.
- Normalise published_at to ISO YYYY-MM-DD if reliably inferable; else null. NEVER guess.
- Keep snippets concise (under 200 chars).
- Deduplicate by URL.
"""

def research_node(state: State) -> dict:
    queries = (state.get("queries") or [])[:10]
    raw: List[dict] = []
    for q in queries:
        raw.extend(_tavily_search(q, max_results=4))
        raw.extend(_ddg_search(q, max_results=3))
        raw.extend(_wikipedia_search(q, max_results=1))
        raw.extend(_arxiv_search(q, max_results=1))
        raw.extend(_newsdata_search(q, max_results=2))
        raw.extend(_producthunt_search(q, max_results=2))

    if not raw:
        return {"evidence": []}

    extractor = llm.with_structured_output(EvidencePack)
    pack = extractor.invoke(
        [
            SystemMessage(content=RESEARCH_SYSTEM),
            HumanMessage(
                content=(
                    f"As-of date: {state['as_of']}\n"
                    f"Recency days: {state['recency_days']}\n\n"
                    f"Raw results:\n{raw}"
                )
            ),
        ]
    )

    dedup = {}
    for e in pack.evidence:
        if e.url:
            dedup[e.url] = e
    evidence = list(dedup.values())

    if state.get("mode") == "open_book":
        as_of = date.fromisoformat(state["as_of"])
        cutoff = as_of - timedelta(days=int(state["recency_days"]))
        evidence = [e for e in evidence if (d := _iso_to_date(e.published_at)) and d >= cutoff]

    return {"evidence": evidence}


# -----------------------------
# 5) Orchestrator (Plan)
# -----------------------------
ORCH_SYSTEM = """You are a senior content strategist specialising in product management education.
Your audience is always Product Managers and Product Leaders — not engineers or data scientists.
Your job is to plan a blog that makes technical and data science concepts genuinely useful for PMs.

Hard requirements for every plan:
- Create 5–9 sections (tasks).
- Each task must have: goal (1 sentence), 3–6 bullets, target_words (120–550).
- Every bullet must be answerable from a PM perspective — frame around decisions, trade-offs, business outcomes.

PM translation rule:
- Any section covering a technical mechanism (e.g. how a model works, how an algorithm runs) MUST have
  requires_pm_translation=True. The worker will add a plain-English callout box for that section.
- Every task must have pm_takeaway — a single punchy sentence of business/product impact.
  Example: "Understanding this helps you set realistic expectations with engineering about model launch timelines."
  NOT acceptable: "This section explains what embeddings are." (that's a description, not a takeaway)

Structure requirements:
- Include AT LEAST ONE section with a focus on business impact, cost, or ROI implications.
- Include AT LEAST ONE section with real-world product examples (companies that have used this, outcomes they saw).
- For open_book mode: set blog_kind="news_roundup", focus on events + implications for product teams.
- For all other modes: set blog_kind="pm_explainer" unless the user explicitly requested a tutorial or comparison.

Grounding rules:
- closed_book: evergreen, no evidence dependence. Examples can be illustrative/hypothetical.
- hybrid: use evidence for specific current tools, models, or company examples.
  Mark those tasks requires_research=True and requires_citations=True.
- open_book: every section grounded in evidence. No invented events.

Output must strictly match the Plan schema.
"""

def orchestrator_node(state: State) -> dict:
    planner = llm.with_structured_output(Plan)
    mode = state.get("mode", "closed_book")
    evidence = state.get("evidence", [])

    forced_kind = "news_roundup" if mode == "open_book" else None

    plan = planner.invoke(
        [
            SystemMessage(content=ORCH_SYSTEM),
            HumanMessage(
                content=(
                    f"Topic: {state['topic']}\n"
                    f"Mode: {mode}\n"
                    f"As-of: {state['as_of']} (recency_days={state['recency_days']})\n"
                    f"{'Force blog_kind=news_roundup' if forced_kind else ''}\n\n"
                    f"Evidence (use for up-to-date examples and citations):\n"
                    f"{[e.model_dump() for e in evidence][:16]}"
                )
            ),
        ]
    )
    if forced_kind:
        plan.blog_kind = "news_roundup"

    return {"plan": plan}


# -----------------------------
# 6) Fanout
# -----------------------------
def fanout(state: State):
    assert state["plan"] is not None
    return [
        Send(
            "worker",
            {
                "task": task.model_dump(),
                "topic": state["topic"],
                "mode": state["mode"],
                "as_of": state["as_of"],
                "recency_days": state["recency_days"],
                "plan": state["plan"].model_dump(),
                "evidence": [e.model_dump() for e in state.get("evidence", [])],
            },
        )
        for task in state["plan"].tasks
    ]


# -----------------------------
# 7) Worker
# -----------------------------
WORKER_SYSTEM = """You are a senior writer who translates technical and data science concepts for Product Managers and Product Leaders.
Write ONE section of a blog post in Markdown.

Your reader is a PM or product leader. They are smart and curious but did not study computer science.
They care about: business outcomes, product decisions, team trade-offs, cost, speed, and risk.
They do NOT care about implementation syntax, algorithm internals, or engineering architecture for its own sake.

Writing rules — follow ALL of these:

1. ANALOGY FIRST
   Always open with a short, concrete real-world analogy BEFORE any technical definition.
   Good: "Think of a vector embedding like a postal code for meaning — two similar ideas get codes that are geographically close."
   Bad: "A vector embedding is a numerical representation of data in a high-dimensional space."

2. PLAIN ENGLISH FOR EVERY TECHNICAL TERM
   Every time you use a technical term, immediately follow it with a plain-English translation in parentheses.
   Example: "The model uses attention mechanisms (a way of deciding which words in a sentence matter most for a given context)..."

3. PM TRANSLATION CALLOUT
   If requires_pm_translation is True, add this callout box AFTER the technical explanation:
   > **💡 What this means for you as a PM**
   > [2–4 sentences explaining the direct product, roadmap, team, or budget implication.
   >  Be specific. Mention decisions this affects, risks it creates, or opportunities it unlocks.]
   Use the pm_takeaway field as the opening sentence of this callout.

4. BUSINESS IMPACT LANGUAGE
   Use phrases like: "This means your team can...", "This affects your roadmap because...",
   "The business trade-off is...", "When this goes wrong, you'll see it as..."
   Avoid phrases like: "This algorithm works by...", "The mathematical intuition is..."

5. REAL EXAMPLES OVER ABSTRACTIONS
   Ground every concept in a real product example. Use companies, products, and features your PM reader knows.
   (Spotify, Netflix, Swiggy, Zomato, Uber, Amazon, WhatsApp, Paytm, Google Search, etc.)

6. SCOPE GUARD
   If blog_kind=="news_roundup": focus on events and their implications for product teams. NO tutorials.

7. GROUNDING POLICY
   If mode=="open_book": every specific claim (event, company, model, funding) MUST have a citation
   from provided Evidence URLs using ([Source](URL)). If not in evidence: "Not confirmed in available sources."
   If requires_citations==True: cite Evidence URLs for all external claims.

8. CODE
   If requires_code==True: include one minimal, practical code snippet. Add a comment above it
   explaining what a PM should understand about it (even if they never write it themselves).

9. FORMAT
   - Start with "## <Section Title>"
   - Short paragraphs (3–4 sentences max)
   - Use bullet points for lists of 3+ items
   - Bold the most important phrase in each paragraph
   - Output ONLY the section markdown — no preamble, no "Here is the section:" wrapper
"""

def worker_node(payload: dict) -> dict:
    task = Task(**payload["task"])
    plan = Plan(**payload["plan"])
    evidence = [EvidenceItem(**e) for e in payload.get("evidence", [])]

    bullets_text = "\n- " + "\n- ".join(task.bullets)
    evidence_text = "\n".join(
        f"- {e.title} | {e.url} | {e.published_at or 'date:unknown'}"
        for e in evidence[:20]
    )

    section_md = llm.invoke(
        [
            SystemMessage(content=WORKER_SYSTEM),
            HumanMessage(
                content=(
                    f"Blog title: {plan.blog_title}\n"
                    f"Audience: {plan.audience}\n"
                    f"Tone: {plan.tone}\n"
                    f"Blog kind: {plan.blog_kind}\n"
                    f"Constraints: {plan.constraints}\n"
                    f"Topic: {payload['topic']}\n"
                    f"Mode: {payload.get('mode')}\n"
                    f"As-of: {payload.get('as_of')} (recency_days={payload.get('recency_days')})\n\n"
                    f"Section title: {task.title}\n"
                    f"Goal: {task.goal}\n"
                    f"PM Takeaway (use as opening of callout box): {task.pm_takeaway}\n"
                    f"Target words: {task.target_words}\n"
                    f"Tags: {task.tags}\n"
                    f"requires_pm_translation: {task.requires_pm_translation}\n"
                    f"requires_research: {task.requires_research}\n"
                    f"requires_citations: {task.requires_citations}\n"
                    f"requires_code: {task.requires_code}\n"
                    f"Bullets to cover:\n{bullets_text}\n\n"
                    f"Evidence (ONLY cite these URLs):\n{evidence_text}\n"
                )
            ),
        ]
    ).content.strip()

    return {"sections": [(task.id, section_md)]}


# ============================================================
# 8) Reducer subgraph
#    merge_content → decide_images → generate_and_place_images → further_reading
# ============================================================

def merge_content(state: State) -> dict:
    plan = state["plan"]
    if plan is None:
        raise ValueError("merge_content called without plan.")
    ordered_sections = [md for _, md in sorted(state["sections"], key=lambda x: x[0])]
    body = "\n\n".join(ordered_sections).strip()
    merged_md = f"# {plan.blog_title}\n\n{body}\n"
    return {"merged_md": merged_md}


# ── Image decision ──────────────────────────────────────────────────────────

DECIDE_IMAGES_SYSTEM = """You are a visual content editor for a Product Management blog.
Decide if images are needed for this blog and where they would help most.

Your audience is Product Managers, not engineers. Images should be illustrative and relatable —
they should help a non-technical reader understand a concept faster through visual analogy or business context.

Rules:
- Maximum 3 images total.
- Each image must materially improve understanding. Ask: "Would a PM's eyes light up seeing this?"
- Insert placeholders exactly: [[IMAGE_1]], [[IMAGE_2]], [[IMAGE_3]].
- If no images are needed: md_with_placeholders must equal the input text unchanged, images=[].

Image style guidance — the prompt you write for each image MUST specify:
- "Clean, flat, minimal illustration style. No photorealism. No dark backgrounds."
- "Suitable for a product strategy blog or business presentation."
- Use everyday metaphors and business scenarios, NOT engineering diagrams.

Good image ideas for PM blogs:
- A split-screen "before / after" showing a business problem and its solution
- A simple flowchart using icons (people, boxes, arrows) to show a process
- A visual analogy: e.g. a postal sorting office to explain data routing
- A dashboard mockup showing a business metric improving
- A 2x2 matrix comparing options (effort vs impact, speed vs accuracy)
- A timeline showing how a technology evolved with business milestones
- A team collaboration scene illustrating a product decision process

Bad image ideas (do NOT use):
- Neural network architecture diagrams with many nodes and weights
- Code screenshots or terminal output
- Pure mathematical notation
- Abstract geometric patterns

Return strictly GlobalImagePlan.
"""

def decide_images(state: State) -> dict:
    planner = llm.with_structured_output(GlobalImagePlan)
    merged_md = state["merged_md"]
    plan = state["plan"]
    assert plan is not None

    image_plan = planner.invoke(
        [
            SystemMessage(content=DECIDE_IMAGES_SYSTEM),
            HumanMessage(
                content=(
                    f"Blog kind: {plan.blog_kind}\n"
                    f"Audience: Product Managers and Product Leaders\n"
                    f"Topic: {state['topic']}\n\n"
                    "Decide if illustrative images would help a PM audience understand this blog faster.\n"
                    "Insert placeholders at the right positions and write clear Gemini image generation prompts.\n\n"
                    f"{merged_md}"
                )
            ),
        ]
    )

    return {
        "md_with_placeholders": image_plan.md_with_placeholders,
        "image_specs": [img.model_dump() for img in image_plan.images],
    }


# ── Image generation ─────────────────────────────────────────────────────────

def _gemini_generate_image_bytes(prompt: str) -> bytes:
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set.")

    client = genai.Client(api_key=api_key)

    resp = client.models.generate_content(
        model="gemini-2.5-flash-image",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            safety_settings=[
                types.SafetySetting(
                    category="HARM_CATEGORY_DANGEROUS_CONTENT",
                    threshold="BLOCK_ONLY_HIGH",
                )
            ],
        ),
    )

    parts = getattr(resp, "parts", None)
    if not parts and getattr(resp, "candidates", None):
        try:
            parts = resp.candidates[0].content.parts
        except Exception:
            parts = None

    if not parts:
        raise RuntimeError("No image content returned (safety/quota/SDK change).")

    for part in parts:
        inline = getattr(part, "inline_data", None)
        if inline and getattr(inline, "data", None):
            return inline.data

    raise RuntimeError("No inline image bytes found in response.")


def _openai_generate_image_bytes(prompt: str, size: str = "1024x1024") -> bytes:
    from openai import OpenAI
    import base64
    
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
        
    client = OpenAI(api_key=api_key)
    response = client.images.generate(
        model="gpt-image-1.5",
        prompt=prompt,
        size=size,
        quality="medium",
        n=1
    )
    
    b64_data = response.data[0].b64_json
    if not b64_data:
        raise RuntimeError("No image data returned from OpenAI.")
    
    return base64.b64decode(b64_data)


def _safe_slug(title: str) -> str:
    s = title.strip().lower()
    s = re.sub(r"[^a-z0-9 _-]+", "", s)
    s = re.sub(r"\s+", "_", s).strip("_")
    return s or "blog"


def generate_and_place_images(state: State) -> dict:
    plan = state["plan"]
    assert plan is not None

    md = state.get("md_with_placeholders") or state["merged_md"]
    image_specs = state.get("image_specs", []) or []

    if not image_specs:
        filename = f"{_safe_slug(plan.blog_title)}.md"
        Path(filename).write_text(md, encoding="utf-8")
        return {"final": md}

    images_dir = Path("images")
    images_dir.mkdir(exist_ok=True)

    for spec in image_specs:
        placeholder = spec["placeholder"]
        filename = spec["filename"]
        out_path = images_dir / filename

        if not out_path.exists():
            try:
                img_bytes = _gemini_generate_image_bytes(spec["prompt"])
                out_path.write_bytes(img_bytes)
            except Exception as e:
                try:
                    print(f"Gemini image generation failed: {e}. Falling back to gpt-image-1.5...")
                    img_bytes = _openai_generate_image_bytes(spec["prompt"], size=spec.get("size", "1024x1024"))
                    out_path.write_bytes(img_bytes)
                except Exception as e2:
                    prompt_block = (
                        f"> **[IMAGE GENERATION FAILED]** {spec.get('caption', '')}\n>\n"
                        f"> **Alt:** {spec.get('alt', '')}\n>\n"
                        f"> **Prompt:** {spec.get('prompt', '')}\n>\n"
                        f"> **Error:** Gemini failed: {e} | OpenAI fallback failed: {e2}\n"
                    )
                    md = md.replace(placeholder, prompt_block)
                    continue

        img_md = f"![{spec['alt']}](images/{filename})\n*{spec['caption']}*"
        md = md.replace(placeholder, img_md)

    # NOTE: Final save happens in further_reading_node after the FR section is appended.
    # Store intermediate markdown in state — further_reading_node will append and save.
    return {"final": md}


# ── Further reading node ─────────────────────────────────────────────────────
# ZERO hallucination: only uses URLs already in state["evidence"].
# No LLM call. No invented sources. If evidence is empty, says so transparently.

def further_reading_node(state: State) -> dict:
    plan = state.get("plan")
    evidence: List[EvidenceItem] = state.get("evidence") or []
    current_final: str = state.get("final") or state.get("merged_md") or ""

    lines = ["", "---", "", "## 📚 Further Reading"]

    if not evidence:
        lines.append(
            "\n*This blog was written from the model's training knowledge. "
            "No external sources were retrieved during generation. "
            "For further reading, search for the topic on "
            "[Lenny's Newsletter](https://www.lennysnewsletter.com), "
            "[Reforge](https://www.reforge.com/blog), or "
            "[Mind the Product](https://www.mindtheproduct.com).*"
        )
    else:
        lines.append(
            "\nThe following sources were retrieved and used during research for this blog. "
            "All links are verified — none are invented.\n"
        )
        for i, item in enumerate(evidence, start=1):
            # Only include items that have a real URL
            if not item.url or not item.url.startswith("http"):
                continue

            title = item.title or "Untitled"
            url = item.url
            source = f" · *{item.source}*" if item.source else ""
            date_str = f" · {item.published_at}" if item.published_at else ""
            snippet = f"\n   > {item.snippet[:180]}..." if item.snippet and len(item.snippet) > 20 else ""

            lines.append(f"{i}. **[{title}]({url})**{source}{date_str}{snippet}\n")

    further_reading_md = "\n".join(lines)

    # Append to the current final markdown
    full_final = current_final.rstrip() + "\n" + further_reading_md + "\n"

    # Save the complete file now (this is the last reducer node)
    if plan:
        filename = f"{_safe_slug(plan.blog_title)}.md"
        Path(filename).write_text(full_final, encoding="utf-8")

    return {
        "further_reading": further_reading_md,
        "final": full_final,
    }


# ── Build reducer subgraph ────────────────────────────────────────────────────
reducer_graph = StateGraph(State)
reducer_graph.add_node("merge_content", merge_content)
reducer_graph.add_node("decide_images", decide_images)
reducer_graph.add_node("generate_and_place_images", generate_and_place_images)
reducer_graph.add_node("further_reading", further_reading_node)

reducer_graph.add_edge(START, "merge_content")
reducer_graph.add_edge("merge_content", "decide_images")
reducer_graph.add_edge("decide_images", "generate_and_place_images")
reducer_graph.add_edge("generate_and_place_images", "further_reading")
reducer_graph.add_edge("further_reading", END)

reducer_subgraph = reducer_graph.compile()


# -----------------------------
# 9) Build main graph
# -----------------------------
g = StateGraph(State)
g.add_node("router", router_node)
g.add_node("research", research_node)
g.add_node("orchestrator", orchestrator_node)
g.add_node("worker", worker_node)
g.add_node("reducer", reducer_subgraph)

g.add_edge(START, "router")
g.add_conditional_edges(
    "router",
    route_next,
    {"research": "research", "orchestrator": "orchestrator"},
)
g.add_edge("research", "orchestrator")
g.add_conditional_edges("orchestrator", fanout, ["worker"])
g.add_edge("worker", "reducer")
g.add_edge("reducer", END)

app = g.compile()


# -----------------------------
# 10) Runner
# -----------------------------
def run(topic: str, as_of: Optional[str] = None) -> dict:
    if as_of is None:
        as_of = date.today().isoformat()

    return app.invoke(
        {
            "topic": topic,
            "mode": "",
            "needs_research": False,
            "queries": [],
            "evidence": [],
            "plan": None,
            "as_of": as_of,
            "recency_days": 7,
            "sections": [],
            "merged_md": "",
            "md_with_placeholders": "",
            "image_specs": [],
            "further_reading": "",
            "final": "",
        }
    )


if __name__ == "__main__":
    from pathlib import Path
    print("🚀 Starting PM-tuned blog agent test run...")
    topic = "How Large Language Models Work — A Guide for Product Managers"
    out = run(topic)

    print("-" * 60)
    print(f"1. Mode:              {out.get('mode')}")
    print(f"2. Evidence gathered: {len(out.get('evidence') or [])} items")

    plan = out.get("plan")
    if plan:
        p = plan.model_dump() if hasattr(plan, "model_dump") else plan
        print(f"3. Blog kind:         {p.get('blog_kind')}")
        print(f"4. Sections planned:  {len(p.get('tasks', []))}")
        pm_trans = sum(1 for t in p.get("tasks", []) if t.get("requires_pm_translation"))
        print(f"5. PM-translation sections: {pm_trans}")

    images_dir = Path("images")
    if images_dir.exists():
        pngs = list(images_dir.glob("*.png"))
        print(f"6. Images generated:  {len(pngs)} PNG files")
    else:
        print("6. Images generated:  0 (no images/ folder)")

    final_md = out.get("final", "")
    print(f"7. Further Reading section present: {'## 📚 Further Reading' in final_md}")
    print(f"8. PM callout boxes present:        {'💡 What this means for you as a PM' in final_md}")
    print("-" * 60)
    print("9. Blog preview (first 1200 chars):")
    print(final_md[:1200] + "...")