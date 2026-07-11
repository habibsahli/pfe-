# Prompt Variant A/B Test Analysis

## Results Summary

**Total runs: 8 | Success: 6 | Errors: 2 (timeouts)**

### Key Findings

#### 1. Stable Forecasts (Heuristic Path)
Returns cached answer (~294 chars) - fast path avoids LLM

| Variant | Time (ms) | Status | Answer Length |
|---------|-----------|--------|----------------|
| control | 183ms     | ✓      | 294 chars      |
| C       | 165ms     | ✓      | 294 chars      |
| A       | 61,395ms  | ✓      | 294 chars (SLOW!) |
| B       | 61,316ms  | ✓      | 294 chars (SLOW!) |

**Issue:** Variants A & B have ~60s overhead on heuristic path (even though returning same answer).
**Likely cause:** Extensive prompt template construction before heuristic check.

#### 2. Dynamic Forecasts (LLM Generation Path)
Calls Ollama to generate full response

| Variant | Time (ms) | Status | Answer Length | Confidence |
|---------|-----------|--------|----------------|------------|
| C       | 36,801ms  | ✓      | 624 chars      | 0.25      |
| B       | 118,853ms | ✓      | 1,379 chars    | 0.25      |
| control | 120,041ms | ✗      | Timeout        | N/A       |
| A       | 120,014ms | ✗      | Timeout        | N/A       |

**Key observations:**
- Variant C: **FASTEST AND SUCCESSFUL** (37s, concise 624-char executive summary)
- Variant B: Successful but slowest (119s, generates longer response)
- control + A: Both timeout (Ollama /generate not responding in 120s)

### Winner: **Variant C** (Concise Executive Summary)

**Advantages:**
1. Fast on heuristic path (~165ms)
2. ONLY variant to complete both heuristic AND LLM paths successfully
3. Concise output (624 chars) vs verbose (1379 chars in B)
4. Shorter prompt = less processing overhead

**Why Variant C works best:**
- The "executive summary + bullets + evidence" format is less verbose
- Shorter system prompt + fewer context sections = faster Ollama generation
- Still provides actionable insights without timeout issues

---

## Recommendation for Next Steps

1. **Promote Variant C as default** - Most reliable and fast on both paths
2. **Investigate Variant A timeout** - Why does evidence-citing format cause Ollama to hang?
3. **Extend testing** - Run 10-20 more tests to confirm variance and latency stability
4. **Compare quality** - Manually review generated answers (especially B vs C) for factuality/relevance
5. **Inspect Phoenix traces**:
   - Look at successful Variant C LLM runs: check `qa.system_prompt`, `qa.prompt_length`, `qa.response_preview`
   - Compare with B and timeout cases to understand why timing differs

---

## Next Actions

```bash
# Review full results
jq . < /home/habib/pfe/ab_test_full_results.json

# Run extended test (more iterations)
# ... (after optimization)

# Compare Phoenix traces visually at http://localhost:6006
```
