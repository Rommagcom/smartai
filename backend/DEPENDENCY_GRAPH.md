"""Dependency graph analysis and refactoring plan for node_helpers.

CURRENT STATE:
node_helpers.py is a facade/re-export layer that imports from ~10 domain modules.

PROBLEM:
- Hides where functions actually come from
- Makes it hard to understand module structure
- Creates circular import risks
- Adds unnecessary indirection

SOLUTION STRATEGY:
Make imports explicit in each node/helper module. Import directly from source modules.

═══════════════════════════════════════════════════════════════════════════════

DEPENDENCY MAPPING - What comes from where

SOURCE MODULE → FUNCTIONS → USED IN

routing_policy.py:
  - WEB_SEARCH_RE, deterministic_route, fallback_explicit_export_route,
    fallback_live_data_export_route, feedback_requires_web_search,
    feedback_to_search_query, followup_export_route, is_live_data_query,
    is_web_search_intent, strip_web_search_prefix
  USED IN: router_helpers.py, router_node.py

output_postprocess.py:
  - apply_direct_route_fallback, apply_inline_cron_bridge,
    apply_inline_integration_bridge, apply_output_guardrail,
    enqueue_export_if_needed
  USED IN: output_helpers.py, output_pipeline.py

output_policy.py:
  - has_successful_export_call, requested_export_kind,
    sanitize_false_attachment_claims, should_reenqueue_export
  USED IN: output_helpers.py, output_pipeline.py

text_policy.py:
  - looks_like_small_talk, looks_like_incomplete_markdown_answer,
    sanitize_llm_answer
  USED IN: compose_helpers.py, output_helpers.py

router_recovery.py:
  - extract_non_json_answer_from_exception, extract_router_output_from_exception
  USED IN: router_helpers.py, compose_helpers.py

prompt_routing_policy.py:
  - build_enriched_system_prompt, hard_structured_route
  USED IN: nodes.py

tool_result_formatter.py:
  - build_raw_tool_summary, format_deterministic_tool_answer
  USED IN: compose_helpers.py, output_helpers.py

artifact_utils.py:
  - extract_artifacts
  USED IN: output_helpers.py, tool_execution_node.py, output_pipeline.py

web_fallback.py:
  - build_raw_web_summary, web_result_field
  USED IN: compose_helpers.py, output_helpers.py

post_output_tasks.py:
  - extract_facts_to_ltm
  USED IN: output_helpers.py

user_tool_context.py:
  - load_user_tool_context
  USED IN: router_helpers.py, router_pipeline.py

═══════════════════════════════════════════════════════════════════════════════

REFACTORING PLAN - Migrate to Direct Imports

PHASE 1: Router Pipeline (READY NOW)
  router_helpers.py:
    - routing_policy → direct import ✓ available
    - router_recovery → direct import ✓ available
    - user_tool_context → direct import ✓ available
  
  router_node.py:
    - routing_policy → already has direct imports!
    - router_decision_policy → already has direct imports!
  
  router_pipeline.py:
    - user_tool_context → direct import ✓ available

PHASE 2: Output Pipeline (READY NOW)
  output_helpers.py:
    - output_postprocess → direct import ✓ available
    - output_policy → direct import ✓ available
    - text_policy → direct import ✓ available
    - artifact_utils → direct import ✓ available
    - tool_result_formatter → direct import ✓ available
    - post_output_tasks → direct import ✓ available
    - web_fallback → direct import ✓ available
  
  output_pipeline.py:
    - output_policy → direct import ✓ available
    - artifact_utils → direct import ✓ available

PHASE 3: Compose Pipeline (READY NOW)
  compose_helpers.py:
    - text_policy → direct import ✓ available
    - tool_result_formatter → direct import ✓ available
    - router_recovery → direct import ✓ available
    - web_fallback → direct import ✓ available
  
  compose_node.py:
    - text_policy (via compose_helpers) → direct import ✓ available

PHASE 4: Other Nodes
  tool_execution_node.py:
    - artifact_utils → direct import ✓ available
  
  nodes.py:
    - prompt_routing_policy → direct import ✓ available

═══════════════════════════════════════════════════════════════════════════════

IMPLEMENTATION NOTES:

node_helpers.py will remain as a DEPRECATED re-export layer for:
  1. Backward compatibility (if external code imports from it)
  2. Gradual migration period

After migration, consider:
  - Add deprecation notice to node_helpers.py
  - Eventually remove node_helpers.py entirely when no code uses it
  - This makes dependency graph explicit and readable

BENEFITS AFTER MIGRATION:
  ✓ Source of each function is explicit in imports
  ✓ Easier to understand module organization
  ✓ Clearer dependency tree
  ✓ Easier to identify unused functions
  ✓ Harder to create circular imports
  ✓ Better IDE navigation and refactoring support

═══════════════════════════════════════════════════════════════════════════════
"""
