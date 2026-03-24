from __future__ import annotations

import json


_VALID_TASKS = {
    "technical_audit",
    "keyword_strategy",
    "on_page_checklist",
    "link_building_plan",
    "seo_strategy_plan",
}


def _require_text(value: str | None, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def seo_specialist(
    task: str,
    website: str = "",
    business: str = "",
    goals: str = "",
    notes: str = "",
) -> str:
    task_name = _require_text(task, "task").lower()
    if task_name not in _VALID_TASKS:
        allowed = ", ".join(sorted(_VALID_TASKS))
        raise ValueError(f"task must be one of: {allowed}")

    context = {
        "website": str(website or "").strip(),
        "business": str(business or "").strip(),
        "goals": str(goals or "").strip(),
        "notes": str(notes or "").strip(),
    }

    if task_name == "technical_audit":
        template = _technical_audit_template(context)
    elif task_name == "keyword_strategy":
        template = _keyword_strategy_template(context)
    elif task_name == "on_page_checklist":
        template = _on_page_checklist_template(context)
    elif task_name == "link_building_plan":
        template = _link_building_template(context)
    else:
        template = _seo_strategy_plan(context)

    payload = {
        "skill": "seo_specialist",
        "task": task_name,
        "context": context,
        "output_format": "markdown",
        "result": template ,
    }
    return json.dumps(payload, ensure_ascii=True)


def _technical_audit_template(ctx: dict[str, str]) -> str:
    return (
        "# Technical SEO Audit Report\n\n"
        f"Target site: {ctx['website'] or '[add domain]'}\n"
        f"Business: {ctx['business'] or '[add niche]'}\n"
        f"Goals: {ctx['goals'] or '[add goals]'}\n\n"
        "## Crawlability and Indexation\n"
        "- Robots.txt: verify allow/deny rules and sitemap declaration.\n"
        "- XML sitemap: compare submitted URLs vs indexed URLs.\n"
        "- Crawl budget: identify crawl waste and parameter traps.\n\n"
        "## Site Architecture and Internal Linking\n"
        "- Validate click depth and orphan page count.\n"
        "- Remove redirect chains and canonical conflicts.\n\n"
        "## Core Web Vitals\n"
        "- Targets: LCP < 2.5s, INP < 200ms, CLS < 0.1.\n"
        "- Segment mobile and desktop field data.\n\n"
        "## Structured Data\n"
        "- Validate existing schema and patch errors.\n"
        "- Add missing schema types aligned to page intent.\n\n"
        f"Notes: {ctx['notes'] or '[none]'}\n"
    )


def _keyword_strategy_template(ctx: dict[str, str]) -> str:
    return (
        "# Keyword Strategy Document\n\n"
        f"Site: {ctx['website'] or '[add domain]'}\n"
        f"Niche: {ctx['business'] or '[add niche]'}\n\n"
        "## Topic Cluster Planning\n"
        "- Define pillar pages for high-intent head terms.\n"
        "- Build supporting long-tail clusters by intent stage.\n\n"
        "## Content Gap Analysis\n"
        "- Competitor keywords we do not rank for.\n"
        "- Low-hanging opportunities (positions 4-20).\n"
        "- Featured snippet candidates.\n\n"
        "## Intent Mapping\n"
        "- Informational -> guides and tutorials.\n"
        "- Commercial -> comparisons and case studies.\n"
        "- Transactional -> landing and product pages.\n\n"
        f"Primary goals: {ctx['goals'] or '[add goals]'}\n"
        f"Additional notes: {ctx['notes'] or '[none]'}\n"
    )


def _on_page_checklist_template(ctx: dict[str, str]) -> str:
    return (
        "# On-Page SEO Checklist\n\n"
        f"Target site: {ctx['website'] or '[add domain]'}\n"
        "- [ ] Title tag is within 50-60 chars and includes primary keyword.\n"
        "- [ ] Meta description includes value proposition and CTA.\n"
        "- [ ] Canonical URL is correct and self-referencing when needed.\n"
        "- [ ] One H1 aligned with primary intent.\n"
        "- [ ] H2/H3 hierarchy covers subtopics and PAA intent.\n"
        "- [ ] Primary keyword appears naturally in first 100 words.\n"
        "- [ ] Internal links connect to cluster and pillar pages.\n"
        "- [ ] Images have descriptive alt text and are compressed.\n"
        "- [ ] Relevant schema markup is valid.\n"
        "- [ ] Page passes mobile usability checks.\n\n"
        f"Business context: {ctx['business'] or '[add context]'}\n"
        f"Goal context: {ctx['goals'] or '[add goals]'}\n"
    )


def _link_building_template(ctx: dict[str, str]) -> str:
    return (
        "# Link Authority Building Plan\n\n"
        f"Website: {ctx['website'] or '[add domain]'}\n"
        "## Current Profile\n"
        "- Benchmark domain authority and referring domains.\n"
        "- Audit toxic links and prioritize cleanup if needed.\n\n"
        "## Acquisition Strategy\n"
        "- Digital PR via original data and expert commentary.\n"
        "- Linkable assets: tools, calculators, definitive guides.\n"
        "- Outreach: broken links, unlinked mentions, resource pages.\n\n"
        "## Monthly Targets\n"
        "- DR 60+ links from PR stories.\n"
        "- DR 40+ links from content assets.\n"
        "- DR 50+ links from strategic outreach.\n\n"
        f"Constraints and notes: {ctx['notes'] or '[none]'}\n"
    )


def _seo_strategy_plan(ctx: dict[str, str]) -> str:
    return (
        "# SEO Strategy Plan\n\n"
        f"Website: {ctx['website'] or '[add domain]'}\n"
        f"Business: {ctx['business'] or '[add niche]'}\n"
        f"Goals: {ctx['goals'] or '[add goals]'}\n\n"
        "## Phase 1: Discovery\n"
        "- Technical crawl, Search Console baseline, competitor benchmark.\n\n"
        "## Phase 2: Strategy\n"
        "- Keyword universe, cluster map, and prioritization matrix.\n\n"
        "## Phase 3: Execution\n"
        "- Technical fixes, on-page optimization, internal linking rollout.\n\n"
        "## Phase 4: Authority\n"
        "- Digital PR and link gap closure campaigns.\n\n"
        "## Phase 5: Measurement\n"
        "- Weekly rankings, monthly traffic quality review, quarterly roadmap updates.\n\n"
        f"Special notes: {ctx['notes'] or '[none]'}\n"
    )