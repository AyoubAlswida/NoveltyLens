from copy import deepcopy
from pathlib import Path

from docx import Document
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import RGBColor

SRC = Path(r"C:\Users\Lenovo\Desktop\AgenticAI-main\docs\Novelty-Lens-Comprehensive-Development-Plan-EN-v1.1.docx")
OUT = Path(r"C:\Users\Lenovo\Desktop\AgenticAI-main\docs\Novelty-Lens-Comprehensive-Development-Plan-EN-v1.2.docx")

doc = Document(SRC)


def find_exact(text):
    for p in doc.paragraphs:
        if p.text.strip() == text:
            return p
    raise ValueError(f"Paragraph not found: {text}")


def replace_text(old, new):
    p = find_exact(old)
    if p.runs:
        p.runs[0].text = new
        for r in p.runs[1:]:
            r.text = ""
    else:
        p.add_run(new)
    return p


def add_before(anchor, text, style=None):
    p = anchor.insert_paragraph_before(text, style=style)
    return p


def add_heading_before(anchor, text, level=2):
    return add_before(anchor, text, f"Heading {level}")


def set_repeat_table_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)


def set_keep_next(paragraph):
    p_pr = paragraph._p.get_or_add_pPr()
    if p_pr.find(qn("w:keepNext")) is None:
        p_pr.append(OxmlElement("w:keepNext"))


def prevent_row_split(row):
    tr_pr = row._tr.get_or_add_trPr()
    if tr_pr.find(qn("w:cantSplit")) is None:
        tr_pr.append(OxmlElement("w:cantSplit"))


def replace_anywhere(old, new):
    paragraphs = list(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                paragraphs.extend(cell.paragraphs)
    for p in paragraphs:
        if p.text.strip() == old:
            if p.runs:
                p.runs[0].text = new
                for r in p.runs[1:]:
                    r.text = ""
            else:
                p.add_run(new)
            return p
    raise ValueError(f"Text not found anywhere: {old}")


def restart_numbered_group(texts):
    numbering = doc.part.numbering_part.element
    style_ppr = doc.styles["List Number"]._element.pPr
    base_num_id = int(style_ppr.numPr.numId.val)
    base_num = numbering.find(f"w:num[@w:numId='{base_num_id}']", numbering.nsmap)
    abstract_id = base_num.find("w:abstractNumId", numbering.nsmap).get(qn("w:val"))
    existing = [int(n.get(qn("w:numId"))) for n in numbering.findall("w:num", numbering.nsmap)]
    new_id = max(existing, default=0) + 1
    num = OxmlElement("w:num")
    num.set(qn("w:numId"), str(new_id))
    abstract = OxmlElement("w:abstractNumId")
    abstract.set(qn("w:val"), abstract_id)
    num.append(abstract)
    override = OxmlElement("w:lvlOverride")
    override.set(qn("w:ilvl"), "0")
    start = OxmlElement("w:startOverride")
    start.set(qn("w:val"), "1")
    override.append(start)
    num.append(override)
    numbering.append(num)
    for text in texts:
        p = find_exact(text)
        p_pr = p._p.get_or_add_pPr()
        old_num_pr = p_pr.find(qn("w:numPr"))
        if old_num_pr is not None:
            p_pr.remove(old_num_pr)
        num_pr = OxmlElement("w:numPr")
        ilvl = OxmlElement("w:ilvl")
        ilvl.set(qn("w:val"), "0")
        num_id = OxmlElement("w:numId")
        num_id.set(qn("w:val"), str(new_id))
        num_pr.append(ilvl)
        num_pr.append(num_id)
        p_pr.insert(0, num_pr)


# Cover and executive summary corrections.
replace_text("Version 1.1 | 25 August 2026", "Version 1.2 | 25 August 2026")
replace_text(
    "A product and engineering roadmap for semantic and hybrid retrieval, human review, reference management, evidence comparison, and professional novelty reporting.",
    "A product and engineering roadmap for a free evidence-first MVP using SerpAPI discovery, EPO enrichment, Hugging Face models, section-aware passage retrieval, human review, and auditable novelty reporting.",
)
replace_text("5.3 Qwen and Qdrant Calibration Gate", "5.3 Hugging Face Embeddings and Qdrant Calibration Gate")
replace_text(
    "Validate the Qwen3 query instruction empirically before enabling semantic search.",
    "Validate the selected Hugging Face embedding model, query formatting, and score distribution empirically before enabling passage retrieval.",
)
replace_text(
    "The project already has a strong technical foundation: FastAPI, Angular 22, a single LangGraph pipeline, real SSE progress, LLM-based invention understanding, live patent search, relevance validation, embedding-based ranking, explanations, PostgreSQL history, and a written Qdrant semantic branch. The main gap is that the current experience moves directly from input to final report instead of exposing a reviewable, iterative search process.",
    "The project already has a strong technical foundation: FastAPI, Angular 22, one LangGraph pipeline, real SSE progress, Hugging Face-compatible LLM and embedding endpoints, live SerpAPI patent retrieval, full-text enrichment, LLM relevance validation, embedding-based ranking, explanations, and PostgreSQL history. Qdrant code exists, but semantic retrieval, indexing, and fusion are currently disabled in the live graph. The main gaps are the lack of review checkpoints and the current ranking rule, which assigns each patent the score of its single highest-scoring invention-chunk/patent-chunk pair rather than measuring feature coverage and preserving the supporting passages.",
)
replace_text(
    "The target platform should let users review what the system understood, approve atomic technical features, inspect keywords and classifications, follow each search strategy, curate references, and receive a feature-by-feature evidence matrix with verifiable quotations and source locations.",
    "The target platform should let users review what the system understood, approve atomic technical features, inspect keywords and classifications, follow each search strategy, curate references, and receive a feature-by-feature evidence matrix with exact quotations and stable locations. The MVP will use Hugging Face models for both LLM and embedding work, and Qdrant will be introduced after candidate reduction as a passage-retrieval index for the best 10-15 patents, not as the initial global patent source.",
)

# Add an explicit v1.2 update summary before section 1.
anchor = find_exact("1. Current-State Baseline")
add_heading_before(anchor, "Version 1.2 Update Summary", 1)
updates = [
    "Confirmed the live baseline: Qdrant semantic search, Qdrant indexing, and result fusion are disabled; keyword retrieval feeds validation directly.",
    "Confirmed the current passage comparison: 180-word windows, 20-word overlap, a maximum of 12 chunks from the start of each document, all-pairs cosine comparison, and one maximum chunk-pair score per patent.",
    "Selected Hugging Face as the continuing model ecosystem for both the LLM and embedding model; deployment may be local or through a compatible hosted inference endpoint.",
    "Selected the free-MVP data path: SerpAPI for candidate discovery, EPO OPS for bibliographic/family/full-text enrichment, PostgreSQL as the source of truth, and Qdrant as a rebuildable passage index.",
    "Defined the target funnel: 50-100 lightweight candidates, 10-15 enriched patents for passage retrieval, and 3-5 deeply verified references for the final evidence matrix.",
    "Deferred the final LLM verification prompt to a separate Claude drafting task followed by joint human review before implementation.",
]
for item in updates:
    add_before(anchor, item, "List Bullet")

# Correct outdated language in semantic retrieval sections.
replace_text(
    "Retrieval quality depends more on corpus coverage and document representations than on the vector database brand. For the MVP, build a versioned cache from externally retrieved, licensed, and verifiable patent documents; a global local corpus is not required. Never overwrite a full-text representation with an abstract-only record.",
    "Retrieval quality depends more on corpus coverage, section-aware chunking, and evidence preservation than on the vector database brand. The free MVP does not require a global patent vector corpus. It retrieves candidates externally, enriches the best 10-15 eligible patents, and indexes their passages in Qdrant for feature-level retrieval. Never overwrite a full-text representation with an abstract-only record, and never treat Qdrant as the authoritative evidence store.",
)
replace_text(
    "In free MVP mode, retrieve 50-100 lightweight candidates in total, consolidate families, semantically rerank the best 10-15 locally, and send only the best 3-5 references to deep evidence extraction.",
    "In free MVP mode, retrieve 50-100 lightweight candidates, consolidate families, reduce to the best 10-15 patents, index their eligible passages in Qdrant with Hugging Face embeddings, and send only the best 3-5 references to deep LLM evidence verification.",
)
replace_text(
    "Local semantic stage: use the existing local embedding capability to rerank only the best 10-15 candidates; do not require a global patent vector corpus for the MVP.",
    "Passage semantic stage: use the selected Hugging Face embedding model to encode approved features and the eligible passages of only the best 10-15 patents. Store and query those passage vectors in Qdrant; do not require a global patent vector corpus for the MVP.",
)
replace_text(
    "Deep evidence stage: fetch claims and relevant description passages only for the best 3-5 references and extract quotations with stable locators.",
    "Deep evidence stage: retrieve the best diverse claims and description passages per feature and patent, then use the Hugging Face LLM to verify technical disclosure. Every accepted judgment must retain an exact quotation, chunk ID, section, and stable locator.",
)

# Add detailed current and target comparison design before section 6.
anchor = find_exact("6. Lexical, Classification, and Iterative Search")
add_heading_before(anchor, "5.5 Current Production Comparison Baseline", 2)
add_before(anchor, "The current production path does not use Qdrant. It performs two direct embedding stages through the configured Hugging Face-compatible embeddings endpoint:")
current_steps = [
    "Cheap pre-filter: embed the complete invention and each candidate's title plus abstract as one text; keep the best 20 before full-text enrichment.",
    "Eligibility: enrich the survivors and remove every patent for which full text was not retrieved.",
    "LLM relevance validation: send the remaining candidate set to one batched relevance call and remove records judged irrelevant.",
    "Chunking: build each patent document as title plus full text, then split both the invention and each patent into 180-word windows with 20-word overlap, capped at 12 chunks from the start of each document.",
    "Direct scoring: embed all chunks, calculate cosine similarity for every invention-chunk/patent-chunk pair, and assign the patent the single maximum pair score.",
    "Presentation: sort patents by that maximum score and keep the best 15 for LLM rationales and recommendations.",
]
for item in current_steps:
    add_before(anchor, item, "List Number")
add_before(anchor, "Baseline limitation: one highly similar passage can produce a high patent score even when the same document fails to disclose the invention's other core features. The score-producing chunk is not currently preserved or passed explicitly to the rationale generator.", "Intense Quote")

add_heading_before(anchor, "5.6 Target Qdrant Passage-Comparison Workflow", 2)
target_steps = [
    "Obtain user approval for the technical summary, atomic features, required feature elements, required relationships, cutoff date, and search limits.",
    "Use SerpAPI to retrieve 50-100 lightweight candidates. Normalize publication identifiers, enforce the cutoff date, remove duplicates, and consolidate families.",
    "Use a cheap Hugging Face embedding pass over title and abstract, plus LLM relevance validation where approved, to reduce the pool to 10-15 patents before expensive passage processing.",
    "Use EPO OPS to obtain bibliography, priority and publication dates, family information, claims, description, and stable source locators. Exclude a patent from final evidence analysis when required claims and description are unavailable; record the exclusion reason transparently.",
    "Split claims on claim boundaries and split description text on paragraph or section boundaries. Use token windows only when a natural unit is too large. Preserve section, claim number, paragraph locator, neighboring context, source, dates, and family information in the payload.",
    "Encode approved features and eligible patent passages with the same pinned Hugging Face embedding model and revision. A model or embedding-dimension change requires a new Qdrant collection or complete re-indexing.",
    "Store one Qdrant point per passage. Qdrant payloads must include publication number, family ID, section, locator, chunk ID, language, eligibility, source date, text hash, and model revision. PostgreSQL remains the authoritative record of runs, approvals, exclusions, and final evidence.",
    "For every feature-patent pair, query all eligible passages belonging to that patent and retrieve an initial top eight. Qdrant similarity is candidate-retrieval metadata, not proof of disclosure.",
    "Remove duplicates, merge adjacent description passages when required, preserve a claims-first mix, and retain approximately three to five diverse passages for LLM verification. Retrieve neighboring context once when the selected passage is ambiguous or referential.",
    "Ask the LLM to assess every required element and relationship using only the supplied passages. Allowed feature outcomes are FULLY_DISCLOSED, PARTIALLY_DISCLOSED, NOT_DISCLOSED, and UNCLEAR.",
    "Validate the structured response in application code: every chunk ID must be an input chunk; every quotation must occur verbatim in that chunk; all required elements must be accounted for; and FULLY_DISCLOSED is invalid when any required element or relationship is missing or unclear.",
    "Aggregate verified feature judgments at document level. A novelty warning based on anticipation requires one eligible earlier publication to disclose all required elements and their relationships; do not combine separate patents to claim lack of novelty.",
]
for item in target_steps:
    add_before(anchor, item, "List Number")

add_heading_before(anchor, "5.7 Retrieval and Evidence Responsibilities", 2)
roles = [
    "SerpAPI discovers candidates; it does not supply the final evidence judgment.",
    "EPO OPS supplies verified enrichment and full-text evidence when available.",
    "The Hugging Face embedding model finds semantically promising passages.",
    "Qdrant stores and retrieves passage vectors; it does not determine novelty or disclosure.",
    "The Hugging Face LLM verifies feature elements and relationships against the supplied passages.",
    "Application code validates quotations and output schema and rejects unsupported model output.",
    "PostgreSQL stores the authoritative workflow state, user revisions, provider records, exclusions, evidence judgments, and report history.",
]
for item in roles:
    add_before(anchor, item, "List Bullet")

# Add final API/model decision before the data model section.
anchor = find_exact("8. Proposed Data Model")
add_heading_before(anchor, "7.1 Final MVP Provider and Model Decision", 2)
decisions = [
    "Candidate discovery: SerpAPI Google Patents results, with a strict request and result budget suitable for the free pilot.",
    "Authoritative enrichment: EPO OPS for bibliography, family, citations, full-text availability, claims, and description where covered.",
    "LLM: a pinned Hugging Face instruction model selected through benchmark testing; the provider adapter must support local or hosted execution without changing the workflow contract.",
    "Embeddings: a pinned Hugging Face embedding model used consistently for feature and passage vectors.",
    "Vector retrieval: Qdrant, introduced only after reduction to 10-15 patents and used as a passage index rather than the evidence source of truth.",
    "Persistence: PostgreSQL for workflow state, audit history, provider payload references, exclusions, and final reports.",
]
for item in decisions:
    add_before(anchor, item, "List Bullet")
add_before(anchor, "A patent lacking the required claims and description is excluded from final feature-disclosure analysis. Its metadata and exclusion reason may be retained for audit, but snippets and titles must never be presented as proof.", "Intense Quote")

# Update performance language and add target timings.
replace_text(
    "The goal is not maximum result volume or minimum latency at any cost; it is the strongest prior-art set within a transparent budget. Free MVP mode uses an adaptive funnel: 50-100 lightweight candidates, 10-15 local semantic analyses, and 3-5 deep evidence reviews. Estimate cost before execution, cap each strategy, deduplicate before full-text calls, and parallelize independent work only when provider quotas and failure semantics remain correct.",
    "The goal is not maximum result volume or minimum latency at any cost; it is the strongest defensible prior-art set within a transparent budget. Free MVP mode uses an adaptive funnel: 50-100 lightweight candidates, 10-15 Hugging Face/Qdrant passage analyses, and 3-5 deep evidence reviews. Estimate cost before execution, cap each strategy, deduplicate before EPO calls, reuse verified passage embeddings when permitted, and parallelize independent work only when provider quotas and failure semantics remain correct.",
)
anchor = find_exact("16. Key Risks and Mitigations")
add_heading_before(anchor, "15.1 MVP Latency Targets", 2)
timings = [
    "Typical automated run: approximately 90 seconds to 3 minutes, excluding time spent waiting for user approval.",
    "Service objective: P50 below 2 minutes and P95 below 4 minutes under the agreed free-MVP candidate limits.",
    "Hard automated timeout: 6 minutes, followed by a clear error with the failed phase; never issue a partial report.",
    "Measure each stage independently: understanding, discovery, filtering, EPO enrichment, embedding/indexing, passage retrieval, LLM evidence verification, persistence, and report assembly.",
]
for item in timings:
    add_before(anchor, item, "List Bullet")

# Add the explicit Claude prompt-drafting brief before the conclusion.
anchor = find_exact("20. Conclusion")
add_heading_before(anchor, "19.1 Immediate Next Work Package: Claude Prompt Drafting", 2)
add_before(anchor, "Claude should be asked to draft the production LLM prompt only after this architecture is accepted. The prompt is a separate reviewed artifact and must not be silently embedded into implementation code before Ayoub and the reviewing assistant approve it.")
prompt_requirements = [
    "Draft one system prompt and one parameterized user-prompt template for evaluating one technical feature against passages from one patent publication.",
    "Accept feature ID, feature text, required technical elements, required relationships, cutoff date, patent metadata, and three to five retrieved passages with chunk IDs, sections, locators, text, and retrieval scores.",
    "Use only supplied passages. Retrieval scores may guide ordering but may never be treated as evidence.",
    "Permit evidence combination only within the same publication and only when the supplied text explicitly links the passages to the same embodiment, apparatus, method, or claimed combination.",
    "Forbid external knowledge, fabricated quotations, invented locators, cross-patent combination, and later-added family-member text as pre-cutoff evidence.",
    "Return strict JSON with feature-level outcome, element-level outcomes, relationship-level outcomes, exact quotations, chunk IDs, locators, missing elements, ambiguities, and a request-more-context indicator.",
    "Use the fixed labels FULLY_DISCLOSED, PARTIALLY_DISCLOSED, NOT_DISCLOSED, and UNCLEAR. Define deterministic conditions for each label.",
    "Require exact quotations rather than paraphrases and make every evidence item traceable to an input chunk.",
    "Include positive, partial, unclear, and not-disclosed examples plus adversarial tests for high semantic similarity without complete technical disclosure.",
    "Keep legal scope narrow: the prompt performs technical disclosure verification, not a final legal opinion.",
]
for item in prompt_requirements:
    add_before(anchor, item, "List Bullet")
add_before(anchor, "Review gate: Ayoub and the reviewing assistant will examine Claude's draft for grounding, label definitions, evidence rules, JSON validity, context requests, and resistance to unsupported inference before any prompt is approved for implementation.", "Intense Quote")

# Remove model-specific or outdated wording that survived in existing tables.
replace_anywhere(
    "Calibrated Qwen retrieval, multi-vector indexing, and reranking.",
    "Calibrated Hugging Face embedding retrieval, multi-vector indexing, and reranking.",
)
replace_anywhere(
    "Calibrate Qwen/Qdrant, enable semantic indexing and fusion behind evaluation controls.",
    "Calibrate Hugging Face embeddings and Qdrant passage retrieval behind evaluation controls.",
)
replace_anywhere(
    "Calibrate Qwen threshold and query instruction.",
    "Calibrate embedding thresholds, query formatting, and model revision.",
)
replace_anywhere(
    "Implement free-mode adaptive funnel: 50-100 lightweight candidates, 10-15 local semantic analyses, and 3-5 deep evidence reviews.",
    "Implement the free-mode adaptive funnel: 50-100 lightweight candidates, 10-15 Hugging Face/Qdrant passage analyses, and 3-5 deep evidence reviews.",
)

# Each inserted workflow is an independent numbered sequence.
restart_numbered_group(current_steps)
restart_numbered_group(target_steps)

# Add current source files to Appendix A.
anchor = find_exact("Note: Patsnap does not publicly disclose its embedding model, vector dimensions, or exact reranking architecture. Any architectural interpretation of those details is an engineering inference, not a published fact.")
for item in [
    "Current comparison implementation: backend/compare.py and backend/embeddings.py.",
    "Current disabled Qdrant topology and direct keyword-to-validation edge: backend/graph/pipeline.py.",
    "Current retrieval, full-text eligibility filtering, and dormant Qdrant nodes: backend/graph/searching.py and backend/search/vector_store.py.",
    "Current ranking and result cap: backend/graph/ranking.py.",
]:
    add_before(anchor, item, "List Bullet")

# Keep every visible run black and force left-to-right paragraph direction.
for p in doc.paragraphs:
    p_pr = p._p.get_or_add_pPr()
    bidi = p_pr.find(qn("w:bidi"))
    if bidi is not None:
        p_pr.remove(bidi)
    for run in p.runs:
        run.font.color.rgb = RGBColor(0, 0, 0)
        r_pr = run._r.get_or_add_rPr()
        rtl = r_pr.find(qn("w:rtl"))
        if rtl is not None:
            r_pr.remove(rtl)

for table in doc.tables:
    if table.rows:
        set_repeat_table_header(table.rows[0])
        for p in table.rows[0].cells[0].paragraphs:
            set_keep_next(p)
    for row in table.rows:
        prevent_row_split(row)
        for cell in row.cells:
            for p in cell.paragraphs:
                p_pr = p._p.get_or_add_pPr()
                bidi = p_pr.find(qn("w:bidi"))
                if bidi is not None:
                    p_pr.remove(bidi)
                for run in p.runs:
                    run.font.color.rgb = RGBColor(0, 0, 0)

doc.core_properties.title = "Novelty Lens Comprehensive Development Plan"
doc.core_properties.subject = "Updated MVP search, passage retrieval, and prompt-development plan"
doc.core_properties.comments = "Version 1.2: current baseline, Hugging Face/Qdrant workflow, and Claude prompt drafting brief."
doc.save(OUT)
print(OUT)
