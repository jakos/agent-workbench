# PR review results

Status: **UNMEASURED**. No authorized live generation or hosted activation has run.

The frozen `cases.json` contains six reconstructed known-defect cases and two clean
controls. Offline integrity, local oracles, fake scoring and failure accounting are
testable without a provider. Fake output supplies no semantic detection evidence.

| Measurement | Result |
| --- | --- |
| Live generation attempts | 0 |
| Defect detection (six cases) | unmeasured |
| False positives (two controls) | unmeasured |
| Live usage / USD | unknown; no paid run authorized |
| Provider/model prepared | Anthropic / `claude-haiku-4-5-20251001` |
| Proposed maximum | 8 calls, aggregate $4, separately confirmed manifest |

Outstanding empirical proof: obtain explicit confirmation of the concrete manifest,
run the bounded live command in [README.md](README.md), retain the report, and inspect
each finding against its independent scenario. Report failed attempts and unknown
cost alongside detection and false positives. Do not interpret this page, fake scores
or green deterministic checks as evidence of live review quality.
