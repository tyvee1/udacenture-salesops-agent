# Monitoring Report

Generated 2026-11-15 19:41 UTC by `uv run generate-monitoring-report` from `logs/runs.jsonl`.
Window: 2026-11-15T19:39:26+00:00 to 2026-11-15T19:41:32+00:00.

## Summary

| Metric | Value |
| --- | ---: |
| Total runs | 38 |
| Success rate | 100.0% |
| Failure rate | 0.0% |
| Average latency (ms) | 3155.8 |
| Median / p95 / max latency (ms) | 1841.8 / 9426.2 / 11930.4 |
| Estimated total cost (USD) | 0.019893 |
| Estimated average cost per run (USD) | 0.000523 |
| Tool calls | 31 |
| Guardrail blocks | 13 |
| HITL approvals | 2 |
| HITL rejections | 4 |
| Evaluation-suite runs included | 24 |

Success means the run ended as designed (completed, blocked by a guardrail, or rejected by a human). Failure means it ended in an error.

## Runs by final status

| Status | Runs |
| --- | ---: |
| blocked | 7 |
| completed | 27 |
| rejected | 4 |

## Tool call counts

| Tool | Calls |
| --- | ---: |
| lookup_account_context | 7 |
| analyze_crm_data | 7 |
| send_email | 7 |
| draft_email | 5 |
| lookup_account | 2 |
| summarize_pipeline | 1 |
| lookup_internal_financial_data | 1 |
| lookup_internal_hr_data | 1 |

### Tool call outcomes

| Tool:outcome | Calls |
| --- | ---: |
| analyze_crm_data:blocked | 3 |
| analyze_crm_data:executed | 4 |
| draft_email:executed | 5 |
| lookup_account:executed | 2 |
| lookup_account_context:executed | 7 |
| lookup_internal_financial_data:blocked | 1 |
| lookup_internal_hr_data:blocked | 1 |
| send_email:blocked | 1 |
| send_email:executed | 2 |
| send_email:rejected | 4 |
| summarize_pipeline:executed | 1 |

## Guardrail blocks

| Layer | Blocks |
| --- | ---: |
| input | 5 |
| output | 2 |
| tool | 6 |

| Layer:reason | Blocks |
| --- | ---: |
| tool:sandbox_violation | 3 |
| input:executive_compensation | 2 |
| tool:restricted_internal_data | 2 |
| input:prompt_injection | 1 |
| input:mergers_and_acquisitions | 1 |
| input:confidential_strategy | 1 |
| output:executive_compensation | 1 |
| output:layoffs | 1 |
| tool:unsafe_email_content | 1 |

## Human-in-the-loop decisions

| Decision source | Requests |
| --- | ---: |
| cli_flag | 5 |
| default_no_signal | 1 |

## Common failure reasons

| Reason | Runs |
| --- | ---: |
| (none) | 0 |

## Versions and models

| Prompt version | Runs |
| --- | ---: |
| 6 | 38 |

| Model | Runs |
| --- | ---: |
| vocareum:gpt-4o-mini | 26 |
| scripted (scripted) | 9 |
| scripted (isolation-demo) | 3 |

## How to read this report

- **Reliability.** 38 of 38 runs ended as designed (100.0%); 0 ended in an error (0.0%). No run failed, so the failure-reason table is empty by design.
- **Guardrails.** 13 blocks across 13 runs; the tool layer blocked most (6). A rising block rate on ordinary traffic suggests a false positive; a block on the tool or output layer means an earlier layer let something through.
- **Human approval.** 6 approval requests: 2 approved, 4 rejected, of which 1 were rejected automatically because no approval signal was given.
- **Latency.** Median 1841.8 ms, p95 9426.2 ms. Runs stopped by the input guardrail never call the model, so they pull the average down; compare p95 between reports rather than the mean.
- **Cost.** Estimated $0.019893 in total, $0.000523 per run. 12 runs used a scripted model whose token counts are estimated from text length, so treat those costs as indicative.
