"""Output node: Final answer processing and state finalization."""


async def output_node(state: dict) -> dict:
    """Final output processing: guardrail check + STM append.
    
    Delegates to explicit 6-stage pipeline for:
    1. Input extraction and type validation
    2. Export context setup (kind + reenqueue decision)
    3. Export enqueuing and artifact merging
    4. Output bridges application (direct, cron, integration)
    5. Claim sanitization and web answer recovery
    6. Final persistence and memory append
    
    Pipeline: run_output_pipeline(state) -> dict
    """
    from app.graph.output_pipeline import run_output_pipeline

    return await run_output_pipeline(state)